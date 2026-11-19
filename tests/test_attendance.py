import datetime as dt

import numpy as np
import pytest

from pendant_direct import attendance, process

EVENTS = {"events": [
    {"title": "Dana-Noah weekly", "start_time": "2026-11-09T13:00:00-05:00",
     "end_time": "2026-11-09T14:00:00-05:00", "attendees": ["Dana Brooks", "noah@work.example"]},
    {"title": "Team Lead", "start_time": "2026-11-09T15:30:00-05:00",
     "end_time": "2026-11-09T16:30:00-05:00", "attendees": ["jamie@x.example", "noah@work.example", "Leads"]},
    {"title": "All day", "start_time": "2026-11-09T00:00:00-05:00", "end_time": "2026-11-10T00:00:00-05:00",
     "attendees": [], "is_all_day": True},
]}
PEOPLE = {"Dana Brooks": {"canonical_name": "Dana Brooks", "display_name": "Dana Brooks", "email": "dana@x.example"},
          "jamie@x.example": {"canonical_name": "Jamie Wells", "email": "jamie@x.example"},
          "Project HQ": {"canonical_name": "Project HQ", "email": None}}


def fake_get(path, params):
    if path == "/api/calendar/search":
        return EVENTS
    p = PEOPLE.get(params["q"])
    return {"people": [p] if p else []}


def ms(h, m):
    return int(dt.datetime(2026, 11, 9, h, m, tzinfo=attendance.TZ).timestamp() * 1000)


def test_events_and_matching():
    evs = attendance.events_on(dt.date(2026, 11, 9), get=fake_get)
    assert [e.title for e in evs] == ["Dana-Noah weekly", "Team Lead"]       # all-day dropped
    assert attendance.event_for(evs, ms(13, 5), ms(13, 50)).title == "Dana-Noah weekly"
    assert attendance.event_for(evs, ms(10, 0), ms(10, 30)) is None


def test_resolve_people_requires_every_attendee_to_be_a_person():
    assert attendance.resolve_people(["Dana Brooks", "noah@work.example"], get=fake_get) == \
        ["Dana Brooks", "Noah Park"]
    assert attendance.resolve_people(["Project HQ", "noah@work.example"], get=fake_get) is None
    assert attendance.resolve_people(["jamie@x.example", "noah@work.example", "Leads"], get=fake_get) is None


@pytest.fixture
def proc(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "DATA", tmp_path)
    monkeypatch.setattr(process, "DB", tmp_path / "pages.db")
    monkeypatch.setattr(process, "NOTES_DIR", tmp_path / "notes" / "Pendant" / "Raw")
    monkeypatch.setattr(attendance, "events_on", lambda day, get=None: attendance.Event.__class__ and
                        [attendance.Event("Dana-Noah weekly", ms(13, 0), ms(14, 0),
                                          ["Dana Brooks", "noah@work.example"], with_owner=True, series="w"),
                         attendance.Event("Team Lead", ms(15, 30), ms(16, 30),
                                          ["jamie@x.example", "noah@work.example", "Leads"], with_owner=True)])
    monkeypatch.setattr(attendance, "resolve_people", lambda a, get=None: attendance.__dict__["_rp"](a))
    attendance._rp = lambda a: (None if "Leads" in a else
                                sorted({"Dana Brooks" if x == "Dana Brooks" else "Noah Park" for x in a}))
    return process


def add_conv(con, start, end, clusters, heading="13:05 — Weekly (40m00s) · new", day="2026-11-09"):
    con.execute("INSERT INTO conversations (device, start_ms, end_ms, day, raw_heading) VALUES (?,?,?,?,?)",
                ("DEVA", start, end, day, heading))
    for tag, label, speech in clusters:
        con.execute("INSERT INTO speaker_clusters (device, start_ms, tag, label, embedding, speech_s) "
                    "VALUES (?,?,?,?,?,?)", ("DEVA", start, tag, label, np.ones(4, np.float32).tobytes(), speech))
    con.commit()


