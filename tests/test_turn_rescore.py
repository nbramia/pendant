"""Turn rescoring (speakers.rescore_turns): after clustering and naming, each turn is
scored on its own and moves when it is clearly someone else; voices losing most of
their speech are `mixed`, never folded into a voiceprint, calendar or meeting
enrollment, nor relabeled in bulk. Plus the eval's line mode. Fake 4-d embeddings."""
from __future__ import annotations

import json

import numpy as np
import pytest
from test_process import (at, daily_for, fake_embedder, fake_whisper, pages, proc, run,  # noqa: F401
                          section_body, write_segment)
from test_enroll import world  # noqa: F401
from test_reassign_all import ba  # noqa: F401
from test_speakers import by_time, timeline_words
from test_trash import two  # noqa: F401
from test_voices import env  # noqa: F401

from pendant_direct import feed, process, speakers, voices
from pendant_direct.transcribe import Seg

A = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)               # Noah
B = speakers.normalize(np.array([0.3, 0.954, 0.0, 0.0], dtype=np.float32))   # Alex: 0.3 from Noah
MID = speakers.normalize(A + B)                                     # equally like both
PRINTS = {"Noah": (A, 5), "Alex": (B, 5)}
KW = dict(cluster_threshold=0.2, match_threshold=0.5)               # A and B cluster together


def conversation(spec, gap=0.3):
    """Whisper segments with word timings, one per (voice, seconds), and the fake embedder
    that hears each window's voice."""
    segs, switches, t = [], [], 0.0
    for v, dur in spec:
        words = timeline_words(t, t + dur)
        segs.append(Seg(words[0][0], words[-1][1], " ".join(w[2] for w in words), words))
        switches.append((t + dur + gap / 2, v))
        t += dur + gap
    switches[-1] = (1e9, switches[-1][1])
    return segs, by_time(switches)


def labels(turns):
    return [t.label for t in turns]


def scorer(**kw):
    return speakers.TurnScorer(PRINTS, **kw)


def test_two_people_in_one_voice_are_split_by_turn(tmp_path):
    segs, emb = conversation([(A, 4), (B, 4), (A, 4), (B, 4), (A, 4)])
    d = speakers.diarize_words(tmp_path / "c.opus", segs, emb, PRINTS, **KW, rescorer=scorer(), keep_before=True)
    assert labels(d.before) == ["Noah"] * 5          # the clustering heard one voice
    assert labels(d.turns) == ["Noah", "Alex", "Noah", "Alex", "Noah"]
    by = {c.tag: c for c in d.clusters}
    assert set(by) == {"Noah", "Alex"} and by["Alex"].name == "Alex"
    assert by["Alex"].embedding @ B > 0.99           # the new voice is the moved turns' audio
    assert not by["Noah"].mixed                      # kept 60% of its speech
    assert by["Alex"].speech_s == pytest.approx(7.0, abs=0.5)
    assert d.rescore["moved"] == 2 and d.rescore["moves"] == {"Noah -> Alex": 2}


def test_a_voice_keeping_under_60_percent_is_mixed(tmp_path):
    segs, emb = conversation([(A, 3.6), (B, 4.4), (A, 3.6), (B, 4.4), (A, 3.6)])
    d = speakers.diarize_words(tmp_path / "c.opus", segs, emb, PRINTS, **KW, rescorer=scorer())
    by = {c.tag: c for c in d.clusters}
    assert labels(d.turns) == ["Noah", "Alex", "Noah", "Alex", "Noah"]
    assert by["Noah"].mixed and not by["Alex"].mixed
    assert d.rescore["mixed"] == ["Noah"]


def test_a_turn_moves_to_another_unnamed_voice_of_the_conversation():
    c = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)
    d_ = speakers.normalize(np.array([0.0, 0.0, 0.3, 0.954], dtype=np.float32))
    segs, emb = conversation([(c, 4), (d_, 4), (d_, 4)])
    words = [w for s in segs for w in s.words]
    wseg = [i for i, s in enumerate(segs) for _w in s.words]
    windows, _v = speakers.voice_windows(words)
    x = speakers.normalize(emb(None, windows))
    # clustering put D's first turn in with C (voice 0); voice 1 is D alone; nobody enrolled
    wc = [0 if i < 2 else 1 for i in wseg]
    centre = np.array([(a + b) / 2 for a, b in windows])
    cents = speakers.normalize(np.stack([x[centre < 8.3].mean(0), x[centre >= 8.3].mean(0)]))
    out, cents2, names, info = speakers.rescore_turns(words, wseg, wc, windows, x, cents, [None, None],
                                                      speakers.TurnScorer({}))
    assert [out[i] for i in range(len(words)) if wseg[i] == 1] == [1] * sum(1 for i in wseg if i == 1)
    assert info["moves"] == {(0, 1): 1} and info["mixed"] == [0] and names == [None, None]
    assert len(cents2) == 2                            # no new voice: it took voice 1's tag


