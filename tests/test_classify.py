"""Kind and importance: classify rules, summarizer fields, storage, feed, backfill."""
from __future__ import annotations

import json
import sqlite3

import pytest

from pendant_direct import backfill_kind, classify as cl, feed, process, summarize as sm
from pendant_direct import highlights as hl


def S(kind=None, importance=None, items=()):
    return sm.Summary(title="T", kind=kind, importance=importance, action_items=list(items))


def sig(words=200, audio_s=120, low=0.0, noah=0.5, voices=2, absent="auto"):
    if absent == "auto":   # provably absent when labelled speech says so
        absent = None if noah is None else noah <= 0.10
    return cl.Signals(words=words, audio_s=audio_s, low_conf_share=low, wearer_share=noah,
                      wearer_absent=absent, voices=voices)


def test_few_words_is_fragment():
    kind, imp, why = cl.classify(S("fragment", 2), sig(words=24))
    assert (kind, imp) == ("fragment", 0) and "24 words" in why[0]
    assert cl.classify(S("conversation", 0), sig(words=24))[0] == "fragment"
    assert cl.classify(S(), sig(words=25))[0] == "conversation"


def test_low_confidence_share_is_fragment():
    assert cl.classify(S("conversation"), sig(low=0.41))[0] == "fragment"
    assert cl.classify(S("conversation"), sig(low=0.40))[0] == "conversation"


def test_sparse_long_recording_is_fragment():
    # 30 words over 13 minutes: whisper half-hearing noise, even if the model says media
    kind, imp, why = cl.classify(S("media", 1), sig(words=30, audio_s=796, noah=0.0))
    assert (kind, imp) == ("fragment", 0) and "words/min" in why[0]
    assert cl.classify(S(), sig(words=30, audio_s=100))[0] == "conversation"   # too short to judge by rate
    assert cl.classify(S(), sig(words=247, audio_s=942))[0] == "conversation"  # 15.7 wpm: quiet but real


def test_model_fragment_needs_under_80_words():
    assert cl.classify(S("fragment"), sig(words=79))[0] == "fragment"
    k, _i, why = cl.classify(S("fragment"), sig(words=80))
    assert k == "conversation" and "but 80 words" in why[0]


def test_media_needs_noah_absent_or_under_10_percent():
    assert cl.classify(S("media"), sig(noah=0.0))[0] == "media"
    assert cl.classify(S("media"), sig(noah=0.10))[0] == "media"
    assert cl.classify(S("media"), sig(noah=0.15))[0] == "conversation"   # between: not media


def test_media_overridden_when_noah_speaks_over_25_percent():
    k, _i, why = cl.classify(S("media"), sig(noah=0.26))
    assert k == "conversation" and "overridden" in why[0]


def test_unknown_voices_never_make_media():
    assert cl.classify(S("media"), sig(noah=None, voices=0))[0] == "conversation"


def test_media_and_fragment_are_never_important():
    assert cl.classify(S("media", 2), sig(noah=0))[1] == 0
    assert cl.classify(S("fragment", 2), sig(words=30))[1] == 0


def test_model_importance_is_used_and_clamped():
    assert cl.classify(S("conversation", 2), sig())[1] == 2
    assert cl.classify(S("conversation", 1), sig())[1] == 1
    assert cl.classify(S("conversation", 7), sig())[1] == 0   # out of range: ignored, local rules


def test_local_only_fallback():
    # heuristic summary: no model opinion
    assert cl.classify(S(), sig(words=60, noah=0.5)) [:2] == ("conversation", 0)
    assert cl.classify(S(items=["Call Bob"]), sig())[1] == 1
    assert cl.classify(S(), sig(words=500, audio_s=600))[1] == 1
    kind, imp, why = cl.classify(S(), sig(words=300, audio_s=120, noah=0.0))   # 150 wpm, no Noah
    assert (kind, imp) == ("media", 0) and "local" in why[0]
    assert cl.classify(S(), sig(words=300, audio_s=120, noah=0.3))[0] == "conversation"
    assert cl.classify(S(), sig(words=300, audio_s=600, noah=0.0))[0] == "conversation"  # slow speech
    assert cl.classify(S(), sig(words=300, audio_s=120, noah=None))[0] == "conversation"


