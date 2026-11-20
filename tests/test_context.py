"""Context bars (context.py): a person unexpected in the conversation's setting needs a higher raw
score to be named, in cluster naming, turn rescoring and the offline eval. Fake embeddings."""
from __future__ import annotations

import datetime as dt

from pendant_direct import attendance
import io
import json
from zoneinfo import ZoneInfo

import numpy as np
from test_turn_rescore import A, B, KW, PRINTS, conversation, labels, scorer, write_two_voices  # noqa: F401
from test_process import fake_embedder, fake_whisper, proc, run  # noqa: F401
from test_voices import env, unit  # noqa: F401

from pendant_direct import calibration, context, eval_speakers, speakers
from pendant_direct import speakers as sp

TZ = ZoneInfo("America/New_York")
ON = {**context.DEFAULTS, "enabled": True, "not_on_event": 0}
PEOPLE = {"Morgan Hale": {"category": "work", "keys": {"morgan@work.example", "e-morgan"}},
          "Alex Rivera": {"category": "family", "keys": {"alex.rivera@example.com", "e-alex"}},
          "Noah Park": {"category": "self", "keys": set()}}


def at(y, m, d, h, **kw):
    return context.Context(start=dt.datetime(y, m, d, h, tzinfo=TZ), **kw)


def ms(y, m, d, h):
    return int(dt.datetime(y, m, d, h, tzinfo=TZ).timestamp() * 1000)


# ---------------------------------------------------------------------------
# the setting and the bars (pure)

def test_work_people_need_more_outside_work():
    assert context.extra_bars(at(2026, 3, 2, 19), PEOPLE, ON) == {"morgan hale": 0.05}   # Monday evening
    assert context.extra_bars(at(2026, 3, 7, 11), PEOPLE, ON) == {"morgan hale": 0.05}   # Saturday morning
    assert context.extra_bars(at(2026, 3, 2, 11), PEOPLE, ON) == {}                      # Monday 11:00
    assert context.extra_bars(at(2026, 3, 2, 18), PEOPLE, ON) == {"morgan hale": 0.05}   # work_end is exclusive


def test_family_needs_more_in_a_meeting_they_are_not_on():
    meeting = at(2026, 3, 2, 20, meeting=True, attendees=frozenset({"morgan@work.example"}))
    assert context.extra_bars(meeting, PEOPLE, ON) == {"alex rivera": 0.03}   # a meeting is work, even at 20:00
    with_alex = at(2026, 3, 2, 11, meeting=True, attendees=frozenset({"e-alex"}))   # by entity id
    assert context.extra_bars(with_alex, PEOPLE, ON) == {}


def test_off_by_default_and_bars_never_lower():
    assert context.extra_bars(at(2026, 3, 2, 19), PEOPLE, context.DEFAULTS) == {}
    assert context.extra_bars(None, PEOPLE, ON) == {}
    assert context.extra_bars(at(2026, 3, 2, 19), PEOPLE, {**ON, "work_off": -0.2}) == {"morgan hale": 0.0}


def test_a_solo_block_is_not_a_meeting():
    wearer_only = [{"name": "Noah Park", "email": None, "entity_id": "e-noah", "wearer": True}]
    assert context.meeting_attendees(wearer_only) is None
    assert context.meeting_attendees([]) is None and context.meeting_attendees(None) is None
    stored = wearer_only + [{"name": "Morgan Hale", "email": "morgan@work.example", "entity_id": "e-morgan"}]
    assert {"e-morgan", "morgan@work.example", "morgan hale"} <= context.meeting_attendees(stored)
    assert context.meeting_attendees(["Morgan@Work.example"]) == frozenset({"morgan@work.example"})
    evening = ms(2026, 3, 2, 19)
    assert context.extra_bars(context.context_at(evening, wearer_only), PEOPLE, ON) == {"morgan hale": 0.05}
    assert context.extra_bars(context.context_at(evening, stored), PEOPLE, ON) == {"alex rivera": 0.03}


class FakeRun:
    def __init__(self, attendees, ok=True):
        self.att, self.ok = attendees, ok

    def _people(self, links):
        return {"e-morgan": {"category": "work", "email": "morgan@work.example", "name": "Morgan Hale",
                             "entity_id": "e-morgan"},
                "e-alex": {"category": "family", "email": "alex.rivera@example.com", "name": "Alex Rivera",
                           "entity_id": "e-alex"}}

    def _attendees(self, s, e):
        return self.ok, self.att


