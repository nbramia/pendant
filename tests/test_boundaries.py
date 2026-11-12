"""Turn boundaries (speakers.Boundaries): blip absorption, change points inside long turns,
audio cleanup for the voice model only, their config switches and fallbacks, and the eval's
turn mode. Fake 4-d embeddings."""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest
from test_process import (at, daily_for, fake_embedder, fake_whisper, pages, proc, run,  # noqa: F401
                          section_body, write_segment)
from test_speakers import by_time, one_segment, timeline_words

from pendant_direct import eval_speakers, process, speakers
from pendant_direct.transcribe import Seg

A = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
B = speakers.normalize(np.array([0.3, 0.954, 0.0, 0.0], dtype=np.float32))   # 0.3 from A
MID = speakers.normalize(A + B)                                              # as like A as B


def blip_world(blip_vec):
    """12 s of A with two words at 6.0-6.8 s labelled B; windows embed as A except those
    centred on the blip, which embed as blip_vec. Returns (words, labels, windows, x, cents)."""
    words = timeline_words(0.0, 12.0)
    labels = [1 if 6.0 <= w[0] < 6.8 else 0 for w in words]
    assert labels.count(1) == 2
    windows, _v = speakers.voice_windows(words)
    x = np.stack([blip_vec if abs((s + e) / 2 - 6.4) <= 0.8 else A for s, e in windows])
    return words, labels, windows, x, np.stack([A, B])


def test_a_two_word_blip_between_one_voice_is_absorbed():
    words, labels, windows, x, cents = blip_world(MID)          # sounds as much like A as like B
    out, info = speakers.absorb_blips(words, labels, windows, x, cents, speakers.Boundaries(blip_absorb=True))
    assert set(out) == {0} and info == {"absorbed": 1, "kept": 0, "no_evidence": 0}


def test_a_blip_whose_embedding_clearly_differs_is_kept():
    words, labels, windows, x, cents = blip_world(B)            # clearly the other voice
    out, info = speakers.absorb_blips(words, labels, windows, x, cents, speakers.Boundaries(blip_absorb=True))
    assert out == labels and info == {"absorbed": 0, "kept": 1, "no_evidence": 0}


def test_a_blip_of_more_words_than_blip_words_is_left_alone():
    words, labels, windows, x, cents = blip_world(MID)
    out, info = speakers.absorb_blips(words, labels, windows, x, cents,
                                      speakers.Boundaries(blip_absorb=True, blip_words=1))
    assert out == labels and info == {"absorbed": 0, "kept": 0, "no_evidence": 0}


def test_a_blip_no_window_is_mostly_inside_is_kept():
    """Missing evidence keeps the run: a 0.4 s blip that no 1.5 s window lies half inside."""
    words = timeline_words(0.0, 6.0) + [(6.0, 6.2, "no"), (6.2, 6.4, "no")] + timeline_words(6.4, 12.4)
    labels = [1 if 6.0 <= w[0] < 6.4 else 0 for w in words]
    windows, _v = speakers.voice_windows(words)
    x = np.stack([MID if abs((s + e) / 2 - 6.2) <= 0.8 else A for s, e in windows])   # as like A as B
    out, info = speakers.absorb_blips(words, labels, windows, x, np.stack([A, B]),
                                      speakers.Boundaries(blip_absorb=True))
    assert out == labels and info == {"absorbed": 0, "kept": 0, "no_evidence": 1}