def test_thresholds_come_from_config():
    assert cl.classify(S(), sig(words=30), {"fragment_words": 40})[0] == "fragment"


def test_short_is_fragment_only_when_not_a_real_conversation():
    short = sig(words=10, audio_s=20)
    assert cl.classify(S("conversation", 2), short)[0] == "conversation"       # "The surgery is tomorrow..."
    assert cl.classify(S("conversation", 1), short)[0] == "conversation"
    assert cl.classify(S("conversation", 0, items=["Drive Sam"]), short)[0] == "conversation"
    assert cl.classify(S("conversation", 0), short)[0] == "fragment"            # importance 0, no items
    assert cl.classify(S("fragment", 2), short)[0] == "fragment"                # model agrees
    assert cl.classify(S(), short)[0] == "fragment"                             # local only
    assert cl.classify(S(items=["Call Bob"]), short)[0] == "conversation"


def test_silence_after_a_real_exchange_is_not_junk():
    quiet = sig(words=30, audio_s=796, noah=0.6, absent=False)
    assert cl.classify(S("conversation", 0), quiet)[0] == "conversation"
    # model says media or nothing, or Noah provably absent: still sparse junk
    assert cl.classify(S("media"), sig(words=30, audio_s=796, noah=0.0))[0] == "fragment"
    assert cl.classify(S(), sig(words=30, audio_s=796, noah=0.6, absent=False))[0] == "fragment"
    assert cl.classify(S("conversation", 0), sig(words=30, audio_s=796, noah=0.0))[0] == "fragment"


def test_evidence_labelled_speech():
    assert cl.evidence([("Noah", 30, None), ("Alex", 70, None)], "Noah") == (0.3, False, 2)
    assert cl.evidence([], "Noah") == (None, None, 0)
    assert cl.evidence([("Alex", 10, None)], "Noah") == (0.0, True, 1)
    assert cl.evidence([("Alex", 85, None), ("Noah", 15, None)], "Noah")[1] is True   # >= 80% others


def test_unlabelled_speech_is_unknown_not_absent():
    assert cl.evidence([(None, 100, None)], "Noah") == (0.0, False, 1)             # similarity unknown
    assert cl.evidence([(None, 100, 0.7)], "Noah")[1] is False                      # sounds like Noah
    assert cl.evidence([(None, 100, 0.2)], "Noah")[1] is True                       # provably not
    assert cl.evidence([(None, 60, 0.2), (None, 40, 0.45)], "Noah")[1] is False     # one might be him
    assert cl.evidence([("Alex", 50, None), (None, 50, None)], "Noah")[1] is False


def test_the_wearers_unlabelled_speech_never_makes_media():
    share, absent, n = cl.evidence([(None, 200, None), ("Alex", 10, None)], "Noah")
    s = cl.Signals(words=300, audio_s=300, wearer_share=share, wearer_absent=absent, voices=n)
    assert cl.classify(S("media"), s)[0] == "conversation"
    assert cl.classify(S(), cl.Signals(words=600, audio_s=200, wearer_share=share, wearer_absent=absent))[0] \
        == "conversation"
    # same recording with the voice known to be someone else's is media
    share, absent, n = cl.evidence([(None, 200, 0.1), ("Alex", 10, None)], "Noah")
    assert cl.classify(S("media"), cl.Signals(words=300, audio_s=300, wearer_share=share,
                                              wearer_absent=absent, voices=n))[0] == "media"


def test_wearer_similarities_use_the_voiceprint_scorer(tmp_path):
    import numpy as np
    from pendant_direct import speakers
    vdir = speakers.voiceprint_dir(tmp_path)
    vdir.mkdir()
    noah = np.zeros(192, dtype=np.float32); noah[0] = 1
    speakers.add(vdir, "Noah", 1, noah)
    other = np.zeros(192, dtype=np.float32); other[1] = 1
    got = process.wearer_similarities([(None, 10, noah), (None, 10, other), ("Alex", 5, other),
                                       (None, 5, None)], "Noah", tmp_path)
    assert got[0][2] > 0.9 and got[1][2] < 0.4 and got[2][2] is None and got[3][2] is None
    assert process.wearer_similarities([(None, 1, noah)], "Noah", tmp_path / "none")[0][2] is None


