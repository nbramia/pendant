"""Near-field wearer signal (wearer.py): per-turn features on synthetic near and far audio,
the logistic model, and its use in naming (speakers.wearer_pass via rescore_turns).
Without a fitted model nothing changes."""
from __future__ import annotations

import io
import json

import numpy as np
import pytest
from test_process import daily_for, fake_embedder, fake_whisper, proc, run, section_body  # noqa: F401
from test_turn_rescore import A, B, PRINTS, conversation, write_two_voices

from pendant_direct import eval_speakers, process, speakers, wearer

SR = 16_000
U = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)    # nobody enrolled


def voiced(seconds: float, seed: int = 0) -> np.ndarray:
    """Speech-like: a 150 Hz buzz with harmonics, in 180 ms syllables with sharp 70 ms gaps."""
    t = np.arange(int(seconds * SR)) / SR
    x = sum(np.sin(2 * np.pi * 150 * k * t) / k for k in range(1, 12))
    gate = ((t % 0.25) < 0.18).astype(np.float32)
    return (0.3 * x * gate).astype(np.float32)


def near(seconds, seed=0):
    rng = np.random.default_rng(seed)
    return voiced(seconds) + 0.002 * rng.normal(size=int(seconds * SR)).astype(np.float32)


def far(seconds, seed=0):
    """Quiet and reverberated: the same buzz through a 0.7 s exponentially decaying room."""
    rng = np.random.default_rng(seed)
    n = int(0.7 * SR)
    ir = rng.normal(size=n) * np.exp(-6.9 * np.arange(n) / n)
    ir[0] = 1.0
    dry = voiced(seconds)
    size = 1 << int(np.ceil(np.log2(len(dry) + n)))
    wet = np.fft.irfft(np.fft.rfft(dry, size) * np.fft.rfft(ir, size), size)[:len(dry)]
    wet = 0.03 * wet / np.abs(wet).max()
    return (wet + 0.002 * rng.normal(size=len(wet))).astype(np.float32)


def words_over(t0, t1):
    return [(t, t + 0.35, "w") for t in np.arange(t0, t1 - 0.35, 0.4)]


def test_a_loud_dry_voice_reads_near_and_a_quiet_reverberated_one_far():
    audio = np.concatenate([near(4), far(4, seed=1)])
    (ln, dn, fn), (lf, df, ff) = wearer.clip_features(audio, [words_over(0, 4), words_over(4, 8)])
    assert ln > lf + 10           # louder above the conversation's floor
    assert dn > df + 3            # steeper offsets: no room echo filling the gaps
    assert fn < ff                # less noise-like
    assert wearer.clip_features(audio, [[(0.0, 0.05, "w")]]) == [None]   # too little audio


def test_the_model_fits_round_trips_and_rejects_a_malformed_config():
    rng = np.random.default_rng(0)
    rows = [("DEV", (20 + rng.normal(), 8 + rng.normal(), -1.6), 0.1 + 0.05 * rng.normal(), 1) for _ in range(40)]
    rows += [("DEV", (8 + rng.normal(), 4 + rng.normal(), -1.2), -0.2 + 0.05 * rng.normal(), 0) for _ in range(40)]
    m = wearer.fit(rows)
    assert m.coef[0] > 0 and m.coef[1] > 0 and m.coef[3] > 0
    p = [wearer.sigmoid(m.logit(f, s, m.norm(d))) for d, f, s, _y in rows]
    assert np.mean([(q >= 0.5) == bool(r[3]) for q, r in zip(p, rows)]) > 0.95
    back = wearer.Model.from_config(json.loads(json.dumps(m.to_config())))
    assert back.norm("DEV") == m.devices["DEV"] and back.coef == pytest.approx(m.coef, abs=1e-5)
    assert back.norm("OTHER") == back.pooled                                  # a pendant it never saw
    assert back.norm("OTHER", {"mean": [0, 0, 0], "std": [1, 1, 1], "n": 500})["n"] == 500   # its own turns
    assert back.norm("OTHER", {"mean": [0, 0, 0], "std": [1, 1, 1], "n": 5}) == back.pooled
    for bad in (None, {}, {**m.to_config(), "coef": [1, 2]}, {**m.to_config(), "features": ["x"]},
                {**m.to_config(), "pooled": {"mean": [0, 0, 0], "std": [0, 1, 1]}},
                {**m.to_config(), "pooled": {"mean": [0, 0, float("nan")], "std": [1, 1, 1]}},
                {**m.to_config(), "bias": float("inf")}, {**m.to_config(), "score": {"mean": 0, "std": -1}},
                {**m.to_config(), "score": [1, 2]}, {**m.to_config(), "devices": ["x"]},
                {**m.to_config(), "devices": {"D": "nope"}}):
        assert wearer.Model.from_config(bad) is None