def test_a_short_run_joins_its_longer_neighbour_only_on_evidence():
    words = timeline_words(0.0, 12.0)
    labels = [1 if 6.0 <= w[0] < 7.2 else (2 if w[0] >= 7.2 and w[0] < 8.0 else 0) for w in words]
    windows, _v = speakers.voice_windows(words)
    cents = np.stack([A, B, speakers.normalize(np.array([0, 0, 1.0, 0], dtype=np.float32))])
    b = speakers.Boundaries(blip_absorb=True, short_run_s=1.5)
    # run 1 (1.05 s of speech) between voice 0 (6 s) and voice 2 (0.7 s): its audio sounds like voice 0
    x = np.tile(A, (len(windows), 1))
    out, info = speakers.absorb_blips(words, labels, windows, x, cents, b)
    assert {lab for w, lab in zip(words, out) if 6.0 <= w[0] < 7.2} == {0} and info["absorbed"] >= 1
    # the same run whose audio is clearly voice 1 stays
    x = np.stack([B if 6.0 <= (s + e) / 2 < 7.2 else A for s, e in windows])
    out, _info = speakers.absorb_blips(words, labels, windows, x, cents, b)
    assert {lab for w, lab in zip(words, out) if 6.0 <= w[0] < 7.2} == {1}


def blip_conversation():
    """A speaks 6 s, B says two words, A speaks 6 s, then B speaks 6 s (B is a real voice here)."""
    spec = [(A, 6.0), (B, 0.8), (A, 6.0), (B, 6.0)]
    segs, switches, t = [], [], 0.0
    for v, dur in spec:
        ws = timeline_words(t, t + dur)
        segs.append(Seg(ws[0][0], ws[-1][1], " ".join(w[2] for w in ws), ws))
        switches.append((t + dur + 0.15, v))
        t += dur + 0.3
    switches[-1] = (1e9, switches[-1][1])
    return segs, by_time(switches)


def voices_in_order(turns):
    """The turn labels with consecutive repeats collapsed (who speaks after whom)."""
    out = []
    for t in turns:
        if not out or out[-1] != t.label:
            out.append(t.label)
    return out


def test_blip_absorption_runs_inside_diarize_words_and_only_when_switched_on(tmp_path):
    segs, emb = blip_conversation()
    kw = dict(cluster_threshold=0.5, match_threshold=0.5)
    short = dict(min_run_s=0.5)       # let the 2-word turn through the flicker filter
    off = speakers.diarize_words(tmp_path / "c.opus", segs, emb, {}, **kw,
                                 boundaries=speakers.Boundaries(**short))
    assert voices_in_order(off.turns) == ["Speaker A", "Speaker B", "Speaker A", "Speaker B"]
    assert off.boundaries is None
    kept = speakers.diarize_words(tmp_path / "c.opus", segs, emb, {}, **kw,
                                  boundaries=speakers.Boundaries(**short, blip_absorb=True))
    assert voices_in_order(kept.turns) == ["Speaker A", "Speaker B", "Speaker A", "Speaker B"]
    assert kept.boundaries == {"blips": {"absorbed": 0, "kept": 1, "no_evidence": 0}}
    gone = speakers.diarize_words(tmp_path / "c.opus", segs, emb, {}, **kw, rescorer=speakers.TurnScorer({}),
                                  boundaries=speakers.Boundaries(**short, blip_absorb=True, blip_margin=0.8))
    assert voices_in_order(gone.turns) == ["Speaker A", "Speaker B"]
    assert gone.boundaries == {"blips": {"absorbed": 1, "kept": 0, "no_evidence": 0}}


def test_on_the_default_path_a_short_reply_from_another_voice_is_kept(tmp_path):
    """Shipped settings (process.boundary_settings({}), no override): a 1.2 s "yes" whose audio
    clearly favours another voice of the conversation keeps its own line."""
    spec = [(A, 12.0), (B, 1.2), (A, 12.0), (B, 25.0)]   # both voices past the 20 s cluster floor
    segs, switches, t = [], [], 0.0
    for v, dur in spec:
        ws = timeline_words(t, t + dur)
        segs.append(Seg(ws[0][0], ws[-1][1], " ".join(w[2] for w in ws), ws))
        switches.append((t + dur + 0.15, v))
        t += dur + 0.3
    switches[-1] = (1e9, switches[-1][1])
    assert len(segs[1].words) == 3                                     # "yes", three words of 1.2 s
    d = speakers.diarize_words(tmp_path / "c.opus", segs, by_time(switches), {}, cluster_threshold=0.5,
                               match_threshold=0.5, rescorer=speakers.TurnScorer({}),
                               boundaries=process.boundary_settings({}))
    assert voices_in_order(d.turns) == ["Speaker A", "Speaker B", "Speaker A", "Speaker B"]
    reply = [t for t in d.turns if 12.0 < t.start < 13.6]
    assert reply and all(t.label == "Speaker B" for t in reply)