def test_moving_half_of_a_voice_to_another_voice_of_the_same_person_is_not_mixed():
    near = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)
    far = speakers.normalize(np.array([0.0, 0.0, 0.3, 0.954], dtype=np.float32))
    segs, emb = conversation([(near, 4), (far, 4), (far, 4)])
    words = [w for s in segs for w in s.words]
    wseg = [i for i, s in enumerate(segs) for _w in s.words]
    windows, _v = speakers.voice_windows(words)
    x = speakers.normalize(emb(None, windows))
    centre = np.array([(a + b) / 2 for a, b in windows])
    cents = speakers.normalize(np.stack([x[centre < 8.3].mean(0), x[centre >= 8.3].mean(0)]))
    wc = [0 if i < 2 else 1 for i in wseg]
    # Noah near-field and far-field, two voices both named Noah: half of the first moves over
    out, _c, _n, info = speakers.rescore_turns(words, wseg, wc, windows, x, cents, ["Noah", "Noah"],
                                               speakers.TurnScorer({}))
    assert info["moves"] == {(0, 1): 1} and info["mixed"] == []
    # the same move between two unnamed voices is a change of label: mixed
    _o, _c, _n, info = speakers.rescore_turns(words, wseg, wc, windows, x, cents, [None, None],
                                              speakers.TurnScorer({}))
    assert info["mixed"] == [0]


def test_an_ambiguous_turn_stays(tmp_path):
    segs, emb = conversation([(A, 4), (MID, 4), (A, 4)])
    d = speakers.diarize_words(tmp_path / "c.opus", segs, emb, PRINTS, **KW, rescorer=scorer())
    assert labels(d.turns) == ["Noah"] * 3 and d.rescore["moved"] == 0


def test_the_margin_is_in_the_scorers_units(tmp_path):
    segs, emb = conversation([(A, 4), (B, 4), (A, 4)])
    d = speakers.diarize_words(tmp_path / "c.opus", segs, emb, PRINTS, **KW, rescorer=scorer(margin=0.8))
    assert d.rescore["moved"] == 0                     # Alex beats Noah by 0.7 cosine, not 0.8


def test_a_named_voice_gives_turns_to_another_person_below_the_match_threshold(tmp_path):
    # far-field audio: every turn scores low, but A's turns are clearly Noah rather than Alex
    E2 = np.eye(4, dtype=np.float32)
    ta = speakers.normalize(np.array([0.42, 0.1, 0.9, 0.0], dtype=np.float32))     # Noah 0.42, Alex 0.10
    tb = speakers.normalize(np.array([0.05, 0.8, 0.6, 0.0], dtype=np.float32))     # Alex 0.80
    prints = {"Noah": (E2[0], 5), "Alex": (E2[1], 5)}
    segs, emb = conversation([(tb, 4), (ta, 4), (tb, 4), (ta, 4), (tb, 4)])
    d = speakers.diarize_words(tmp_path / "c.opus", segs, emb, prints, **KW, rescorer=speakers.TurnScorer(prints))
    assert labels(d.turns) == ["Alex", "Noah", "Alex", "Noah", "Alex"]
    strict = speakers.TurnScorer(prints, person_floor=None)           # the match threshold instead
    d = speakers.diarize_words(tmp_path / "c.opus", segs, emb, prints, **KW, rescorer=strict)
    assert labels(d.turns) == ["Alex"] * 5
    low = speakers.TurnScorer(prints, person_floor=0.5)
    assert labels(speakers.diarize_words(tmp_path / "c.opus", segs, emb, prints, **KW, rescorer=low).turns) == \
        ["Alex"] * 5