def test_two_person_meeting_enrolls_the_other_voice(proc, monkeypatch):
    from pendant_direct import speakers
    got = []
    monkeypatch.setattr(speakers, "enroll", lambda vdir, name, emb, allowed, **kw: got.append((name, allowed)) or 1)
    con = proc.db()
    add_conv(con, ms(13, 5), ms(13, 45), [("Noah", "Noah", 600), ("Speaker B", None, 500), ("Speaker C", None, 10)])
    raw = proc.NOTES_DIR / "2026-11-09.md"
    raw.parent.mkdir(parents=True)
    raw.write_text("## 13:05 — Weekly (40m00s) · new\n\n[13:06] **Speaker B:** hi\n\n---\n")
    out = proc.enroll_from_calendar(con, "DEVA", ms(13, 5), ms(13, 45), "2026-11-09",
                                    "13:05 — Weekly (40m00s) · new", {}, {})
    assert out == "enrolled Dana Brooks (Dana-Noah weekly)"
    assert got == [("Dana Brooks", ["Dana Brooks"])]
    assert "**Dana Brooks:** hi" in raw.read_text()


def test_a_short_attendee_voice_is_named_without_enrolling(proc, monkeypatch):
    from pendant_direct import speakers
    monkeypatch.setattr(speakers, "enroll", lambda *a, **kw: pytest.fail("a short voice must not be enrolled"))
    con = proc.db()
    add_conv(con, ms(13, 5), ms(13, 45), [("Noah", "Noah", 600), ("Speaker B", None, 500)])
    out = proc.enroll_from_calendar(con, "DEVA", ms(13, 5), ms(13, 45), "2026-11-09",
                                    "13:05 — Weekly (40m00s) · new", {"speakers": {"min_sample_speech_s": 900}}, {})
    assert out == "enrolled Dana Brooks (Dana-Noah weekly)"
    assert con.execute("SELECT label, labeled_by, enrolled_in FROM speaker_clusters WHERE tag = 'Dana Brooks'"
                       ).fetchone() == ("Dana Brooks", "calendar", "")


def test_unknown_attendance_and_missing_wearer(proc, monkeypatch):
    from pendant_direct import speakers
    monkeypatch.setattr(speakers, "enroll", lambda *a, **kw: pytest.fail("must not enroll"))
    con = proc.db()
    add_conv(con, ms(15, 35), ms(16, 20), [("Speaker A", None, 600), ("Speaker B", None, 500)])
    out = proc.enroll_from_calendar(con, "DEVA", ms(15, 35), ms(16, 20), "2026-11-09", "15:35 — x", {}, {})
    assert out.startswith("attendance unknown")
    add_conv(con, ms(13, 10), ms(13, 40), [("Speaker A", None, 600), ("Speaker B", None, 500)])
    assert proc.enroll_from_calendar(con, "DEVA", ms(13, 10), ms(13, 40), "2026-11-09", "13:10 — y", {}, {}) is None
    assert proc.enroll_from_calendar(con, "DEVA", ms(9, 0), ms(9, 30), "2026-11-09", "09:00 — z", {}, {}) == "no event"


def test_more_voices_than_invitees_is_skipped(proc, monkeypatch):
    from pendant_direct import speakers
    monkeypatch.setattr(speakers, "enroll", lambda *a, **kw: pytest.fail("must not enroll"))
    con = proc.db()
    # a 2-person invite, but three people talked (e.g. a doctor or therapist not on the invite)
    add_conv(con, ms(13, 5), ms(13, 45), [("Noah", "Noah", 600), ("Speaker B", None, 400), ("Speaker C", None, 300)])
    out = proc.enroll_from_calendar(con, "DEVA", ms(13, 5), ms(13, 45), "2026-11-09", "13:05 — x", {}, {})
    assert out.startswith("3 voices but 2 invited")


def ev(title, h0, m0, h1, m1, attendees=("morgan@x.example", "noah@work.example")):
    return attendance.Event(title, ms(h0, m0), ms(h1, m1), list(attendees), id=title)


