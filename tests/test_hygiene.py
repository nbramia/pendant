"""Voiceprint hygiene: nobody is named automatically after a thinly sampled person
(speakers.min_samples), short voices don't become samples (speakers.min_sample_speech_s, and
`sample_hygiene` for samples stored before), and Voice Scoring asks about thin voiceprints first.
Fake embeddings, fixture people."""
from __future__ import annotations

import io
import json

import numpy as np
import pytest
from test_eval_speakers import fill
from test_process import fake_embedder, fake_whisper, proc, run  # noqa: F401
from test_turn_rescore import KW, conversation, labels, write_two_voices
from test_voices import ALEX, NOAH, env, unit  # noqa: F401

from pendant_direct import calibration, eval_speakers, feed, sample_hygiene, speakers, voices

E = np.eye(4, dtype=np.float32)


def u(*v):
    return speakers.normalize(np.array(v, dtype=np.float32))


def set_speakers(data, **kw):
    path = data / "config.json"
    cfg = json.loads(path.read_text()) if path.exists() else {}
    cfg.setdefault("speakers", {}).update(kw)
    path.write_text(json.dumps(cfg))


# ---------------------------------------------------------------------------
# speakers.min_samples: naming and rescoring

def test_a_thin_person_is_never_named_but_still_counts_against_the_margin():
    prints = {"Noah": (E[0], 5), "Riley": (E[1], 1)}
    riley = np.stack([u(0.1, 1, 0, 0)])
    assert speakers.match(riley, prints, 0.55) == ["Riley"]
    assert speakers.match(riley, prints, 0.55, min_samples=2) == [None]          # nobody else takes it
    # Noah only just beats thin Riley: still not clear enough to be named Noah
    close = np.stack([u(1, 0.97, 0, 0)])
    assert speakers.match(close, prints, 0.55, margin=0.05) == [None]
    assert speakers.match(close, prints, 0.55, margin=0.05, min_samples=2) == [None]
    assert speakers.match(np.stack([u(1, 0.1, 0, 0)]), prints, 0.55, min_samples=2) == ["Noah"]


def test_the_calibrated_scorer_counts_samples_the_same_way():
    samples = {"Noah": (np.stack([E[0]] * 3), [1, 2, 3]), "Riley": (E[1][None, :], [4])}
    cal = calibration.Calibration(samples, calibration.Cohort.empty(4), mode="topk", raw_threshold=0.55)
    riley = np.stack([u(0.1, 1, 0, 0)])
    assert speakers.match(riley, {}, 0.55, calibration=cal) == ["Riley"]
    assert speakers.match(riley, {}, 0.55, calibration=cal, min_samples=2) == [None]
    assert speakers.sample_count("Riley", {"Riley": (E[1], 7)}, cal) == 7        # the voiceprint's own count wins


def test_a_turn_never_moves_to_a_thin_person(tmp_path):
    a, b = E[0], u(0.3, 0.954, 0, 0)
    prints = {"Noah": (a, 5), "Alex": (b, 1)}
    segs, emb = conversation([(a, 4), (b, 4), (a, 4), (b, 4), (a, 4)])

    def turns(n):
        return labels(speakers.diarize_words(tmp_path / "c.opus", segs, emb, prints, **KW,
                                             rescorer=speakers.TurnScorer(prints, min_samples=n)).turns)
    assert turns(0) == ["Noah", "Alex", "Noah", "Alex", "Noah"]
    assert turns(2) == ["Noah"] * 5


@pytest.mark.parametrize("value", [0, 4])
def test_the_processor_hands_min_samples_to_naming_and_rescoring(proc, fake_whisper, fake_embedder, monkeypatch,
                                                                 value):
    write_two_voices(proc, fake_whisper, fake_embedder, [(E[0], 4), (u(0.3, 0.954, 0, 0), 4), (E[0], 4)])
    set_speakers(proc.DATA, min_samples=value)
    seen_match, seen_turns = [], []
    real_match, real_rescore = speakers.match, speakers.rescore_turns
    monkeypatch.setattr(speakers, "match", lambda *a, **k: seen_match.append(k.get("min_samples")) or real_match(*a, **k))
    monkeypatch.setattr(speakers, "rescore_turns",
                        lambda *a, **k: seen_turns.append(a[7].min_samples) or real_rescore(*a, **k))
    con, _n = run(proc)
    assert seen_match and all(s == value for s in seen_match) and seen_turns == [value]
    named = {r[0] for r in con.execute("SELECT label FROM speaker_clusters WHERE labeled_by = 'match'")}
    # two samples each: named at 0, nobody named at 4
    assert named == ({"Noah", "Alex"} if value == 0 else set())