def test_bars_for_uses_lifeos_categories_and_the_calendar():
    cfg = {"people": {"Morgan Hale": "e-morgan", "Alex Rivera": "e-alex"}, "context": {"enabled": True, "not_on_event": 0}}
    names = ["Morgan Hale", "Alex Rivera", "Noah Park"]
    key, segs = ("DEV", ms(2026, 3, 2, 19)), [(0.0, 60.0, "x")]
    assert context.bars_for(cfg, names, key, segs, run=FakeRun(None)) == {"morgan hale": 0.05}
    assert context.bars_for(cfg, names, key, segs, run=FakeRun({"morgan@work.example"})) == {"alex rivera": 0.03}
    # a stored match may know an attendee only by entity id
    assert context.bars_for(cfg, names, key, segs, run=FakeRun({"morgan@work.example", "e-alex"})) == {}
    assert context.bars_for(cfg, names, key, segs, run=FakeRun(None, ok=False)) == {}     # calendar unknown
    assert context.bars_for({**cfg, "context": {}}, names, key, segs, run=FakeRun(None)) == {}


# ---------------------------------------------------------------------------
# cluster naming

def test_match_extra_bar_only_raises_the_bar():
    a, b = np.eye(4)[0], np.eye(4)[1]
    prints = {"Morgan Hale": (a, 3), "Alex Rivera": (b, 3)}
    cents = np.stack([0.58 * a + 0.81 * np.eye(4)[2]])   # cosine to Morgan 0.58
    assert sp.match(cents, prints, 0.55) == ["Morgan Hale"]
    assert sp.match(cents, prints, 0.55, extra={"morgan hale": 0.05}) == [None]
    assert sp.match(cents, prints, 0.55, extra={"morgan hale": 0.02}) == ["Morgan Hale"]
    assert sp.match(cents, prints, 0.55, extra={"alex rivera": 0.5}) == ["Morgan Hale"]


def test_a_barred_winner_leaves_the_voice_unnamed_not_the_runner_up():
    a, b, c = np.eye(4)[0], np.eye(4)[1], np.eye(4)[2]
    prints = {"Morgan Hale": (a, 3), "Alex Rivera": (b, 3)}
    cents = speakers.normalize(np.stack([0.60 * a + 0.56 * b + 0.57 * c]))
    s = (cents @ np.stack([a, b]).T)[0]
    assert s[0] > s[1]                                     # Morgan wins on score
    thr = float(min(s)) - 0.01                             # both clear the threshold
    assert sp.match(cents, prints, thr) == ["Morgan Hale"]
    assert sp.match(cents, prints, thr, extra={"morgan hale": 0.2}) == [None]   # not Alex


def test_a_bar_applies_to_the_raw_score_on_the_fallback_scale():
    a = np.eye(4, dtype=np.float32)[0]
    samples = {"Morgan Hale": (np.stack([a, a, a]), [1, 2, 3])}
    cal = calibration.Calibration(samples, calibration.Cohort.empty(4), mode="topk", raw_threshold=0.55)
    cents = np.stack([0.58 * a + 0.81 * np.eye(4, dtype=np.float32)[2]])
    assert sp.match(cents, {}, 0.55, calibration=cal) == ["Morgan Hale"]
    assert sp.match(cents, {}, 0.55, calibration=cal, extra={"morgan hale": 0.05}) == [None]


# ---------------------------------------------------------------------------
# turn rescoring and the processor

def test_a_bar_keeps_a_turn_from_moving_to_that_person(tmp_path):
    segs, emb = conversation([(A, 4), (B, 4), (A, 4), (B, 4), (A, 4)])
    run_ = lambda extra: speakers.diarize_words(tmp_path / "c.opus", segs, emb, PRINTS, **KW,  # noqa: E731
                                                 rescorer=scorer(extra=extra))
    assert labels(run_({"alex": 0.1}).turns) == ["Noah", "Alex", "Noah", "Alex", "Noah"]   # clears 0.4 + 0.1
    assert labels(run_({"alex": 0.7}).turns) == ["Noah"] * 5                              # the turns stay