def test_short_turns_follow_their_neighbours(tmp_path):
    # B's 1.2 s line between two of B's moved turns follows them; A's short line stays with A
    segs, emb = conversation([(A, 4), (B, 4), (B, 1.2), (B, 4), (A, 4), (A, 1.2), (A, 4)])
    d = speakers.diarize_words(tmp_path / "c.opus", segs, emb, PRINTS, **KW, rescorer=scorer())
    assert labels(d.turns) == ["Noah", "Alex", "Alex", "Alex", "Noah", "Noah", "Noah"]
    assert d.rescore["inherited"] == 1 and d.rescore["moved"] == 2


def test_a_short_turn_alone_is_never_judged(tmp_path):
    segs, emb = conversation([(A, 4), (B, 1.5), (A, 4)])
    d = speakers.diarize_words(tmp_path / "c.opus", segs, emb, PRINTS, **KW, rescorer=scorer())
    assert labels(d.turns) == ["Noah"] * 3 and d.rescore["scored"] == 2


def test_rescoring_failure_keeps_cluster_level_labels(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad scores")
    monkeypatch.setattr(speakers, "rescore_turns", boom)
    segs, emb = conversation([(A, 4), (B, 4), (A, 4)])
    d = speakers.diarize_words(tmp_path / "c.opus", segs, emb, PRINTS, **KW, rescorer=scorer())
    assert labels(d.turns) == ["Noah"] * 3
    assert d.rescore["error"].startswith("RuntimeError") and not any(c.mixed for c in d.clusters)


def test_calibrated_scorer_moves_turns_on_its_own_scale(tmp_path):
    from pendant_direct import calibration
    rng = np.random.default_rng(0)
    samples = {"Noah": (speakers.normalize(np.stack([A + rng.normal(0, 0.05, 4) for _ in range(3)])), [1, 2, 3]),
               "Alex": (speakers.normalize(np.stack([B + rng.normal(0, 0.05, 4) for _ in range(3)])), [4, 5, 6])}
    cohort = calibration.Cohort(speakers.normalize(rng.normal(0, 1, (12, 4)).astype(np.float32)),
                                [("X", i) for i in range(12)], [None] * 12)
    cal = calibration.Calibration(samples, cohort, threshold=1.0, raw_threshold=0.5)
    segs, emb = conversation([(A, 4), (B, 4), (A, 4)])
    d = speakers.diarize_words(tmp_path / "c.opus", segs, emb, {}, **KW, calibration=cal,
                               rescorer=speakers.TurnScorer({}, calibration=cal, margin=5.0, margin_calibrated=0.5))
    assert labels(d.turns) == ["Noah", "Alex", "Noah"]
    m = speakers.TurnScorer({}, calibration=cal).matrix(np.stack([B]), np.stack([A]))
    assert m.calibrated[0, m.names.index("Alex")]    # the move was judged on the s-norm scale


# ---------------------------------------------------------------------------
# the processor

def write_two_voices(proc, fake_whisper, fake_embedder, spec):
    vdir = proc.DATA / "voiceprints"
    for k in (0, 2):     # two samples each: enough to name a voice (speakers.min_samples)
        speakers.enroll(vdir, "Noah", A, ["Noah"], cluster_id=901 + k)
        speakers.enroll(vdir, "Alex", B, ["Alex"], cluster_id=902 + k)
    (proc.DATA / "config.json").write_text(json.dumps({"speakers": {"allowed": ["Noah", "Alex"],
                                                                    "cluster_threshold": 0.2}}))
    segs, emb = conversation(spec)
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 60))
    fake_whisper.segs = segs
    fake_embedder.fn = emb


def test_processor_writes_rescored_turns_and_flags_mixed_voices(proc, fake_whisper, fake_embedder):
    write_two_voices(proc, fake_whisper, fake_embedder, [(A, 3.6), (B, 4.4), (A, 3.6), (B, 4.4), (A, 3.6)])
    con, n = run(proc)
    assert n == 1
    body = section_body(daily_for(proc).read_text())
    assert [p.split("**")[1] for p in body.split("\n\n")] == ["Noah:", "Alex:", "Noah:", "Alex:", "Noah:"]
    rows = con.execute("SELECT tag, label, labeled_by, mixed, enrolled_in FROM speaker_clusters ORDER BY tag").fetchall()
    assert rows == [("Alex", "Alex", "match", 0, ""), ("Noah", "Noah", "match", 1, "")]
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE done = 0").fetchone()[0] == 0