def test_decide_names_unnames_and_keeps_a_strong_voiceprint():
    assert wearer.decide(0.95, False, None, 0.65, 0.9).name
    assert not wearer.decide(0.85, False, None, 0.65, 0.9).name
    assert wearer.decide(0.05, True, 0.6, 0.65, 0.9).unname
    assert not wearer.decide(0.05, True, 0.7, 0.65, 0.9).unname        # above the strict threshold
    assert not wearer.decide(0.2, True, 0.6, 0.65, 0.9).unname         # not low enough
    assert not wearer.decide(None, True, None, 0.65, 0.9).unname


def audio_for(spec, kinds, gap=0.3):
    """The clip for conversation(spec): each turn's stretch near or far."""
    total = sum(d for _v, d in spec) + gap * len(spec) + 1
    out = np.zeros(int(total * SR), dtype=np.float32)
    t = 0.0
    for i, ((_v, dur), k) in enumerate(zip(spec, kinds)):
        piece = (near if k == "near" else far)(dur + gap, seed=i)
        a = int(t * SR)
        out[a:a + len(piece)] = piece
        t += dur + gap
    return out


def level_model(scoring="mean"):
    """Probability rises with level alone: near turns ~1, far turns ~0."""
    ln = wearer.clip_features(near(4), [words_over(0, 4)])[0][0]
    lf = wearer.clip_features(far(4), [words_over(0, 4)])[0][0]
    st = {"mean": [(ln + lf) / 2, 0.0, 0.0], "std": [1.0, 1.0, 1.0], "n": 100}
    return wearer.Model([1.0, 0.0, 0.0, 0.0], 0.0, scoring, 0.0, 1.0, {}, st)


def diarize(spec, kinds, **kw):
    segs, emb = conversation(spec)
    audio = audio_for(spec, kinds)
    sc = speakers.TurnScorer(PRINTS, audio_loader=lambda _c: audio, **{"wearer": "Noah", **kw})
    return speakers.diarize_words(None, segs, emb, PRINTS, cluster_threshold=0.5, match_threshold=0.5, rescorer=sc)


def labels(d):
    return [t.label for t in d.turns]


def test_without_a_model_features_are_measured_and_nothing_changes():
    spec, kinds = [(U, 4), (B, 4), (U, 4)], ["near", "far", "near"]
    on = diarize(spec, kinds)
    off = diarize(spec, kinds, features=False)
    assert labels(on) == labels(off) == ["Speaker A", "Alex", "Speaker A"]
    assert [c.name for c in on.clusters] == [c.name for c in off.clusters]
    turns = on.rescore["turns"]
    assert [t["voice"] for t in turns] == ["Speaker A", "Alex", "Speaker A"]
    assert turns[0]["level_db"] > turns[1]["level_db"] + 10
    assert all(t["score"] is not None for t in turns) and "wearer" not in on.rescore
    assert "turns" not in off.rescore and on.rescore["features_cpu_s"] >= 0


def test_a_near_voice_is_named_the_wearer():
    d = diarize([(U, 4), (B, 4), (U, 4)], ["near", "far", "near"], wearer_model=level_model())
    assert labels(d) == ["Noah", "Alex", "Noah"]
    w = d.rescore["wearer"]
    assert w["named"] == ["Noah"] and dict(w["p"])["Noah"] > 0.99 and dict(w["p"])["Alex"] < 0.01
    assert {c.name for c in d.clusters} == {"Noah", "Alex"}