def test_the_processor_hands_its_bars_to_naming_and_rescoring(proc, fake_whisper, fake_embedder, monkeypatch):
    write_two_voices(proc, fake_whisper, fake_embedder, [(A, 4), (B, 4), (A, 4)])
    monkeypatch.setattr(proc, "context_bars", lambda *a, **k: {"alex": 0.03})
    seen_match, seen_turns = [], []
    real_match, real_rescore = speakers.match, speakers.rescore_turns
    monkeypatch.setattr(speakers, "match", lambda *a, **k: seen_match.append(k.get("extra")) or real_match(*a, **k))
    monkeypatch.setattr(speakers, "rescore_turns",
                        lambda *a, **k: seen_turns.append(a[7].extra) or real_rescore(*a, **k))
    run(proc)
    assert seen_match and all(e == {"alex": 0.03} for e in seen_match)
    assert seen_turns == [{"alex": 0.03}]


def test_context_bars_are_empty_when_off_and_on_failure(proc, monkeypatch):
    assert proc.context_bars({"context": {"enabled": False}}, PRINTS, ("DEV", 1), [(0.0, 1.0, "x")]) == {}
    monkeypatch.setattr(context, "bars_for", lambda *a, **k: 1 / 0)
    assert proc.context_bars({"context": {"enabled": True}}, PRINTS, ("DEV", 1), [(0.0, 1.0, "x")]) == {}


# ---------------------------------------------------------------------------
# the offline eval

def voice(center: int, rng, noise: float = 0.1) -> np.ndarray:
    v = np.zeros(16, np.float32)
    v[center] = 1.0
    return unit(v + rng.normal(0, noise, 16).astype(np.float32))


def test_context_eval_counts_what_the_bars_change(env):
    rng = np.random.default_rng(3)
    evening, workday = ms(2026, 3, 2, 19), ms(2026, 3, 3, 11)
    for i in range(4):
        env.add(voice(0, rng), tag="Morgan", label="Morgan", labeled_by="user", start_ms=evening + i)
        env.add(voice(0, rng), tag="Morgan", label="Morgan", labeled_by="user", start_ms=workday + i)
        env.add(voice(1, rng), tag="Alex", label="Alex", labeled_by="user", start_ms=evening + 100 + i)
    cfg = json.loads((env.data / "config.json").read_text())
    cfg["speakers"]["context"] = {"work_off": 0.5}          # big enough to bind on these clean voices
    (env.data / "config.json").write_text(json.dumps(cfg))
    people = {"Morgan": {"category": "work", "keys": set()}, "Alex": {"category": "family", "keys": set()}}
    before = [tuple(r) for r in env.con.execute("SELECT * FROM speaker_clusters ORDER BY id")]
    out = io.StringIO()
    res = eval_speakers.context_eval(env.data, out, scales=(1.0, 2.0), people=people)
    one, two = res
    assert one["base_correct"] == 12 and one["base_wrong"] == 0
    assert one["conversations"] == 12 and one["work"] == 4 and one["meetings"] == 0
    assert one["correct"] == 8 and one["wrong"] == 0                     # the evening Morgan voices go unnamed
    assert one["moves"] == {"right->unnamed": 4} and one["bind"] == 4 and one["present"] == 4
    assert two["work_off"] == 1.0 and two["moves"] == {"right->unnamed": 4}
    text = out.getvalue()
    assert "these are some spoken words" not in text and "bind in 4 conversations" in text
    assert [tuple(r) for r in env.con.execute("SELECT * FROM speaker_clusters ORDER BY id")] == before


def test_anyone_not_on_the_event_needs_more_even_with_context_off():
    meeting = context.context_at(int(dt.datetime(2026, 3, 2, 20, tzinfo=attendance.TZ).timestamp() * 1000),
                                 ["morgan@work.example", "noah@work.example"])
    off = dict(context.DEFAULTS)                       # enabled False, not_on_event on
    bars = context.extra_bars(meeting, PEOPLE, off)
    assert "morgan hale" not in bars                   # on the event
    assert bars.get("alex rivera") == context.DEFAULTS["not_on_event"]
    assert "noah park" not in bars                     # never the wearer
    solo = context.context_at(int(dt.datetime(2026, 3, 2, 20, tzinfo=attendance.TZ).timestamp() * 1000), [])
    assert context.extra_bars(solo, PEOPLE, off) == {}                  # no event listing people: no bar
    assert context.extra_bars(meeting, PEOPLE, {**off, "not_on_event": 0}) == {}