def test_processor_falls_back_to_cluster_labels_when_rescoring_fails(proc, fake_whisper, fake_embedder, monkeypatch):
    write_two_voices(proc, fake_whisper, fake_embedder, [(A, 4), (B, 4), (A, 4)])
    monkeypatch.setattr(speakers, "rescore_turns", lambda *a, **k: 1 / 0)
    con, n = run(proc)
    assert n == 1
    body = section_body(daily_for(proc).read_text())
    assert body.count("**Noah:**") == 1 and "Alex" not in body
    assert con.execute("SELECT tag, mixed FROM speaker_clusters").fetchall() == [("Noah", 0)]
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE done = 0").fetchone()[0] == 0


def test_rescoring_can_be_switched_off(proc, fake_whisper, fake_embedder):
    write_two_voices(proc, fake_whisper, fake_embedder, [(A, 4), (B, 4), (A, 4)])
    cfg = json.loads((proc.DATA / "config.json").read_text())
    cfg["speakers"]["turn_rescore"] = False
    (proc.DATA / "config.json").write_text(json.dumps(cfg))
    run(proc)
    assert "Alex" not in daily_for(proc).read_text()


def test_a_line_noah_reassigned_keeps_his_label_through_a_voice_wide_relabel(proc, fake_whisper, fake_embedder):
    write_two_voices(proc, fake_whisper, fake_embedder, [(A, 4), (B, 4), (A, 4), (B, 4), (A, 4)])
    con, _n = run(proc)
    dev, ms = con.execute("SELECT device, start_ms FROM conversations").fetchone()
    alex = con.execute("SELECT id FROM speaker_clusters WHERE tag = 'Alex'").fetchone()[0]
    voices.queue_paragraph_relabel(con, dev, ms, 3, "Riley")          # one rescored line, by hand
    voices.queue_relabel(con, alex, dev, ms, "Alex", "Someone")    # then the whole voice
    voices.apply_relabels(con, proc.NOTES_DIR)
    body = section_body(daily_for(proc).read_text())
    assert [p.split("**")[1] for p in body.split("\n\n")] == ["Noah:", "Someone:", "Noah:", "Riley:", "Noah:"]


def test_voices_that_lost_turns_get_the_centroid_of_what_they_kept(tmp_path):
    segs, emb = conversation([(A, 4), (B, 4), (A, 4), (B, 4), (A, 4)])
    d = speakers.diarize_words(tmp_path / "c.opus", segs, emb, PRINTS, **KW, rescorer=scorer(), keep_before=True)
    noah = next(c for c in d.clusters if c.tag == "Noah")
    assert noah.embedding @ A > 0.99                 # no longer the A+B mixture


# ---------------------------------------------------------------------------
# mixed voices are never a voiceprint sample nor relabeled in bulk

def test_naming_a_mixed_voice_relabels_but_never_enrolls(env):
    cid = env.add(A, tag="Speaker A")
    env.con.execute("UPDATE speaker_clusters SET mixed = 1 WHERE id = ?", (cid,))
    env.con.commit()
    out = feed.label_cluster(cid, "Noah", "user")
    assert out["count"] == 0 and speakers.load_voiceprints(env.vdir) == {}
    row = env.con.execute("SELECT label, labeled_by, enrolled_in FROM speaker_clusters WHERE id = ?", (cid,)).fetchone()
    assert row == ("Noah", "user", "")


def test_mixed_voices_are_offered_without_the_budget():
    cands = [{"id": i, "similarity": 0.9, "score": 0.9, "confidence": 0.9, "noise_like": False, "when": i}
             for i in range(3)]
    cands[2]["mixed"] = True
    out, new = voices.select(cands, {}, budget=1, new_voice_threshold=0.5)
    assert {c["id"] for c in out} == {1, 2} and (2, 0) in new    # 1 took the only budget slot