def test_a_far_voice_loses_the_wearers_name_unless_its_voiceprint_score_is_strict():
    spec, kinds = [(A, 4), (B, 4), (A, 4)], ["far", "near", "far"]
    assert labels(diarize(spec, kinds)) == ["Noah", "Alex", "Noah"]
    d = diarize(spec, kinds, wearer_model=level_model(), wearer_strict=1.01)
    assert labels(d) == ["Speaker A", "Alex", "Speaker A"]
    assert d.rescore["wearer"]["unnamed"] == ["Speaker A"]
    kept = diarize(spec, kinds, wearer_model=level_model(), wearer_strict=0.9)   # cosine 1.0 with his voiceprint
    assert labels(kept) == ["Noah", "Alex", "Noah"]


def test_a_voice_strongly_named_someone_else_keeps_its_name():
    spec, kinds = [(B, 4), (A, 6), (B, 4)], ["near", "far", "near"]
    d = diarize(spec, kinds, wearer_model=level_model(), wearer_strict=1.01)
    assert labels(d) == ["Alex", "Speaker A", "Alex"]          # cosine 1.0 with Alex's voiceprint
    weak = speakers.normalize(B + 0.55 * U)                         # Alex at 0.88: above 0.5, under 0.5 + 0.4
    d = diarize([(weak, 4), (A, 6), (weak, 4)], kinds, wearer_model=level_model(), wearer_strict=1.01, margin=0.4)
    assert labels(d) == ["Noah", "Speaker A", "Noah"]


def test_a_voice_with_under_5_s_of_speech_is_left_alone():
    d = diarize([(U, 4), (B, 4), (B, 4)], ["near", "far", "far"], wearer_model=level_model())
    assert labels(d) == ["Speaker A", "Alex", "Alex"]          # one 3.5 s turn
    assert dict(d.rescore["wearer"]["p"])["Speaker A"] > 0.99 and not d.rescore["wearer"]["named"]
    d = diarize([(U, 4), (B, 4), (B, 4)], ["near", "far", "far"], wearer_model=level_model(), wearer_min_s=3.0)
    assert labels(d)[0] == "Noah" and [c.wearer_named for c in d.clusters] == [True, False]


def test_digital_silence_does_not_sink_the_noise_floor():
    audio = np.concatenate([np.zeros(10 * SR, dtype=np.float32), near(4)])
    db = wearer.frame_db(audio)
    assert wearer.noise_floor(db) > -60 and wearer.noise_floor(np.zeros(5, np.float32) - 100) == wearer.FLOOR_MIN_DB
    (lv, _d, _f), = wearer.clip_features(audio, [[(w[0] + 10, w[1] + 10, "w") for w in words_over(0, 4)]])
    assert lv < 80


def test_a_model_fitted_for_another_scoring_mode_is_not_applied():
    d = diarize([(U, 4), (B, 4), (U, 4)], ["near", "far", "near"], wearer_model=level_model("calibrated"))
    assert labels(d) == ["Speaker A", "Alex", "Speaker A"]
    assert "calibrated" in d.rescore["wearer"]["skipped"]


def test_unreadable_audio_leaves_rescoring_as_it_was():
    segs, emb = conversation([(U, 4), (B, 4), (U, 4)])

    def broken(_clip):
        raise OSError("gone")
    sc = speakers.TurnScorer(PRINTS, audio_loader=broken, wearer_model=level_model(), wearer="Noah")
    d = speakers.diarize_words(None, segs, emb, PRINTS, cluster_threshold=0.5, match_threshold=0.5, rescorer=sc)
    assert labels(d) == ["Speaker A", "Alex", "Speaker A"]
    assert "OSError" in d.rescore["features_error"] and "error" not in d.rescore


def write_conversation(proc, fake_whisper, fake_embedder, spec, monkeypatch, kinds):
    write_two_voices(proc, fake_whisper, fake_embedder, spec)
    set_speakers(proc, cluster_threshold=0.5)
    audio = audio_for(spec, kinds)
    monkeypatch.setattr(speakers, "load_audio", lambda *_a, **_k: audio)


def set_speakers(proc, **kw):
    cfg = json.loads((proc.DATA / "config.json").read_text())
    cfg["speakers"].update(kw)
    (proc.DATA / "config.json").write_text(json.dumps(cfg))