def test_event_for_picks_the_largest_overlap():
    a, b = ev("A", 12, 0, 12, 30), ev("B", 12, 30, 13, 0)
    assert attendance.event_for([a, b], ms(12, 5), ms(12, 35)).title == "A"
    assert attendance.event_for([a, b], ms(12, 26), ms(12, 55)).title == "B"
    assert attendance.event_for([a, b], ms(12, 20), ms(12, 55)).title == "A"     # 10 min before B: never B


def test_event_for_allows_five_minutes_early_never_more():
    morgan = ev("Morgan-Noah week-ahead prep", 12, 0, 12, 30)
    # Nov 9: chatter at 11:41-11:44 is not the noon meeting (it used to match with 20 min of slack)
    assert attendance.event_for([morgan], ms(11, 41), ms(11, 44)) is None
    assert attendance.event_for([morgan], ms(11, 56), ms(11, 59)).title == morgan.title      # gathering early
    # a long stretch of pre-meeting talk that only runs into the meeting isn't it either
    assert attendance.event_for([morgan], ms(11, 44), ms(12, 10)) is None


def test_event_for_allows_an_overrun_after_the_end():
    morgan = ev("Morgan-Noah week-ahead prep", 12, 0, 12, 30)
    assert attendance.event_for([morgan], ms(12, 10), ms(12, 40)).title == morgan.title
    assert attendance.event_for([morgan], ms(12, 40), ms(13, 6)).title == morgan.title       # the second half
    assert attendance.event_for([morgan], ms(13, 5), ms(13, 30)) is None                   # past the overrun


def test_events_without_people_rank_below_and_never_overrun():
    block = ev("Focus", 12, 0, 13, 0, attendees=())
    alone = ev("Errand", 12, 0, 12, 30, attendees=("noah@work.example",))
    meeting = ev("1:1", 12, 0, 12, 30)
    assert attendance.event_for([block, meeting], ms(12, 0), ms(12, 30)).title == "1:1"
    assert attendance.event_for([alone, block], ms(12, 0), ms(12, 45)).title == "Focus"   # more booked overlap
    assert attendance.event_for([alone], ms(12, 35), ms(12, 50)) is None                  # no overrun for a block
    assert attendance.event_for([block], ms(12, 30), ms(12, 50)).title == "Focus"


def test_events_on_keeps_the_event_id():
    def get(path, params):
        return {"events": [{"event_id": "e1", "title": "X", "start_time": "2026-11-09T12:00:00-05:00",
                            "end_time": "2026-11-09T12:30:00-05:00", "attendees": []}]}
    assert attendance.events_on(dt.date(2026, 11, 9), get=get)[0].id == "e1"


CRM = {"morgan@work.example": [{"entity_id": "e-morgan", "name": "Morgan Hale", "email": "morgan@work.example",
                                  "emails": ["morgan@work.example"], "display": "Morgan Hale", "label": "Morgan Hale"}],
       "noah@work.example": [{"entity_id": "e-me", "name": "Noah Park", "email": "noah@home.example",
                                         "emails": ["noah@home.example", "noah@work.example"],
                                         "display": "Noah Park", "label": "Noah Park"}],
       "Dana Brooks": [{"entity_id": "e-dana", "name": "Dana Brooks", "email": "dana@x.example", "emails": ["dana@x.example"],
                      "display": "Dana Brooks", "label": "Dana Brooks"},
                     {"entity_id": "e-dana2", "name": "Danielle Brookfield", "email": None, "emails": [],
                      "display": None, "label": "Danielle Brookfield"}]}


def test_resolve_attendees_to_lifeos_people():
    got = attendance.resolve_attendees(["morgan@work.example", "noah@work.example", "Dana Brooks",
                                        "brandon@vendor.example", "Leads"], lambda q: CRM.get(q, []), me="e-me")
    assert got == [
        {"name": "Morgan Hale", "email": "morgan@work.example", "entity_id": "e-morgan", "wearer": False},
        {"name": "Noah Park", "email": "noah@work.example", "entity_id": "e-me", "wearer": True},
        {"name": "Dana Brooks", "email": "dana@x.example", "entity_id": "e-dana", "wearer": False},
        {"name": "brandon@vendor.example", "email": "brandon@vendor.example", "entity_id": None, "wearer": False},
        {"name": "Leads", "email": None, "entity_id": None, "wearer": False}]