def test_bulk_relabel_skips_mixed_voices_and_a_mixed_tapped_voice_matches_nothing(ba):
    m = ba.conv(2000, "2026-11-08", [("Speaker A", "mixed words here")], [("Speaker A", [0, 1, 0, 0], {})])[0]
    ba.conv(3000, "2026-11-07", [("Speaker A", "plain words")], [("Speaker A", [0, 1, 0.05, 0], {})])
    ba.con.execute("UPDATE speaker_clusters SET mixed = 1 WHERE id = ?", (m,))
    ba.con.commit()
    out = ba.preview()
    assert (out["conversations"], out["skipped"]) == (1, 1) and m not in out["ids"]
    ba.con.execute("UPDATE speaker_clusters SET mixed = 1 WHERE id = ?", (ba.tapped,))
    ba.con.commit()
    assert ba.preview()["conversations"] == 0


def test_calendar_enrollment_skips_a_mixed_voice(tmp_path, monkeypatch):
    import test_attendance as ta
    monkeypatch.setattr(process, "DATA", tmp_path)
    monkeypatch.setattr(process, "DB", tmp_path / "pages.db")
    monkeypatch.setattr(process, "NOTES_DIR", tmp_path / "notes")
    from pendant_direct import attendance
    monkeypatch.setattr(attendance, "events_on", lambda day, get=None: [attendance.Event(
        "Dana-Noah weekly", ta.ms(13, 0), ta.ms(14, 0), ["Dana Brooks", "noah@work.example"], with_owner=True)])
    monkeypatch.setattr(attendance, "resolve_people", lambda a, get=None: ["Dana Brooks", "Noah Park"])
    monkeypatch.setattr(speakers, "enroll", lambda *a, **kw: pytest.fail("must not enroll a mixed voice"))
    con = process.db()
    ta.add_conv(con, ta.ms(13, 5), ta.ms(13, 45), [("Noah", "Noah", 600), ("Speaker B", None, 500)])
    con.execute("UPDATE speaker_clusters SET mixed = 1 WHERE tag = 'Speaker B'")
    out = process.enroll_from_calendar(con, "DEVA", ta.ms(13, 5), ta.ms(13, 45), "2026-11-09", "13:05 — x", {}, {})
    assert out == "mixed voice (Dana-Noah weekly)"


def test_meeting_names_label_a_mixed_voice_without_enrolling(proc, monkeypatch, tmp_path):
    from pendant_direct import meetings
    day, heading = "2026-01-05", "10:00 — Sync (1m00s) · new"
    proc.NOTES_DIR.mkdir(parents=True, exist_ok=True)
    (proc.NOTES_DIR / f"{day}.md").write_text(
        f"# Day\n\n## {heading}\n\n[10:01] **Speaker A:** reviewed precinct captain staffing plan budget numbers "
        "tight\n\n---\n")
    con = proc.db()
    con.execute("INSERT INTO speaker_clusters (device, start_ms, tag, embedding, mixed) VALUES ('DEVA', 1, "
                "'Speaker A', ?, 1)", (np.ones(4, dtype=np.float32).tobytes(),))
    m = meetings.parse(tmp_path / "w.md", "---\ndate: 2026-01-05\nsource: granola\ntitle: weekly\n---\n"
                       "## Transcript\n\n**Me** *(10:01:00)*: reviewed the precinct captain staffing plan and the "
                       "budget numbers are tight\n")
    speakers.add(speakers.voiceprint_dir(proc.DATA), "Noah", None, np.ones(4, np.float32))   # sounds like him
    monkeypatch.setattr(speakers, "enroll", lambda *a, **kw: pytest.fail("must not enroll a mixed voice"))
    proc.apply_meeting_names(con, "DEVA", 1, day, heading, m, {"speakers": {"allowed": ["Noah"], "wearer": "Noah"}})
    assert con.execute("SELECT tag, label, enrolled_in FROM speaker_clusters").fetchone() == ("Noah", "Noah", "")


# ---------------------------------------------------------------------------
# eval line mode