def test_the_processor_stores_each_turns_features(proc, fake_whisper, fake_embedder, monkeypatch):
    write_conversation(proc, fake_whisper, fake_embedder, [(A, 4), (B, 4), (A, 4)], monkeypatch,
                       ["near", "far", "near"])
    con, n = run(proc)
    assert n == 1
    rows = con.execute("SELECT device, tag, level_db, decay_db, flatness, score, p FROM speaker_turns "
                       "ORDER BY start_s").fetchall()
    assert [r[1] for r in rows] == ["Noah", "Alex", "Noah"] and all(r[0] == "DEVA" for r in rows)
    assert rows[0][2] > rows[1][2] + 10 and all(r[5] is not None and r[6] is None for r in rows)


def test_the_processor_names_the_wearer_with_a_model(proc, fake_whisper, fake_embedder, monkeypatch):
    write_conversation(proc, fake_whisper, fake_embedder, [(U, 4), (B, 4), (U, 4)], monkeypatch,
                       ["near", "far", "near"])
    set_speakers(proc, wearer_model=level_model().to_config())
    con, _n = run(proc)
    body = section_body(daily_for(proc).read_text())
    assert [p.split("**")[1] for p in body.split("\n\n")] == ["Noah:", "Alex:", "Noah:"]
    assert con.execute("SELECT label, labeled_by, wearer_named FROM speaker_clusters ORDER BY tag").fetchall() == [
        ("Alex", "match", 0), ("Noah", "match", 1)]
    assert all(p is not None for (p,) in con.execute("SELECT p FROM speaker_turns"))


def test_a_malformed_model_changes_nothing(proc, fake_whisper, fake_embedder, monkeypatch, capsys):
    write_conversation(proc, fake_whisper, fake_embedder, [(U, 4), (B, 4), (U, 4)], monkeypatch,
                       ["near", "far", "near"])
    set_speakers(proc, wearer_model={"coef": "nope"})
    run(proc)
    assert "Noah" not in daily_for(proc).read_text()
    assert "wearer_model is malformed" in capsys.readouterr().out


def test_a_wearer_p_out_of_range_drops_the_model(proc):
    for wp in (0.4, 1.0, "x"):
        ws = process.wearer_settings({"wearer_model": level_model().to_config(), "wearer_p": wp}, "D")
        assert ws["wearer_model"] is None and ws["wearer_p"] == 0.9
    assert process.wearer_settings({"wearer_model": level_model().to_config(), "wearer_p": 0.8}, "D")["wearer_model"]


def test_storing_turns_can_fail_without_losing_the_conversation(proc, fake_whisper, fake_embedder, monkeypatch):
    write_conversation(proc, fake_whisper, fake_embedder, [(A, 4), (B, 4), (A, 4)], monkeypatch,
                       ["near", "far", "near"])
    monkeypatch.setattr(proc, "save_turns", lambda *a: 1 / 0)
    con, n = run(proc)
    assert n == 1 and con.execute("SELECT COUNT(*) FROM chunks WHERE done = 0").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM speaker_clusters").fetchone()[0] == 2


def test_a_pendant_the_model_never_saw_uses_its_stored_turns(proc):
    con = process.db()
    con.executemany("INSERT INTO speaker_turns (device, start_ms, level_db, decay_db, flatness) VALUES (?,?,?,?,?)",
                    [("NEWDEV", i, 30.0 + i % 2, 9.0, -1.5) for i in range(wearer.MIN_DEVICE_TURNS)])
    sp = {"wearer_model": level_model().to_config()}
    ws = process.wearer_settings(sp, "NEWDEV", con)
    assert ws["wearer_model"].norm("NEWDEV", ws["device_stats"])["mean"][0] == pytest.approx(30.5)
    assert process.wearer_settings({}, "NEWDEV", con)["wearer_model"] is None


def test_turn_labels_come_from_the_lines_a_turn_lies_in():
    words = [(0.0, 0.4, "one"), (0.5, 0.9, "two"), (1.0, 1.4, "three"), (2.0, 2.4, "four"), (2.5, 2.9, "five")]
    truth = [("Noah", ["one", "two", "three"]), (None, ["four", "five"])]
    turns = [{"start": 0.0, "end": 1.4}, {"start": 2.0, "end": 2.9}, {"start": 0.0, "end": 2.9}]
    assert eval_speakers.turn_labels(truth, [True, False], words, turns) == [("noah", "hand"), None, ("noah", "hand")]
    truth = [("Noah", ["one", "two", "three"]), ("Alex", ["four", "five"])]
    assert eval_speakers.turn_labels(truth, [True, False], words, turns)[2] is None    # 60/40: disagree