def test_min_run_s_is_the_flicker_length(tmp_path):
    segs, emb = blip_conversation()
    d = speakers.diarize_words(tmp_path / "c.opus", segs, emb, {}, cluster_threshold=0.5, match_threshold=0.5,
                               boundaries=speakers.Boundaries(min_run_s=1.0))
    assert voices_in_order(d.turns) == ["Speaker A", "Speaker B"]          # 0.7 s of speech is a flicker


def monologue(split_at=8.0, length=16.0):
    """One whisper segment: A until split_at, then B, with clustering loose enough (0.2) to hear one voice."""
    words = timeline_words(0.0, length)
    return one_segment(words), by_time([(split_at, A), (1e9, B)])


def test_a_change_point_inside_a_two_speaker_monologue_is_found(tmp_path):
    segs, emb = monologue()
    kw = dict(cluster_threshold=0.2, match_threshold=0.5)
    one = speakers.diarize_words(tmp_path / "c.opus", segs, emb, {}, **kw)
    assert {t.label for t in one.turns} == {"Speaker A"}                     # clustering heard one voice
    d = speakers.diarize_words(tmp_path / "c.opus", segs, emb, {}, **kw,
                               boundaries=speakers.Boundaries(change_points=True, cp_threshold=0.5))
    assert [t.label for t in d.turns] == ["Speaker A", "Speaker B"]
    assert d.turns[1].start == pytest.approx(8.0, abs=0.8)                   # split near the change
    assert d.boundaries["change_points"] == {"splits": 1, "to_voice": 0, "new_voices": 1}
    by = {c.tag: c for c in d.clusters}
    assert by["Speaker A"].embedding @ A > 0.95 and by["Speaker B"].embedding @ B > 0.95


def test_a_split_off_piece_rejoins_the_voice_it_sounds_like():
    words = timeline_words(0.0, 30.0)
    windows, _v = speakers.voice_windows(words)
    # 0-10 s A and 10-20 s B, which clustering gave one voice; 20-30 s is voice 1, which is B
    x = np.stack([A if (s + e) / 2 < 10 else B for s, e in windows])
    labels = [0 if w[0] < 20 else 1 for w in words]
    cents = np.stack([speakers.normalize(1.2 * A + B), B])     # voice 0: mostly A
    out, cents2, names, info = speakers.change_points(words, labels, windows, x, cents, [None, None],
                                                      speakers.Boundaries(change_points=True, cp_threshold=0.5))
    assert info == {"splits": 1, "to_voice": 1, "new_voices": 0} and names == [None, None]
    assert {lab for w, lab in zip(words, out) if w[0] < 9.5} == {0}
    assert {lab for w, lab in zip(words, out) if w[0] > 10.5} == {1}
    assert cents2[0] @ A > 0.95                       # voice 0 keeps only what is left of it


def test_one_voice_speaking_alike_throughout_is_not_split(tmp_path):
    segs, _emb = monologue()
    d = speakers.diarize_words(tmp_path / "c.opus", segs, lambda c, spans: np.tile(A, (len(spans), 1)), {},
                               cluster_threshold=0.2, match_threshold=0.5,
                               boundaries=speakers.Boundaries(change_points=True, cp_threshold=0.5))
    assert {t.label for t in d.turns} == {"Speaker A"} and d.boundaries["change_points"]["splits"] == 0