def test_safe_classify_falls_back_to_conversation():
    class Boom:
        kind = "media"
        importance = 2
        action_items = None
    msgs = []
    out = cl.safe_classify(Boom(), None, log=msgs.append)   # None signals: AttributeError
    assert out[:2] == ("conversation", 0) and msgs


def test_importance_two_is_notable():
    base = dict(key="D-1", audio_s=60, action_items=[], title="t", overview="o", rules={})
    assert hl.is_notable(**base) is False
    assert hl.is_notable(**base, importance=2) is True
    assert hl.is_notable(**base, importance=1) is False
    assert hl.is_notable(**base, importance=2, overrides={"D-1": False}) is False


def test_parse_summary_reads_kind_and_importance():
    s = sm.parse_summary({"title": "x", "kind": " Media ", "importance": "2"})
    assert (s.kind, s.importance) == ("media", 2)
    s = sm.parse_summary({"title": "x", "kind": "movie", "importance": 9})
    assert (s.kind, s.importance) == (None, None)
    assert sm.heuristic("some words here").kind is None


def test_fireworks_schema_asks_for_kind_and_importance(monkeypatch):
    monkeypatch.setattr(sm, "fireworks_key", lambda: "k")
    seen = {}

    def post(url, headers, body):
        seen["schema"] = body["response_format"]["json_schema"]["schema"]
        return {"choices": [{"finish_reason": "stop", "message": {"content":
                '{"title": "Dinner Plans", "overview": "o", "action_items": [], "category": "family",'
                ' "kind": "conversation", "importance": 1}'}}]}
    s = sm.summarize("text", {"jev_action_items": False}, post=post)
    assert (s.kind, s.importance) == ("conversation", 1)
    assert {"kind", "importance"} <= set(seen["schema"]["required"])


# --- storage -----------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "DATA", tmp_path)
    monkeypatch.setattr(process, "DB", tmp_path / "pages.db")
    monkeypatch.setattr(process, "NOTES_DIR", tmp_path / "vault" / "Pendant" / "Raw")
    return process


def test_migration_adds_columns_and_keeps_done_marks(db):
    con = sqlite3.connect(db.DB)
    con.execute("CREATE TABLE conversations (device TEXT, start_ms INT, end_ms INT, audio_s REAL, day TEXT, "
                "pendant TEXT, title TEXT, overview TEXT, action_items TEXT, category TEXT, people TEXT, "
                "notable INT, raw_heading TEXT, event_file TEXT, clip TEXT, meeting_note TEXT, "
                "meeting_source TEXT, PRIMARY KEY (device, start_ms))")
    con.execute("INSERT INTO conversations (device, start_ms, day, title, notable, raw_heading) "
                "VALUES ('D', 5, '2026-11-08', 'Old', 1, '10:00 — Old (1m00s)')")
    con.execute("CREATE TABLE chunks (device TEXT, page INT, ord INT, opus_sha TEXT, ts_ms INT, opus BLOB, "
                "done INT DEFAULT 0, PRIMARY KEY (device, page, ord, opus_sha))")
    con.execute("INSERT INTO chunks VALUES ('D', 1, 0, 'aa', 5, x'00', 1)")
    con.commit()
    con.close()
    con = db.db()
    assert con.execute("SELECT done FROM chunks").fetchall() == [(1,)]
    assert con.execute("SELECT title, notable, kind, importance, kind_reasons FROM conversations").fetchall() \
        == [("Old", 1, None, None, None)]
    db.db().close()   # idempotent


NOV8 = """---
date: 2026-11-08
---
# Sunday

## 10:00 — Unclear Remark About Barn End (0m20s) · old

[10:00] **Noah:** the barn end is um

---

## 11:00 — Planning The Kitchen Remodel (5m00s) · old

[11:00] **Noah:** {talk}

---
"""