def line_eval_world(tmp_path, monkeypatch, yes_line=False):
    """A hand-labelled conversation (Speaker A = Noah, Speaker B = Alex by hand) whose
    middle line clustering gives to Noah; with yes_line a fourth, long hand-labelled line of
    which the fresh transcription reproduces only "yes". Returns (total, printed text)."""
    import io

    from synth_audio import write_tone

    from pendant_direct import eval_speakers
    data, notes, cache = tmp_path / "data", tmp_path / "notes", tmp_path / "cache"
    for p in (data, notes, cache):
        p.mkdir()
    monkeypatch.setattr(process, "DB", data / "pages.db")
    (data / "config.json").write_text(json.dumps({"speakers": {"cluster_threshold": 0.2}}))
    clip = data / "clips" / "c.opus"
    clip.parent.mkdir()
    write_tone(clip, seconds=1.0)
    for k in (0, 2):     # two samples each: enough to name a voice (speakers.min_samples)
        speakers.enroll(data / "voiceprints", "Noah", A, ["Noah"], cluster_id=901 + k)
        speakers.enroll(data / "voiceprints", "Alex", B, ["Alex"], cluster_id=902 + k)
    speakers.enroll(data / "voiceprints", "Alex", B, ["Alex"], cluster_id=1)   # this conversation's: left out
    segs, emb = conversation([(A, 4), (B, 4), (A, 4)])
    heading = "10:00 — Chat (0m13s) · new"
    lines = [f"[10:00] **{who}:** {s[2]}" for who, s in zip(["Speaker A", "Speaker B", "Speaker A"], segs)]
    if yes_line:
        segs.append(Seg(14.0, 14.35, "yes", [(14.0, 14.35, "yes")]))
        lines.append("[10:00] **Speaker A:** yes " + " ".join(f"gone{k}" for k in range(20)))
    (cache / "c.json").write_text(json.dumps({"low_conf_share": 0, "segs": [[s[0], s[1], s[2], [list(w) for w in s.words]]
                                                                          for s in segs]}))
    (notes / "2026-11-08.md").write_text(f"# Day\n\n## {heading}\n\n" + "\n\n".join(lines) + "\n\n---\n")
    con = process.db()
    con.execute("INSERT INTO conversations (device, start_ms, day, raw_heading, clip) VALUES ('DEVA', 7, "
                "'2026-11-08', ?, ?)", (heading, str(clip)))
    for tag, label in (("Speaker A", "Noah"), ("Speaker B", "Alex")):
        con.execute("INSERT INTO speaker_clusters (device, start_ms, tag, label, labeled_by, label_kind, embedding) "
                    "VALUES ('DEVA', 7, ?, ?, 'user', 'person', ?)", (tag, label, A.tobytes()))
    con.commit()
    con.close()
    buf = io.StringIO()
    total = eval_speakers.line_eval(data, notes, "2026-11-08", cache, out=buf, embedder=emb)
    assert speakers.load_voiceprints(data / "voiceprints")["Alex"][1] == 3    # read only
    return total, buf.getvalue()


def test_line_eval_scores_hand_labelled_lines_before_and_after(tmp_path, monkeypatch):
    total, text = line_eval_world(tmp_path, monkeypatch)
    assert (total["lines"], total["before"], total["after"], total["fixed"], total["broken"]) == (3, 2, 3, 1, 0)
    assert "before 2/3" in text and "after 3/3" in text and "Noah -> Alex x1" in text
    assert "spoken" not in text and "w0" not in text           # counts and tags only, no transcript words
    assert total["excluded"] == 0


def test_line_eval_leaves_out_a_line_the_transcription_barely_reproduces(tmp_path, monkeypatch):
    total, text = line_eval_world(tmp_path, monkeypatch, yes_line=True)
    # 1 of its 21 words survives: not scored before or after, and counted as excluded
    assert (total["lines"], total["before"], total["after"], total["excluded"]) == (3, 2, 3, 1)
    assert "(1 excluded)" in text and "1 hand-labelled lines excluded" in text


def test_a_line_needs_half_its_words_and_at_least_three():
    from pendant_direct.eval_speakers import evaluable
    assert not evaluable(1, 21) and not evaluable(2, 2) and not evaluable(5, 11)
    assert evaluable(3, 3) and evaluable(6, 12)



def test_a_mixed_voice_is_never_a_stranger_or_noise_exemplar(env):
    one, two = env.add(A, tag="Speaker A"), env.add(B, tag="Speaker B")
    env.con.execute("UPDATE speaker_clusters SET mixed = 1")
    env.con.commit()
    feed.stranger_cluster(one)
    feed.mark_noise(two)
    assert env.con.execute("SELECT COUNT(*) FROM strangers").fetchone()[0] == 0
    assert env.con.execute("SELECT COUNT(*) FROM noise_exemplars").fetchone()[0] == 0
    assert env.con.execute("SELECT tag, label_kind FROM speaker_clusters ORDER BY id").fetchall() == \
        [("Speaker A", "stranger"), ("Speaker B", "noise")]