def test_resolve_attendees_raises_when_lifeos_is_down():
    def down(q):
        raise OSError("refused")
    with pytest.raises(OSError):
        attendance.resolve_attendees(["morgan@work.example"], down)


def test_a_short_event_never_matches_a_conversation_that_started_long_before():
    standup = ev("Standup", 10, 0, 10, 10)
    # the conversation covers the whole standup, but began 20 minutes before it
    assert attendance.event_for([standup], ms(9, 40), ms(10, 10)) is None
    assert attendance.event_for([standup], ms(9, 56), ms(10, 10)).title == "Standup"


def test_silent_enrollment_needs_the_booked_window(proc, monkeypatch):
    """A 10:50 hallway chat after a 10:00-10:30 1:1: shown and suggested, never enrolled."""
    from pendant_direct import calendar_hint, speakers
    monkeypatch.setattr(speakers, "enroll", lambda *a, **kw: pytest.fail("must not enroll"))
    one = attendance.Event("Dana-Noah 1:1", ms(10, 0), ms(10, 30), ["Dana Brooks", "noah@work.example"])
    monkeypatch.setattr(attendance, "events_on", lambda day, get=None: [one])
    con = proc.db()
    add_conv(con, ms(10, 50), ms(11, 5), [("Noah", "Noah", 300), ("Speaker B", None, 200)])
    assert proc.enroll_from_calendar(con, "DEVA", ms(10, 50), ms(11, 5), "2026-11-09", "10:50 — x", {}, {}) == "no event"
    # the looser match still names the event for display and offers Dana as a suggestion
    assert attendance.event_for([one], ms(10, 50), ms(11, 5)) is one
    raw = '{"title": "Dana-Noah 1:1", "attendees": [{"name": "Dana Brooks", "entity_id": "e-dana"}, ' \
          '{"name": "Noah Park", "wearer": true}]}'
    clusters = [{"id": 1, "tag": "Noah", "label": "Noah", "labeled_by": "match"},
                {"id": 2, "tag": "Speaker B", "label": None, "labeled_by": None}]
    assert calendar_hint.hint(raw, clusters, 2, {"wearer": "Noah Park"})["likely"]["name"] == "Dana Brooks"
    assert con.execute("SELECT label FROM speaker_clusters WHERE tag='Speaker B'").fetchone() == (None,)


def test_the_wearer_is_found_by_alias_name_or_lifeos_self():
    crm = {"np@alias.example": [{"entity_id": "e-me", "name": "Noah B. Park", "email": "np@alias.example",
                               "emails": ["np@alias.example"], "display": "Noah B. Park", "label": "Noah B Park"}]}
    got = attendance.resolve_attendees(["np@alias.example", "Noah Park", "morgan@work.example"],
                                       lambda q: crm.get(q, []), me="e-me", wearer="Noah Park")
    assert [a["wearer"] for a in got] == [True, True, False]
    # without LifeOS's self id the alias is someone else; the configured name still is the wearer
    got = attendance.resolve_attendees(["np@alias.example", "Noah Park"], lambda q: crm.get(q, []),
                                       wearer="Noah Park")
    assert [a["wearer"] for a in got] == [False, True]