def test_fit_wearer_reports_held_out_accuracy_and_saves_only_when_asked(tmp_path, monkeypatch):
    """Two hand-labelled conversations: Noah near (Speaker A), Alex far (Speaker B)."""
    from synth_audio import write_tone
    data, notes, cache = tmp_path / "data", tmp_path / "notes", tmp_path / "cache"
    for p in (data, notes, cache, data / "clips"):
        p.mkdir()
    monkeypatch.setattr(process, "DB", data / "pages.db")
    (data / "config.json").write_text(json.dumps({"speakers": {"cluster_threshold": 0.5}}))
    for k in (0, 2):     # two samples each: enough to name a voice (speakers.min_samples)
        speakers.enroll(data / "voiceprints", "Noah", A, ["Noah"], cluster_id=901 + k)
        speakers.enroll(data / "voiceprints", "Alex", B, ["Alex"], cluster_id=902 + k)
    spec = [(A, 4), (B, 4)] * 6
    kinds = ["near", "far"] * 6
    audio = audio_for(spec, kinds)
    monkeypatch.setattr(speakers, "load_audio", lambda *_a, **_k: audio)
    con = process.db()
    md = "# Day\n"
    for k in range(2):
        clip = data / "clips" / f"c{k}.opus"
        write_tone(clip, seconds=1.0)
        segs, emb = conversation(spec)
        (cache / f"c{k}.json").write_text(json.dumps({"low_conf_share": 0, "segs": [
            [s[0], s[1], s[2], [[w[0], w[1], f"{w[2]}x{k}y{i}"] for w in s.words]] for i, s in enumerate(segs)]}))
        heading = f"1{k}:00 — Chat (1m) · new"
        md += f"\n## {heading}\n\n" + "\n\n".join(
            f"[1{k}:00] **{'Speaker A' if i % 2 == 0 else 'Speaker B'}:** "
            + " ".join(f"{w[2]}x{k}y{i}" for w in s.words) for i, s in enumerate(segs)) + "\n\n---\n"
        con.execute("INSERT INTO conversations (device, start_ms, day, raw_heading, clip) VALUES ('DEVA', ?, "
                    "'2026-11-08', ?, ?)", (k, heading, str(clip)))
        for tag, label, e in (("Speaker A", "Noah", A), ("Speaker B", "Alex", B)):
            con.execute("INSERT INTO speaker_clusters (device, start_ms, tag, label, labeled_by, label_kind, embedding) "
                        "VALUES ('DEVA', ?, ?, ?, 'user', 'person', ?)", (k, tag, label, e.tobytes()))
    con.commit()
    con.close()
    (notes / "2026-11-08.md").write_text(md)
    buf = io.StringIO()
    res = eval_speakers.fit_wearer(data, notes, "2026-11-08", cache, out=buf, embedder=emb)
    text = buf.getvalue()
    assert "labelled turns: 24 (12 wearer, 12 other; 24 from hand labels)" in text
    assert res["full"]["acc"] == 1.0 and res["full"]["n"] == 24
    assert "pendant DEVA: 24 turns" in text and "not saved" in text
    assert "wearer_model" not in json.loads((data / "config.json").read_text())["speakers"]
    assert "x0y" not in text                                   # counts only, no transcript words
    eval_speakers.fit_wearer(data, notes, "2026-11-08", cache, out=io.StringIO(), embedder=emb, save=True)
    saved = json.loads((data / "config.json").read_text())["speakers"]
    assert saved["cluster_threshold"] == 0.5
    m = wearer.Model.from_config(saved["wearer_model"])
    assert m is not None and "DEVA" in m.devices and m.info["turns"] == 24 and m.info["hand_only"]
    assert m.info["heldout_accuracy_hand"] == 1.0