def test_the_default_needs_two_samples():
    assert speakers.naming_settings({})["min_samples"] == 2
    assert voices.sample_ok(1.0, {})                         # short voices still teach by default


# ---------------------------------------------------------------------------
# speakers.min_sample_speech_s: a short voice is named but doesn't teach the voiceprint

def test_a_short_answered_voice_is_named_without_becoming_a_sample(env):
    set_speakers(env.data, min_sample_speech_s=20)
    short = env.add(NOAH)                                    # env voices have 10 s of speech
    long_ = env.add(NOAH, start_ms=2000)
    env.con.execute("UPDATE speaker_clusters SET speech_s = 30 WHERE id = ?", (long_,))
    env.con.commit()
    out = feed.label_cluster(short, "Noah")
    assert out["count"] == 0 and speakers.load_voiceprints(env.vdir) == {}
    row = env.con.execute("SELECT label, labeled_by, enrolled_in FROM speaker_clusters WHERE id = ?", (short,)).fetchone()
    assert row == ("Noah", "user", "")
    assert feed.label_cluster(long_, "Noah")["count"] == 1
    assert speakers.members(env.vdir, "Noah") == [long_]


def test_relabelling_a_short_voice_takes_its_old_sample_out(env):
    cid = env.add(NOAH, tag="Noah", label="Noah", labeled_by="user", enrolled_in="Noah")
    env.enroll("Noah", NOAH, cid)
    set_speakers(env.data, min_sample_speech_s=20)
    feed.label_cluster(cid, "Alex", reassign=True)
    assert speakers.load_voiceprints(env.vdir) == {}         # out of Noah's, not into Alex's
    assert env.con.execute("SELECT label, enrolled_in FROM speaker_clusters WHERE id = ?", (cid,)).fetchone() == \
        ("Alex", "")


def test_a_meeting_names_a_short_voice_without_enrolling_it(proc, monkeypatch, tmp_path):
    from pendant_direct import meetings
    day, heading = "2026-01-05", "10:00 — Sync (1m00s) · new"
    daily = proc.NOTES_DIR / f"{day}.md"
    proc.NOTES_DIR.mkdir(parents=True, exist_ok=True)
    daily.write_text(f"# Day\n\n## {heading}\n\n[10:01] **Speaker A:** reviewed the staffing plan budget numbers "
                     "tight\n\n---\n")
    con = proc.db()
    con.execute("INSERT INTO speaker_clusters (device, start_ms, tag, embedding, speech_s, labeled_by) "
                "VALUES ('DEVA', 1, 'Speaker A', ?, 5, NULL)", (np.ones(4, np.float32).tobytes(),))
    note = ("---\ndate: 2026-01-05\nsource: granola\ntitle: Noah-Kai weekly\n---\n## Transcript\n\n"
            "**Me** *(10:01:00)*: reviewed the staffing plan and the budget numbers are tight\n")
    m = meetings.parse(tmp_path / "Noah-Kai weekly.md", note)
    monkeypatch.setattr(speakers, "enroll", lambda *a, **k: pytest.fail("a short voice must not be enrolled"))
    cfg = {"speakers": {"allowed": ["Noah"], "wearer": "Noah", "min_sample_speech_s": 20}}
    assert proc.apply_meeting_names(con, "DEVA", 1, day, heading, m, cfg) == {"Speaker A": "Noah"}
    assert con.execute("SELECT label, enrolled_in FROM speaker_clusters").fetchone() == ("Noah", "")


# ---------------------------------------------------------------------------
# Voice Scoring and Who's this: thin voiceprints first

def test_priority_puts_thin_voiceprints_after_wearer_suggestions_and_before_confidence():
    cards = [{"id": 1, "confidence": 0.1}, {"id": 2, "confidence": 0.9, "thin": True},
             {"id": 3, "confidence": 0.5, "suggested": "Noah"}, {"id": 4, "confidence": 0.0, "noise_like": True,
                                                                 "thin": True}]
    assert [c["id"] for c in sorted(cards, key=voices.priority)] == [3, 2, 1, 4]