def test_a_failing_pass_keeps_the_turns_it_was_given(tmp_path, monkeypatch):
    segs, emb = monologue()
    monkeypatch.setattr(speakers, "change_points", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(speakers, "absorb_blips", lambda *a, **k: 1 / 0)
    for rescorer in (None, speakers.TurnScorer({})):
        d = speakers.diarize_words(tmp_path / "c.opus", segs, emb, {}, cluster_threshold=0.2, match_threshold=0.5,
                                   rescorer=rescorer,
                                   boundaries=speakers.Boundaries(change_points=True, blip_absorb=True))
        assert {t.label for t in d.turns} == {"Speaker A"}
        assert set(d.boundaries) == {"change_points", "blips"}
        assert all("ZeroDivisionError" in r["error"] for r in d.boundaries.values())


# ---------------------------------------------------------------------------
# config switches through the processor

def test_boundary_settings_defaults_and_bad_values():
    # shipped: blind flicker length 1.0 s, blip and short-run (< 1.5 s) absorption on evidence; change points off
    b = process.boundary_settings({})
    assert (b.blip_absorb, b.blip_margin, b.min_run_s, b.short_run_s, b.change_points) == (True, 0.15, 1.0, 1.5, False)
    # a malformed value never raises: numbers fall back to their default, a switch is on only when exactly true
    b = process.boundary_settings({"blip_absorb": "yes", "change_points": 1, "min_run_s": "x",
                                   "cp_threshold": float("nan"), "blip_words": -3, "cp_same": 0.6})
    assert (b.blip_absorb, b.change_points, b.min_run_s, b.cp_threshold, b.blip_words, b.cp_same) == \
        (False, False, 1.0, 0.3, 3, 0.6)
    assert process.boundary_settings({"change_points": True}).change_points


def test_the_processor_splits_a_monologue_when_change_points_are_on(proc, fake_whisper, fake_embedder):
    segs, emb = monologue(length=16.0)
    (proc.DATA / "config.json").write_text(json.dumps({"speakers": {
        "cluster_threshold": 0.2, "change_points": True, "cp_threshold": 0.5}}))
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 60))
    fake_whisper.segs = segs
    fake_embedder.fn = emb
    con, n = run(proc)
    assert n == 1
    body = section_body(daily_for(proc).read_text())
    assert [p.split("**")[1] for p in body.split("\n\n")] == ["Speaker A:", "Speaker B:"]
    assert con.execute("SELECT COUNT(*) FROM speaker_clusters").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# audio cleanup: the voice model only

def test_denoise_returns_a_new_array_and_gates_steady_noise():
    rng = np.random.default_rng(1)
    sr = speakers.SAMPLE_RATE
    t = np.arange(10 * sr) / sr
    tone = (0.3 * np.sin(2 * np.pi * 300 * t) * (t % 2 < 1)).astype(np.float32)   # 1 s on, 1 s off
    noisy = tone + 0.03 * rng.normal(size=t.shape).astype(np.float32)
    before = noisy.copy()
    clean = speakers.denoise(noisy)
    assert clean is not noisy and np.array_equal(noisy, before)            # input untouched
    off = (t % 2 >= 1.1) & (t % 2 < 1.9)
    assert np.std(clean[off]) < 0.5 * np.std(noisy[off])                    # noise between bursts drops
    on = (t % 2 > 0.1) & (t % 2 < 0.9)
    assert np.corrcoef(clean[on], tone[on])[0, 1] > 0.95                   # the voice stays
    part = speakers.denoise(noisy, spans=[(2.0, 3.0)])
    assert np.all(part[: int(1.5 * sr)] == 0) and np.std(part[int(2.2 * sr):int(2.8 * sr)]) > 0.1


