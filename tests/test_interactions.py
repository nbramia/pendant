"""GET /pendant/feed/interactions: the text-free view of who the wearer was with."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from pendant_direct import feed, process

SENTENCE = "we should absolutely renegotiate the garage contract"
DEV = "0F1E2D3C4B5A6978"
T0 = 1_762_600_000_000      # 2025-11-08 (UTC); day strings are stored, not derived
ENTITY_ALEX, ENTITY_NOAH = "person-alex-0001", "person-noah-0002"
EVENT = {"id": "evt1", "title": "Garage planning", "start_ms": T0, "end_ms": T0 + 3_600_000, "via": "continuation",
         "attendees": [{"name": "Alex Rivera", "email": "alex@example.com", "entity_id": ENTITY_ALEX, "wearer": False},
                       {"name": "Noah Park", "email": "noah@example.com", "entity_id": ENTITY_NOAH, "wearer": True}]}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"speakers": {
        "wearer": "Noah Park", "people": {"Alex Rivera": ENTITY_ALEX, "Noah Park": ENTITY_NOAH}}}))
    notes = tmp_path / "notes"
    notes.mkdir()
    monkeypatch.setenv("PENDANT_DATA", str(data))
    monkeypatch.setenv("PENDANT_NOTES_DIR", str(notes))
    monkeypatch.setattr(process, "DB", data / "pages.db")
    con = process.db()
    srv = feed.make_server(0, "tok")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield con, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    con.close()


def conv(con, start_ms=T0, day="2025-11-08", **kw):
    row = {"device": DEV, "start_ms": start_ms, "end_ms": start_ms + 600_000, "audio_s": 600, "day": day,
           "title": "Secret title", "overview": SENTENCE, "action_items": json.dumps([SENTENCE]),
           "kind": "conversation", "raw_heading": SENTENCE, **kw}
    con.execute(f"INSERT INTO conversations ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    con.commit()


def cluster(con, label, labeled_by="match", start_ms=T0, speech_s=10.0, score=None, label_kind="person", noise=0):
    con.execute("INSERT INTO speaker_clusters (device, start_ms, tag, label, labeled_by, label_kind, speech_s, "
                "match_score, noise, excerpt) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (DEV, start_ms, label or "Speaker A", label, labeled_by, label_kind, speech_s, score, noise, SENTENCE))
    con.commit()


def call(base, query="?from=2025-11-08&to=2025-11-08", token="tok"):
    req = urllib.request.Request(base + "/pendant/feed/interactions" + query,
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def status(base, query, token="tok"):
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base, query, token)
    return e.value.code


def test_requires_token(env):
    assert status(env[1], "?from=2025-11-08&to=2025-11-08", "wrong") == 401


@pytest.mark.parametrize("query", ["", "?from=2025-11-08", "?from=2025-13-01&to=2025-11-08",
                                   "?from=2025-11-08&to=nope", "?from=2025-11-09&to=2025-11-08",
                                   "?from=2025-11-01&to=2025-12-02"])
def test_bad_ranges_are_400(env, query):
    assert status(env[1], query) == 400


def test_a_31_day_range_is_allowed(env):
    out = call(env[1], "?from=2025-11-01&to=2025-12-01")
    assert out == {"from": "2025-11-01", "to": "2025-12-01", "conversations": []}


def test_row_shape_and_no_text_leaks(env):
    con, base = env
    conv(con, calendar_event=json.dumps(EVENT), meeting_notes=json.dumps(
        [{"note": "Meetings/2025-11-08 Garage", "source": "gdrive"}]))
    cluster(con, "Alex Rivera", "match", speech_s=42.04, score=0.71)
    cluster(con, "Noah Park", "user", speech_s=60)
    out = call(base)
    assert set(out) == {"from", "to", "conversations"}
    (row,) = out["conversations"]
    assert set(row) == {"device", "start_ms", "end_ms", "day", "kind", "calendar_event", "meeting_notes", "speakers"}
    assert row["device"] == DEV and row["start_ms"] == T0 and row["end_ms"] == T0 + 600_000
    assert row["meeting_notes"] == ["Meetings/2025-11-08 Garage"]
    assert row["calendar_event"] == {"id": "evt1", "start_ms": T0, "end_ms": T0 + 3_600_000, "inherited": True,
                                     "attendees": [{"entity_id": ENTITY_ALEX, "wearer": False},
                                                   {"entity_id": ENTITY_NOAH, "wearer": True}]}
    assert [set(s) for s in row["speakers"]] == [{"entity_id", "name", "labeled_by", "label_kind",
                                                  "speech_s", "score", "wearer"}] * 2
    noah, alex = row["speakers"]
    assert (noah["name"], noah["entity_id"], noah["labeled_by"], noah["wearer"], noah["score"]) == \
        ("Noah Park", ENTITY_NOAH, "user", True, None)
    assert (alex["entity_id"], alex["labeled_by"], alex["wearer"], alex["speech_s"], alex["score"]) == \
        (ENTITY_ALEX, "match", False, 42.0, 0.71)
    blob = json.dumps(out)
    for leaked in (SENTENCE, "Secret title", "Garage planning", "alex@example.com", "overview", "action_items"):
        assert leaked not in blob


def test_no_event_no_notes_no_speakers(env):
    con, base = env
    conv(con, kind=None)
    (row,) = call(base)["conversations"]
    assert row["calendar_event"] is None and row["meeting_notes"] == [] and row["speakers"] == []
    assert row["kind"] == "conversation"


def test_legacy_single_meeting_note_is_listed(env):
    con, base = env
    conv(con, meeting_note="Meetings/Old", meeting_source="gdrive")
    assert call(base)["conversations"][0]["meeting_notes"] == ["Meetings/Old"]


def test_unnamed_stranger_noise_and_dismissed_voices_are_omitted(env):
    con, base = env
    conv(con)
    cluster(con, None, None)
    cluster(con, "Alex Rivera", "dismissed")
    cluster(con, "Visitor", "user", label_kind="stranger")
    cluster(con, "Hum", "user", label_kind="noise")
    cluster(con, "Static", "match", noise=1)
    cluster(con, "Noah Park", "calendar")
    names = [s["name"] for s in call(base)["conversations"][0]["speakers"]]
    assert names == ["Noah Park"]


def test_one_person_with_several_voices_is_one_entry(env):
    con, base = env
    conv(con)
    cluster(con, "Alex Rivera", "match", speech_s=10, score=0.55)
    cluster(con, "Alex Rivera", "match", speech_s=5, score=0.8)
    cluster(con, "Alex Rivera", "calendar", speech_s=2.5)
    cluster(con, "Alex Rivera", "meeting", speech_s=1)
    (alex,) = call(base)["conversations"][0]["speakers"]
    assert alex["speech_s"] == 18.5 and alex["score"] is None and alex["labeled_by"] == "meeting"
    cluster(con, "Alex Rivera", "user", speech_s=1)
    (alex,) = call(base)["conversations"][0]["speakers"]
    assert alex["labeled_by"] == "user" and alex["score"] is None


def test_unlinked_name_has_null_entity_id(env):
    con, base = env
    conv(con)
    cluster(con, "Casey Lin", "user")
    (s,) = call(base)["conversations"][0]["speakers"]
    assert s["entity_id"] is None and s["name"] == "Casey Lin"


def test_range_selects_by_day_and_orders_by_start(env):
    con, base = env
    conv(con, start_ms=T0 + 5, day="2025-11-09")
    conv(con, start_ms=T0 + 1, day="2025-11-08")
    conv(con, start_ms=T0 - 9, day="2025-11-07")
    got = call(base, "?from=2025-11-08&to=2025-11-09")["conversations"]
    assert [c["start_ms"] for c in got] == [T0 + 1, T0 + 5]


def test_relabel_in_the_app_shows_on_the_next_call(env):
    con, base = env
    conv(con)
    cluster(con, "Alex Rivera", "match", score=0.6)
    assert call(base)["conversations"][0]["speakers"][0]["labeled_by"] == "match"
    con.execute("UPDATE speaker_clusters SET label = 'Noah Park', tag = 'Noah Park', labeled_by = 'user'")
    con.commit()
    (s,) = call(base)["conversations"][0]["speakers"]
    assert (s["name"], s["entity_id"], s["labeled_by"]) == ("Noah Park", ENTITY_NOAH, "user")


def test_a_database_before_the_score_migration_still_answers(env):
    con, base = env
    conv(con)
    cluster(con, "Alex Rivera", "match", score=0.6)
    con.execute("ALTER TABLE speaker_clusters DROP COLUMN match_score")
    con.commit()
    (s,) = call(base)["conversations"][0]["speakers"]
    assert (s["entity_id"], s["labeled_by"], s["score"]) == (ENTITY_ALEX, "match", None)


def test_an_unreadable_database_is_503_not_an_empty_range(env):
    con, base = env
    conv(con)
    con.execute("DROP TABLE speaker_clusters")
    con.commit()
    assert status(base, "?from=2025-11-08&to=2025-11-08") == 503


def test_migration_adds_match_score_and_keeps_data(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "pages.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE speaker_clusters (id INTEGER PRIMARY KEY, device TEXT, start_ms INT, tag TEXT, "
                "label TEXT, embedding BLOB, clip TEXT, start_s REAL, end_s REAL, speech_s REAL, excerpt TEXT, "
                "labeled_by TEXT, created_at REAL, voiced REAL, noise INT DEFAULT 0)")
    old.execute("INSERT INTO speaker_clusters (device, start_ms, tag, label, labeled_by) VALUES ('D', 1, 'Alex', 'Alex', 'match')")
    old.commit()
    old.close()
    con = process.db(db)
    assert con.execute("SELECT label, match_score FROM speaker_clusters").fetchall() == [("Alex", None)]


# ---------------------------------------------------------------------------
# through the real processor and trash

import numpy as np  # noqa: E402

from pendant_direct import speakers  # noqa: E402
from test_process import (at, daily_for, fake_embedder, fake_whisper, pages, proc, run,  # noqa: E402,F401
                          write_segment)
from test_trash import DEV as TDEV, delete, restore, two  # noqa: E402,F401


def test_the_processor_stores_the_match_score(proc, fake_whisper, monkeypatch):
    speakers.enroll(proc.DATA / "voiceprints", "Noah", np.array([1, 0, 0, 0], np.float32), ["Noah"])
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 3))
    con, n = run(proc)
    assert n == 1
    label, by, score = con.execute("SELECT label, labeled_by, match_score FROM speaker_clusters").fetchone()
    assert (label, by) == ("Noah", "match") and score == pytest.approx(1.0, abs=1e-3)
    monkeypatch.setenv("PENDANT_DATA", str(proc.DATA))
    out = feed.interactions_payload(feed.dt.date(2026, 1, 5), feed.dt.date(2026, 1, 5))
    (s,) = out["conversations"][0]["speakers"]
    assert s["name"] == "Noah" and s["labeled_by"] == "match" and s["score"] == pytest.approx(1.0, abs=1e-3)


def test_an_unmatched_voice_stores_no_score(proc, fake_whisper):
    write_segment(proc.SPOOL, "DEVA_20260105T150000Z.pmsg", pages("DEVA", at(0), 3))
    con, _ = run(proc)
    assert con.execute("SELECT labeled_by, match_score FROM speaker_clusters").fetchall() == [(None, None)]


def test_a_user_label_keeps_the_score_it_had(two):
    con = two.db()
    assert con.execute("SELECT DISTINCT labeled_by FROM speaker_clusters").fetchall() == [("user",)]
    assert all(r[0] is not None for r in con.execute("SELECT match_score FROM speaker_clusters"))
    con.close()


def test_deleted_conversation_is_absent_and_restored_one_returns(two):
    day = feed.dt.date(2026, 1, 5)
    starts = lambda: [c["start_ms"] for c in feed.interactions_payload(day, day)["conversations"]]  # noqa: E731
    both = starts()
    assert len(both) == 2
    delete(two)
    assert starts() == both[1:]
    restore(two)
    assert starts() == both