def test_voices_whose_likely_person_is_thin_are_asked_first_within_the_budget(env):
    set_speakers(env.data, daily_budget=2, min_samples=2)
    for _ in range(3):
        env.enroll("Noah", NOAH)
    env.enroll("Alex", ALEX)                                 # one sample: thin
    noah_like = [env.add(u(1, 0.05 * k, 0, 0)) for k in range(3)]
    alex_like = env.add(u(0.05, 1, 0, 0))
    sp = feed.unknown_payload()["speakers"]
    assert [s["id"] for s in sp] == [alex_like, noah_like[2]]   # Alex first; the budget of 2 holds
    assert "thin" not in sp[0]                                 # the card shape is unchanged
    set_speakers(env.data, daily_budget=8, min_samples=0)
    env.con.execute("DELETE FROM voice_offers")
    env.con.commit()
    order = [s["id"] for s in feed.unknown_payload()["speakers"]]
    assert order.index(alex_like) > order.index(noah_like[2])   # off: least confident first


# ---------------------------------------------------------------------------
# sample_hygiene: taking short samples stored before the rule out, explicitly

def hygiene_world(env):
    short = env.add(NOAH, tag="Noah", label="Noah", labeled_by="user", enrolled_in="Noah")      # 10 s
    long_ = env.add(NOAH, tag="Noah", label="Noah", labeled_by="user", enrolled_in="Noah", start_ms=2000)
    env.con.execute("UPDATE speaker_clusters SET speech_s = 30 WHERE id = ?", (long_,))
    env.con.commit()
    env.enroll("Noah", NOAH, short)
    env.enroll("Noah", NOAH, long_)
    env.enroll("Noah", NOAH, speakers.sample_key("para:DEV:1:0"))   # a single line: never touched
    env.enroll("Noah", NOAH)                                         # legacy: never touched
    set_speakers(env.data, min_sample_speech_s=20)
    return short, long_


def test_sample_hygiene_dry_run_changes_nothing(env):
    short, _long = hygiene_world(env)
    before = sorted(p.read_bytes() for p in env.vdir.iterdir() if p.is_file())
    out = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(out):
        assert sample_hygiene.main(["--data", str(env.data)]) == 0
    assert "Noah: 4 samples, 1 from voices under 20 s -> 3" in out.getvalue() and "dry run" in out.getvalue()
    assert sorted(p.read_bytes() for p in env.vdir.iterdir() if p.is_file()) == before
    assert not (env.data / "backups").exists()


def test_sample_hygiene_apply_takes_short_voices_out_and_restore_puts_them_back(env):
    short, long_ = hygiene_world(env)
    p = sample_hygiene.plan(env.data)
    assert p["people"]["Noah"]["short"] == [short]
    backup = sample_hygiene.apply(env.data, p)
    vp = speakers._read(env.vdir, "Noah")
    assert short not in vp.ids and long_ in vp.ids and vp.count == 3 and vp.legacy == 1
    enrolled = lambda: dict(env.con.execute("SELECT id, enrolled_in FROM speaker_clusters").fetchall())  # noqa: E731
    assert enrolled()[short] == "" and enrolled()[long_] == "Noah"
    assert sample_hygiene.restore(env.data, backup) == 1
    assert short in speakers._read(env.vdir, "Noah").ids and enrolled()[short] == "Noah"


# ---------------------------------------------------------------------------
# the offline eval mirrors both settings

def test_the_eval_mirrors_both_settings(env):
    fill(env, np.random.default_rng(5))
    run_ = lambda **kw: eval_speakers.context_eval(env.data, io.StringIO(), scales=(1.0,), people={},  # noqa: E731
                                                   overrides={"split_voice_cosine": None, **kw})[0]
    base = run_(min_samples=0, min_sample_speech_s=0)
    assert (base["base_correct"], base["base_wrong"]) == (8, 0)
    # leave one conversation out, every Alex voice has 3 other samples and every Noah voice 4 or 5: at 4
    # only Noah is named (his 6 voices less the split half that stays unnamed)
    assert run_(min_samples=4, min_sample_speech_s=0)["base_correct"] == 5
    # every voice has 10 s of speech: at 20 s nothing is a sample, so nothing is named
    short = run_(min_samples=0, min_sample_speech_s=20)
    assert (short["base_correct"], short["base_wrong"]) == (0, 0)