def test_the_noise_reduction_switch_leaves_whisper_input_untouched(proc, fake_whisper, fake_embedder, monkeypatch):
    """With speakers.denoise on, Whisper still gets the clip as written (same file, same bytes,
    before the speaker step), and the embedder is handed a cleaned copy of its audio."""
    seen = {}
    segs, _emb = monologue(length=8.0)

    def whisper(path):
        seen["whisper"] = (str(path), hashlib.sha256(path.read_bytes()).hexdigest())
        return segs
    fake_whisper.fn = whisper
    raw = (np.random.default_rng(2).normal(size=20 * speakers.SAMPLE_RATE) * 0.05).astype(np.float32)
    monkeypatch.setattr(speakers, "load_audio", lambda clip: raw.copy())

    class Ecapa:
        def embed_audio(self, audio, spans):
            seen["audio"] = audio
            return np.tile(A, (len(spans), 1))
    monkeypatch.setattr(speakers, "get_embedder", lambda data_dir: Ecapa())
    (proc.DATA / "config.json").write_text(json.dumps({"speakers": {"denoise": True}}))
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 60))
    con, n = run(proc)
    assert n == 1
    clip = proc.CLIPS / next(p.name for p in proc.CLIPS.iterdir())
    assert seen["whisper"] == (str(clip), hashlib.sha256(clip.read_bytes()).hexdigest())
    assert seen["audio"] is not raw and not np.array_equal(seen["audio"], raw)   # the model heard the cleaned copy
    assert "w0" in daily_for(proc).read_text()                                   # the words are Whisper's


# ---------------------------------------------------------------------------
# eval turn mode

def test_paragraph_stats_counts_blips_and_long_paragraphs():
    paras = [("A", 20), ("B", 2), ("A", 30), ("B", 3), ("A", 5), ("B", 4), ("A", 160), (None, 1)]
    st = eval_speakers.paragraph_stats(paras)
    assert st == {"lines": 8, "voices": 2, "blips": 2, "long": 1, "long_multi": 1}
    assert eval_speakers.paragraph_stats([("A", 200)]) == {"lines": 1, "voices": 1, "blips": 0, "long": 1,
                                                           "long_multi": 0}


def test_turn_mode_reports_written_and_replayed_turns_side_by_side(tmp_path, monkeypatch):
    import io

    from synth_audio import write_tone
    data, notes, cache = tmp_path / "data", tmp_path / "notes", tmp_path / "cache"
    for p in (data, notes, cache):
        p.mkdir()
    monkeypatch.setattr(process, "DB", data / "pages.db")
    (data / "config.json").write_text(json.dumps({"speakers": {"cluster_threshold": 0.2}}))
    clip = data / "clips" / "c.opus"
    clip.parent.mkdir()
    write_tone(clip, seconds=1.0)
    segs, emb = monologue()
    (cache / "c.json").write_text(json.dumps({"low_conf_share": 0, "segs": [[s[0], s[1], s[2], [list(w) for w in s.words]]
                                                                          for s in segs]}))
    heading = "10:00 — Chat (0m16s) · new"
    (notes / "2026-11-08.md").write_text(f"# Day\n\n## {heading}\n\n[10:00] **Speaker A:** {segs[0][2]}\n\n---\n")
    con = process.db()
    con.execute("INSERT INTO conversations (device, start_ms, end_ms, day, raw_heading, clip) VALUES ('DEVA', 7, 16007, "
                "'2026-11-08', ?, ?)", (heading, str(clip)))
    con.commit()
    con.close()
    buf = io.StringIO()
    res = eval_speakers.turn_eval(data, notes, "2026-11-08", cache, out=buf, embedder=emb, voices=True,
                                  overrides={"change_points": True, "cp_threshold": 0.5})
    text = buf.getvalue()
    a, b = res["runs"].values()
    # one unnamed voice is never shown (no tags); the split makes two
    assert a[("DEVA", 7)]["stats"]["voices"] == 0 and b[("DEVA", 7)]["stats"]["voices"] == 2
    assert res["total"]["written"]["lines"] == 1
    assert "with change_points=True, cp_threshold=0.5" in text and "change_points splits 1" in text
    assert "w0" not in text and "w1" not in text                     # counts and tags only