def test_auto_labels_never_include_voices_the_wearer_signal_named(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "DB", tmp_path / "pages.db")
    notes = tmp_path / "notes"
    notes.mkdir()
    heading = "10:00 — Chat (1m) · new"
    (notes / "2026-11-08.md").write_text(f"# Day\n\n## {heading}\n\n[10:00] **Noah:** one two three\n\n"
                                         "[10:00] **Alex:** four five six\n\n---\n")
    con = process.db()
    for tag, wn in (("Noah", 1), ("Alex", 0)):
        con.execute("INSERT INTO speaker_clusters (device, start_ms, tag, label, labeled_by, wearer_named) "
                    "VALUES ('D', 1, ?, ?, 'match', ?)", (tag, tag, wn))
    got = eval_speakers.truth_lines(con, notes, "D", 1, "2026-11-08", heading, auto=True)
    assert [n for n, _t in got] == [None, "Alex"]
    assert [n for n, _t in eval_speakers.truth_lines(con, notes, "D", 1, "2026-11-08", heading)] == [None, None]


def test_malformed_wearer_settings_never_touch_rescoring(proc, fake_whisper, fake_embedder, monkeypatch):
    for bad in ({"wearer_strict": "bad"}, {"wearer_strict_calibrated": None}, {"wearer_p": [1]},
                {"wearer_strict": float("nan")}):
        ws = process.wearer_settings(bad, "D")                       # no model: no effect, no error
        assert ws["wearer_model"] is None and ws["wearer_strict"] == 0.65 and ws["wearer_strict_calibrated"] == 2.5
        ws = process.wearer_settings({**bad, "wearer_model": level_model().to_config()}, "D")
        assert ws["wearer_model"] is None
    # end to end: two people in one voice are still split by turn rescoring
    write_two_voices(proc, fake_whisper, fake_embedder, [(A, 4), (B, 4), (A, 4)])
    set_speakers(proc, wearer_strict="bad")
    run(proc)
    body = section_body(daily_for(proc).read_text())
    assert [p.split("**")[1] for p in body.split("\n\n")] == ["Noah:", "Alex:", "Noah:"]


def test_a_model_config_of_the_wrong_types_is_ignored():
    good = level_model().to_config()
    for bad in ({**good, "features": 1}, {**good, "features": "level_db"}, {**good, "coef": 3},
                {**good, "coef": {"a": 1}}, {**good, "pooled": [1]}, {**good, "score": 2}, {**good, "devices": 5},
                {**good, "bias": None}, {**good, "bias": "x"}):
        assert wearer.Model.from_config(bad) is None
    assert wearer.Model.from_config(good) is not None


class Watched(np.ndarray):
    """Frame energies that remember the largest array derived from them."""
    biggest = [0]

    def __array_finalize__(self, obj):
        if obj is not None:
            Watched.biggest[0] = max(Watched.biggest[0], self.size)


def test_a_turns_features_cost_follows_the_turn_not_the_clip():
    audio = np.zeros(20 * 60 * SR, dtype=np.float32)            # a 20-minute clip...
    audio[60 * SR:64 * SR] = near(4)                              # ...with one 4 s turn
    db = wearer.frame_db(audio)
    floor = wearer.noise_floor(db)
    words = [(w[0] + 60, w[1] + 60, "w") for w in words_over(0, 4)]
    plain = wearer.turn_features(audio, db, floor, words)
    watched = db.view(Watched)
    Watched.biggest[0] = 0
    assert wearer.turn_features(audio, watched, floor, words) == pytest.approx(plain)
    assert Watched.biggest[0] < 5 * SR // wearer.FRAME          # never an array the length of the clip (120k)


def test_storing_turns_is_all_or_nothing(proc):
    con = process.db()

    class D:
        rescore = {"turns": [{"start": 0, "end": 3, "speech_s": 3, "voice": "Noah", "level_db": 20.0,
                              "decay_db": 6.0, "flatness": -1.5, "score": 0.1}]}
    process.save_turns(con, "D", 1, D)
    con.commit()
    D.rescore = {"turns": [{"start": 0, "end": 3}]}                # missing fields: fails mid-way
    with pytest.raises(KeyError):
        process.save_turns(con, "D", 1, D)
    con.commit()
    assert con.execute("SELECT tag, level_db FROM speaker_turns").fetchall() == [("Noah", 20.0)]
    D.rescore = {"turns": []}
    process.save_turns(con, "D", 1, D)
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM speaker_turns").fetchone()[0] == 0