# ---------------------------------------------------------------------------
# merged with main: closeness prior, enrollment readings, trash

def test_a_closeness_prior_picks_the_person_like_cluster_naming_does():
    e = np.eye(4, dtype=np.float32)
    t = np.array([0.62, 0.0, 0.60, 0.505], dtype=np.float32)           # Noah 0.62, Riley 0.60
    prints = {"Noah": (e[0], 5), "Alex": (e[1], 5), "Riley": (e[2], 5)}
    segs, emb = conversation([(e[1], 4), (t, 4), (e[1], 4)])
    words = [w for s in segs for w in s.words]
    wseg = [i for i, s in enumerate(segs) for _w in s.words]
    windows, _v = speakers.voice_windows(words)
    x = speakers.normalize(emb(None, windows))
    cents = speakers.normalize(x.mean(0, keepdims=True))

    def moved_to(prior):
        out, _c, names, _i = speakers.rescore_turns(words, wseg, [0] * len(words), windows, x, cents, ["Alex"],
                                                    speakers.TurnScorer(prints, prior=prior))
        return {names[out[i]] for i in range(len(words)) if wseg[i] == 1}
    assert moved_to(None) == {"Noah"}                 # the clearer winner
    assert moved_to({"Riley": 0.1}) == {"Riley"}        # margin above threshold + prior, as match ranks
    assert moved_to({"Noah": 0.1}) == {"Noah"}


def test_the_processor_hands_rescoring_the_naming_prior(proc, fake_whisper, fake_embedder, monkeypatch):
    write_two_voices(proc, fake_whisper, fake_embedder, [(A, 4), (B, 4), (A, 4)])
    monkeypatch.setattr(proc, "naming_prior", lambda *a, **k: {"Alex": 0.05})
    seen = []
    real = speakers.rescore_turns
    monkeypatch.setattr(speakers, "rescore_turns", lambda *a, **k: seen.append(a[7].prior) or real(*a, **k))
    run(proc)
    assert seen == [{"Alex": 0.05}]


def test_samples_of_mixed_voices_never_vouch_for_an_enrollment_reading(world):
    from test_enroll import judge, make

    from pendant_direct import enroll
    vdir = speakers.voiceprint_dir(world.data)
    e = np.eye(4, dtype=np.float32)
    for i in range(5):
        speakers.add(vdir, "Noah", 100 + i, e[0])
    for i in range(3):                                   # mixed voices that sound like the reader
        speakers.add(vdir, "Noah", 200 + i, e[2])
        world.con.execute("INSERT INTO speaker_clusters (id, device, start_ms, tag, mixed) VALUES (?, 'X', 1, 'A', 1)",
                          (200 + i,))
    world.con.commit()
    world.voice = lambda mid: e[2]                       # 0.51 to the mean of all 8, 0 without the mixed ones
    eid = make(world, "Noah")
    judge(world)
    assert enroll.status(world.con, eid)["reason"] == "not allowed"


def test_enrollment_audio_never_reaches_the_speaker_step(world, monkeypatch):
    from test_enroll import END, START, judge, make
    make(world)
    judge(world)
    seen = []
    real = world.proc.find_speakers
    monkeypatch.setattr(world.proc, "find_speakers", lambda clip, segs, cfg, *a: seen.append(a[-1]) or
                        real(clip, segs, cfg, *a))
    world.proc.transcribe_ready(world.con)
    spans = dict(world.con.execute("SELECT start_ms, end_ms FROM conversations").fetchall())
    assert len(seen) == 2 and all(spans[k[1]] < START or k[1] > END for k in seen)


def test_trash_restore_keeps_the_mixed_flag(two):
    from test_trash import A, delete, restore
    con = two.db()
    con.execute("UPDATE speaker_clusters SET mixed = 1 WHERE start_ms = ?", (A,))
    con.commit()
    con.close()
    delete(two)
    restore(two)
    con = two.db()
    assert con.execute("SELECT mixed FROM speaker_clusters WHERE start_ms = ?", (A,)).fetchall() == [(1,)]
    con.close()