def test_no_voice_is_learned_from_an_event_the_wearer_wasnt_at(proc, monkeypatch):
    from pendant_direct import calendar_hint, speakers
    monkeypatch.setattr(speakers, "enroll", lambda *a, **kw: pytest.fail("must not enroll"))
    con = proc.db()
    add_conv(con, ms(13, 5), ms(13, 45), [("Noah", "Noah", 600), ("Speaker B", None, 500)])
    for ev in (attendance.Event("Dana ballet", ms(13, 0), ms(14, 0), ["Dana Brooks", "noah@work.example"],
                                with_owner=False),
               attendance.Event("Dana-Noah weekly", ms(13, 0), ms(14, 0), ["Dana Brooks", "noah@work.example"])):
        monkeypatch.setattr(attendance, "events_on", lambda day, get=None, ev=ev: [ev])
        out = proc.enroll_from_calendar(con, "DEVA", ms(13, 5), ms(13, 45), "2026-11-09", "13:05 — x", {}, {})
        assert out == f"not with them ({ev.title})"
    # a muted series
    con.execute(calendar_hint.MUTES_SQL)
    con.execute("INSERT INTO calendar_mutes VALUES ('w', 'Dana-Noah weekly', 0)")
    con.commit()
    monkeypatch.setattr(attendance, "events_on", lambda day, get=None: [attendance.Event(
        "Dana-Noah weekly", ms(13, 0), ms(14, 0), ["Dana Brooks", "noah@work.example"], id="w_1",
        with_owner=True, series="w")])
    assert proc.enroll_from_calendar(con, "DEVA", ms(13, 5), ms(13, 45), "2026-11-09", "13:05 — x", {}, {}) == \
        "not with them (Dana-Noah weekly)"


def test_events_on_reads_whether_the_wearer_was_there():
    def get(path, params):
        return {"events": [
            {"event_id": "e1_2026", "recurring_event_id": "e1", "with_owner": False, "title": "Kid class",
             "start_time": "2026-11-09T12:00:00-05:00", "end_time": "2026-11-09T12:30:00-05:00", "attendees": []},
            {"event_id": "e2", "title": "Old LifeOS", "start_time": "2026-11-09T13:00:00-05:00",
             "end_time": "2026-11-09T13:30:00-05:00", "attendees": []}]}
    a, b = attendance.events_on(dt.date(2026, 11, 9), get=get)
    assert (a.with_owner, a.series) == (False, "e1") and (b.with_owner, b.series) == (None, "e2")


def test_backfill_presence_fills_stored_matches(proc):
    import json
    con = proc.db()
    old = {"id": "e1_2026", "title": "Kid class", "start_ms": ms(12, 0), "end_ms": ms(12, 30), "attendees": []}
    con.execute("INSERT INTO conversations (device, start_ms, end_ms, day, raw_heading, calendar_event) "
                "VALUES ('DEVA', ?, ?, ?, 'x', ?)", (ms(12, 5), ms(12, 20), dt.date.today().isoformat(), json.dumps(old)))
    con.commit()
    day_events = [attendance.Event("Kid class", ms(12, 0), ms(12, 30), [], "e1_2026", False, "e1")]
    cal = proc.CalendarLookup(events_on=lambda day: day_events, con=con)
    assert proc.backfill_presence(con, cal) == 1
    ev = json.loads(con.execute("SELECT calendar_event FROM conversations").fetchone()[0])
    assert (ev["with_owner"], ev["series"], ev["title"]) == (False, "e1", "Kid class")
    assert proc.backfill_presence(con, cal) == 0                     # nothing left to fill


def test_a_day_cached_before_events_said_is_fetched_again(proc):
    import json
    import time
    con = proc.db()
    con.execute(proc.CALENDAR_SQL)
    day = dt.date(2026, 11, 9)
    con.execute("INSERT INTO calendar_days VALUES (?, ?, ?)", (day.isoformat(), time.time(), json.dumps(
        [{"title": "X", "start_ms": 1, "end_ms": 2, "attendees": [], "id": "e"}])))
    con.commit()
    fresh = [attendance.Event("X", 1, 2, [], "e", True, "e")]
    cal = proc.CalendarLookup(events_on=lambda d: fresh, con=con)
    assert cal.day_events(day)[0].with_owner is True                 # refetched, not the old cache
    stored = json.loads(con.execute("SELECT events FROM calendar_days").fetchone()[0])
    assert stored[0]["with_owner"] is True and stored[0]["series"] == "e"