@pytest.fixture
def seeded(db, tmp_path, monkeypatch):
    notes = tmp_path / "vault" / "Pendant" / "Raw"
    notes.mkdir(parents=True)
    talk = " ".join(["we should pick the cabinets and talk to the contractor about the schedule"] * 6)
    (notes / "2026-11-08.md").write_text(NOV8.format(talk=talk))
    con = db.db()
    for start, heading, title, audio in [(1, "10:00 — Unclear Remark About Barn End (0m20s) · old",
                                          "Unclear Remark About Barn End", 20),
                                         (2, "11:00 — Planning The Kitchen Remodel (5m00s) · old",
                                          "Planning The Kitchen Remodel", 300)]:
        con.execute("INSERT INTO conversations (device, start_ms, end_ms, audio_s, day, pendant, title, notable, "
                    "raw_heading, event_file) VALUES ('D', ?, ?, ?, '2026-11-08', 'old', ?, 0, ?, NULL)",
                    (start, start + 1, audio, title, heading))
        con.execute("INSERT INTO speaker_clusters (device, start_ms, tag, label, speech_s) "
                    "VALUES ('D', ?, 'Noah', 'Noah', 10)", (start,))
    con.commit()
    con.close()
    monkeypatch.setattr(sm, "fireworks_key", lambda: "k")
    return notes


def fake_post(kind, importance):
    def post(url, headers, body):
        junk = "barn end" in body["messages"][-1]["content"]
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(
            {"title": "X", "overview": "o", "action_items": [], "category": "home",
             "kind": "fragment" if junk else kind, "importance": 0 if junk else importance})}}]}
    return post


def test_backfill_classifies_and_promotes_importance_two(seeded, tmp_path):
    rows = backfill_kind.run(tmp_path, seeded, post=fake_post("conversation", 2))
    assert [(r["kind"], r["importance"]) for r in rows] == [("fragment", 0), ("conversation", 2)]
    con = sqlite3.connect(tmp_path / "pages.db")
    got = con.execute("SELECT title, kind, importance, notable, event_file FROM conversations ORDER BY start_ms").fetchall()
    assert got[0][:4] == ("Unclear Remark About Barn End", "fragment", 0, 0)
    assert got[1][:4] == ("Planning The Kitchen Remodel", "conversation", 2, 1) and got[1][4]
    assert json.loads(con.execute("SELECT kind_reasons FROM conversations WHERE start_ms=1").fetchone()[0])
    assert (tmp_path / "vault" / "Pendant" / "Events" / got[1][4]).exists()
    assert "Planning The Kitchen Remodel" in (tmp_path / "vault" / "Pendant" / "Highlights"
                                              / "2026-11-08 Highlights.md").read_text()
    # classified rows are skipped on a second run, unless --all
    assert backfill_kind.run(tmp_path, seeded, post=fake_post("media", 0)) == []
    assert len(backfill_kind.run(tmp_path, seeded, redo=True, dry_run=True, post=fake_post("media", 0))) == 2


def test_backfill_falls_back_to_local_rules_when_deepseek_fails(seeded, tmp_path):
    def boom(*a):
        raise OSError("down")
    rows = backfill_kind.run(tmp_path, seeded, vault=False, post=boom)
    assert [r["kind"] for r in rows] == ["fragment", "conversation"]


def test_backfill_table_has_no_transcript_text(seeded, tmp_path):
    out = backfill_kind.table(backfill_kind.run(tmp_path, seeded, vault=False, post=fake_post("conversation", 1)))
    assert "| 2026-11-08 10:00 | Unclear Remark About Barn End | fragment | 0 |" in out
    assert "cabinets" not in out


def test_feed_returns_kind_fields_and_defaults_for_old_rows(seeded, tmp_path, monkeypatch):
    backfill_kind.run(tmp_path, seeded, vault=False, post=fake_post("conversation", 1))
    con = sqlite3.connect(tmp_path / "pages.db")
    con.execute("UPDATE conversations SET kind=NULL, importance=NULL, kind_reasons=NULL WHERE start_ms=2")
    con.commit()
    monkeypatch.setenv("PENDANT_NOTES_DIR", str(seeded))
    monkeypatch.setenv("PENDANT_DATA", str(tmp_path))
    a, b = feed.day_payload(feed.dt.date(2026, 11, 8))["conversations"]
    assert (a["kind"], a["importance"]) == ("fragment", 0) and a["kind_reasons"]
    assert (b["kind"], b["importance"], b["kind_reasons"]) == ("conversation", 0, [])


# --- backfill promotion rules and retries ------------------------------------

def never_called(*a):
    raise AssertionError("DeepSeek must not be called again")


def events(tmp_path):
    d = tmp_path / "vault" / "Pendant" / "Events"
    return sorted(d.glob("*.md")) if d.exists() else []


def notable_of(tmp_path):
    con = sqlite3.connect(tmp_path / "pages.db")
    return con.execute("SELECT notable FROM conversations WHERE start_ms=2").fetchone()[0]


def test_backfill_promotion_respects_notable_overrides(seeded, tmp_path):
    (tmp_path / "notable_overrides.json").write_text(json.dumps({"D-2": False}))
    backfill_kind.run(tmp_path, seeded, post=fake_post("conversation", 2))
    assert notable_of(tmp_path) == 0 and events(tmp_path) == []


def test_backfill_promotion_skips_conversations_with_a_meeting_note(seeded, tmp_path):
    con = sqlite3.connect(tmp_path / "pages.db")
    con.execute("UPDATE conversations SET meeting_note='Standup', meeting_source='granola' WHERE start_ms=2")
    con.commit()
    con.close()
    backfill_kind.run(tmp_path, seeded, post=fake_post("conversation", 2))
    assert notable_of(tmp_path) == 0 and events(tmp_path) == []


def test_no_vault_leaves_notable_alone_and_a_later_run_creates_the_entries(seeded, tmp_path):
    backfill_kind.run(tmp_path, seeded, vault=False, post=fake_post("conversation", 2))
    con = sqlite3.connect(tmp_path / "pages.db")
    assert con.execute("SELECT kind, importance, notable FROM conversations WHERE start_ms=2").fetchone() \
        == ("conversation", 2, 0)
    assert events(tmp_path) == []
    # no row is unclassified now, yet the vault step still runs, without any model call
    backfill_kind.run(tmp_path, seeded, post=never_called)
    assert notable_of(tmp_path) == 1 and len(events(tmp_path)) == 1
    assert "Planning The Kitchen Remodel" in (tmp_path / "vault" / "Pendant" / "Highlights"
                                              / "2026-11-08 Highlights.md").read_text()


def test_event_write_failure_keeps_the_classification_and_retries_without_deepseek(seeded, tmp_path, monkeypatch):
    real = process.write_event
    monkeypatch.setattr(process, "write_event", lambda conv: (_ for _ in ()).throw(OSError("disk full")))
    backfill_kind.run(tmp_path, seeded, post=fake_post("conversation", 2))
    con = sqlite3.connect(tmp_path / "pages.db")
    assert con.execute("SELECT COUNT(*) FROM conversations WHERE kind IS NOT NULL").fetchone()[0] == 2
    assert notable_of(tmp_path) == 0 and events(tmp_path) == []
    monkeypatch.setattr(process, "write_event", real)
    backfill_kind.run(tmp_path, seeded, post=never_called)        # zero model calls
    assert notable_of(tmp_path) == 1 and len(events(tmp_path)) == 1
    assert "Planning The Kitchen Remodel" in (tmp_path / "vault" / "Pendant" / "Highlights"
                                              / "2026-11-08 Highlights.md").read_text()


def test_highlights_failure_is_repaired_on_rerun(seeded, tmp_path, monkeypatch):
    real = process.write_highlights
    monkeypatch.setattr(process, "write_highlights", lambda con, day: (_ for _ in ()).throw(OSError("nope")))
    backfill_kind.run(tmp_path, seeded, post=fake_post("conversation", 2))
    hlf = tmp_path / "vault" / "Pendant" / "Highlights" / "2026-11-08 Highlights.md"
    assert len(events(tmp_path)) == 1 and not hlf.exists()
    monkeypatch.setattr(process, "write_highlights", real)
    backfill_kind.run(tmp_path, seeded, post=never_called)
    assert "Planning The Kitchen Remodel" in hlf.read_text() and len(events(tmp_path)) == 1
