"""process.relink_meetings: every note a conversation overlaps, its calendar event, and one
meeting split into parts (the 30-minute cap) sharing the event and every note."""
import datetime as dt
import json

import pytest

from pendant_direct import attendance, process

TODAY = dt.datetime.now(process.TZ).date()


def ms(h, m, s=0):
    return int(dt.datetime.combine(TODAY, dt.time(h, m, s), process.TZ).timestamp() * 1000)


@pytest.fixture
def proc(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "DATA", tmp_path)
    monkeypatch.setattr(process, "DB", tmp_path / "pages.db")
    monkeypatch.setattr(process, "NOTES_DIR", tmp_path / "vault" / "Pendant" / "Raw")
    process.NOTES_DIR.mkdir(parents=True)
    return process


def note(folder, name, source, start, end, title="Morgan-Noah week-ahead prep"):
    d = process.vault() / folder
    d.mkdir(parents=True, exist_ok=True)
    t0 = dt.datetime.fromtimestamp(start / 1000, process.TZ)
    t1 = dt.datetime.fromtimestamp(end / 1000, process.TZ)
    who = "Me" if source == "granola" else "Speaker 1"
    (d / f"{name}.md").write_text(
        f"---\ndate: {TODAY}\nsource: {source}\ntitle: {title}\n---\n# {title}\n\n## Transcript\n\n"
        f"**{who}** *({t0:%H:%M:%S})*: Hi.\n\n**{who}** *({t1:%H:%M:%S})*: Bye.\n")


def conv(con, start, end, title, items=("Email Sam",)):
    t = dt.datetime.fromtimestamp(start / 1000, process.TZ)
    heading = f"{t:%H:%M} — {title} (10m00s) · new"
    con.execute("INSERT INTO conversations (device, start_ms, end_ms, day, raw_heading, action_items, notable) "
                "VALUES ('DEVA', ?, ?, ?, ?, ?, 0)", (start, end, TODAY.isoformat(), heading, json.dumps(list(items))))
    con.commit()
    daily = process.NOTES_DIR / f"{TODAY}.md"
    with daily.open("a") as f:
        f.write(f"## {heading}\n\n[{t:%H:%M}] **Speaker A:** words\n\n---\n\n")
    return heading


MORGAN = attendance.Event("Morgan-Noah week-ahead prep", ms(12, 0), ms(12, 30),
                        ["noah@work.example", "morgan@work.example"], id="ev-morgan")
CRM = {"morgan@work.example": [{"entity_id": "e-morgan", "name": "Morgan Hale", "email": "morgan@work.example",
                                  "emails": ["morgan@work.example"], "display": None, "label": "Morgan Hale"}]}


def lookup(events=(MORGAN,), calls=None, con=None, **kw):
    def events_on(day):
        if calls is not None:
            calls.append(day)
        return list(events)
    return process.CalendarLookup(events_on=events_on, people_search=lambda q: CRM.get(q, []), con=con, **kw)


def section(heading):
    text = (process.NOTES_DIR / f"{TODAY}.md").read_text()
    return text.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]


def row(con, start):
    con.row_factory = None
    r = con.execute("SELECT meeting_note, meeting_notes, calendar_event, action_items FROM conversations "
                    "WHERE start_ms=?", (start,)).fetchone()
    return r[0], json.loads(r[1] or "null"), json.loads(r[2]) if r[2] else r[2], json.loads(r[3])


def test_split_meeting_shares_event_and_every_note(proc):
    con = proc.db()
    early = conv(con, ms(11, 41), ms(11, 44), "Chatter")
    first = conv(con, ms(12, 10, 41), ms(12, 40, 24), "First half")
    second = conv(con, ms(12, 40, 26), ms(13, 6, 51), "Second half")
    after = conv(con, ms(13, 6, 53), ms(13, 12), "After")      # the cap cut it 2 s later: same meeting
    later = conv(con, ms(13, 13, 30), ms(13, 17), "Later")     # 90 s later: a new conversation
    # Wispr covered the first half, Granola only the second
    note("Work/Meetings", "Morgan-Noah week-ahead prep 20261109_12-00", "wispr", ms(12, 2), ms(12, 30))
    note("Work/Finance", "Morgan-Noah week-ahead prep 20261109", "granola", ms(12, 45), ms(13, 5))
    assert proc.relink_meetings(con, lookup()) == {TODAY.isoformat()}

    wispr, granola = "Morgan-Noah week-ahead prep 20261109_12-00", "Morgan-Noah week-ahead prep 20261109"
    for start in (ms(12, 10, 41), ms(12, 40, 26)):
        primary, notes, ev, items = row(con, start)
        assert ev["title"] == "Morgan-Noah week-ahead prep" and ev["id"] == "ev-morgan" and "via" not in ev
        assert ev["attendees"][1] == {"name": "Morgan Hale", "email": "morgan@work.example",
                                      "entity_id": "e-morgan", "wearer": False}
        assert {n["note"] for n in notes} == {wispr, granola}
        assert items == []                                  # the notes own the follow-ups
    assert row(con, ms(12, 10, 41))[0] == wispr and row(con, ms(12, 40, 26))[0] == granola
    # each half lists the note that recorded it, and the other as the same meeting
    assert section(first).startswith(f"\n*Also recorded in [[{wispr}]] (Wispr).*\n\n"
                                     f"*Same meeting as [[{granola}]] (Granola).*\n\n")
    assert section(second).startswith(f"\n*Also recorded in [[{granola}]] (Granola).*\n\n"
                                      f"*Same meeting as [[{wispr}]] (Wispr).*\n\n")
    for h in (first, second):
        assert "*Calendar: Morgan-Noah week-ahead prep (12:00–12:30) · Noah Park, Morgan Hale*" in section(h)
    # the part after the meeting: same meeting by continuation, but no note recorded it
    primary, notes, ev, items = row(con, ms(13, 6, 53))
    assert ev["via"] == "continuation" and {n["via"] for n in notes} == {"continuation"}
    assert primary is None and items == ["Email Sam"]
    assert f"*Same meeting as [[{wispr}]] (Wispr).*" in section(after) and "Also recorded" not in section(after)
    assert row(con, ms(13, 13, 30))[1:3] == (None, "") and row(con, ms(11, 41))[1:3] == (None, "")
    assert "Also recorded" not in section(early) and "Calendar" not in section(later)
    # idempotent: nothing changes on the next run, and the transcript lines stay put
    before = (proc.NOTES_DIR / f"{TODAY}.md").read_text()
    assert proc.relink_meetings(con, lookup()) == set()
    assert (proc.NOTES_DIR / f"{TODAY}.md").read_text() == before
    assert section(first).count("[12:10] **Speaker A:** words") == 1


def test_a_different_event_starts_a_new_meeting(proc):
    con = proc.db()
    conv(con, ms(12, 0), ms(12, 29, 30), "Morgan")
    conv(con, ms(12, 30), ms(12, 58), "Dana")
    dana = attendance.Event("Dana-Noah weekly", ms(12, 30), ms(13, 0), ["dana@x.example"], id="ev-dana")
    proc.relink_meetings(con, lookup((MORGAN, dana)))
    assert row(con, ms(12, 0))[2]["title"] == "Morgan-Noah week-ahead prep"
    assert row(con, ms(12, 30))[2]["title"] == "Dana-Noah weekly"


def test_lifeos_down_leaves_no_calendar_line_and_retries(proc):
    con = proc.db()
    h = conv(con, ms(12, 10), ms(12, 30), "Meeting")

    def down(day):
        raise OSError("refused")
    proc.relink_meetings(con, process.CalendarLookup(events_on=down))
    assert row(con, ms(12, 10))[2] is None                 # unknown, not "no event"
    assert "Calendar" not in section(h)
    proc.relink_meetings(con, lookup())
    assert row(con, ms(12, 10))[2]["title"] == "Morgan-Noah week-ahead prep"
    # down again later: the stored match stands
    assert proc.relink_meetings(con, process.CalendarLookup(events_on=down)) == set()
    assert row(con, ms(12, 10))[2]["title"] == "Morgan-Noah week-ahead prep"


def test_attendees_are_looked_up_once_per_unchanged_event(proc):
    con = proc.db()
    conv(con, ms(12, 10), ms(12, 30), "Meeting")
    proc.relink_meetings(con, lookup())
    searched = []
    cal = lookup()
    cal.people_search = lambda q: searched.append(q) or CRM.get(q, [])
    assert proc.relink_meetings(con, cal) == set() and searched == []


def test_same_named_notes_from_two_apps_both_link(proc):
    con = proc.db()
    h = conv(con, ms(14, 21), ms(14, 49), "Dana")
    note("Work/Meetings", "Dana-Noah weekly 20261109", "wispr", ms(14, 30), ms(14, 50), title="Dana")
    note("Work/Finance", "Dana-Noah weekly 20261109", "granola", ms(14, 31), ms(14, 50), title="Dana")
    proc.relink_meetings(con, lookup(()))
    links = sorted(n["note"] for n in row(con, ms(14, 21))[1])
    assert links == ["Work/Finance/Dana-Noah weekly 20261109", "Work/Meetings/Dana-Noah weekly 20261109"]
    assert section(h).count("*Also recorded in [[Work/") == 2


def test_a_real_gap_is_a_new_conversation(proc):
    con = proc.db()
    conv(con, ms(12, 10), ms(12, 29), "Meeting")
    conv(con, ms(12, 30, 9), ms(12, 34), "Hallway")       # 69 s later: matches only on its own merits
    proc.relink_meetings(con, lookup((attendance.Event("Prep", ms(12, 0), ms(12, 20),
                                                       ["morgan@work.example"], id="p"),)))
    assert row(con, ms(12, 10))[2]["title"] == "Prep"
    assert row(con, ms(12, 30, 9))[2]["title"] == "Prep" and "via" not in row(con, ms(12, 30, 9))[2]  # own overrun
    conv(con, ms(13, 40, 9), ms(13, 45), "Later")          # past the overrun, after a real gap
    proc.relink_meetings(con, lookup())
    assert row(con, ms(13, 40, 9))[2] == ""


def test_a_fresh_cache_makes_no_calendar_calls(proc):
    con = proc.db()
    conv(con, ms(12, 10), ms(12, 30), "Meeting")
    t = [1000.0]
    calls = []
    proc.relink_meetings(con, lookup(calls=calls, con=con, now=lambda: t[0]))
    assert calls == [TODAY]
    t[0] += 14 * 60                                        # today: fresh for 15 minutes
    calls.clear()
    proc.relink_meetings(con, lookup(calls=calls, con=con, now=lambda: t[0]))
    assert calls == []
    t[0] += 2 * 60                                         # stale: fetched again
    proc.relink_meetings(con, lookup(calls=calls, con=con, now=lambda: t[0]))
    assert calls == [TODAY]


def test_past_days_are_cached_for_an_hour(proc):
    con = proc.db()
    yesterday = TODAY - dt.timedelta(days=1)
    t = [1000.0]
    cal = lookup(con=con, now=lambda: t[0])
    cal.day_events(yesterday)
    calls = []
    t[0] += 50 * 60
    assert lookup(calls=calls, con=con, now=lambda: t[0]).day_events(yesterday) == [MORGAN] and calls == []
    t[0] += 11 * 60
    lookup(calls=calls, con=con, now=lambda: t[0]).day_events(yesterday)
    assert calls == [yesterday]


def test_a_timeout_or_spent_budget_falls_back_to_the_cache(proc):
    con = proc.db()
    t = [1000.0]
    lookup(con=con, now=lambda: t[0]).day_events(TODAY)
    t[0] += 3600                                           # stale

    def slow(day):
        raise TimeoutError("LifeOS did not answer")
    cal = process.CalendarLookup(events_on=slow, con=con, now=lambda: t[0])
    assert cal.day_events(TODAY) == [MORGAN]                 # the cached events stand
    clock = [0.0]
    calls = []
    spent = lookup(calls=calls, con=con, now=lambda: t[0], clock=lambda: clock[0], budget=1.0)
    spent._remaining()                                     # the run's budget starts ...
    clock[0] = 2.0                                         # ... and is spent
    assert spent.day_events(TODAY) == [MORGAN] and calls == []
    # nothing cached and LifeOS down: unknown, never "no event"
    assert process.CalendarLookup(events_on=slow, con=con).day_events(TODAY - dt.timedelta(days=2)) is None


def test_the_cap_cutting_at_a_short_pause_is_still_one_meeting(proc):
    con = proc.db()
    conv(con, ms(14, 21, 39), ms(14, 49, 42), "First")     # 28 min: the cap cut it at an 8 s pause
    conv(con, ms(14, 49, 50), ms(14, 56), "Second")
    conv(con, ms(14, 56, 30), ms(15, 0), "Third")          # 30 s after a 6-minute part: not a cap cut
    note("Work/Meetings", "Dana 1", "wispr", ms(14, 30), ms(14, 50), title="Dana")
    dana = attendance.Event("Dana-Noah weekly", ms(14, 30), ms(15, 30), ["dana@x.example"], id="m")
    proc.relink_meetings(con, lookup((dana,)))
    assert row(con, ms(14, 21, 39))[2]["title"] == "Dana-Noah weekly"       # shared from the next part
    assert [n["note"] for n in row(con, ms(14, 49, 50))[1]] == ["Dana 1"]
    assert row(con, ms(14, 56, 30))[1] is None and "via" not in row(con, ms(14, 56, 30))[2]


def test_cap_cut_parts_chain_through_a_part_that_matched_nothing(proc):
    con = proc.db()
    conv(con, ms(9, 0), ms(9, 29, 50), "One")              # overlaps the note
    conv(con, ms(9, 29, 52), ms(9, 59, 40), "Two")         # matches nothing itself
    conv(con, ms(9, 59, 42), ms(10, 25), "Three")          # matches nothing itself
    conv(con, ms(10, 27), ms(10, 40), "Four")              # 2 min later: a new conversation
    note("Work/Meetings", "Board 20261109", "granola", ms(9, 2), ms(9, 28), title="Board")
    proc.relink_meetings(con, lookup(()))
    for start in (ms(9, 29, 52), ms(9, 59, 42)):
        assert row(con, start)[1] == [{"note": "Board 20261109", "source": "granola", "via": "continuation"}]
    assert row(con, ms(10, 27))[1] is None


def test_a_name_only_link_gets_its_folder_once_a_second_note_shares_the_name(proc):
    con = proc.db()
    h = conv(con, ms(14, 21), ms(14, 49), "Dana")
    note("Work/Meetings", "Dana weekly", "wispr", ms(14, 30), ms(14, 50), title="Dana")
    proc.relink_meetings(con, lookup(()))
    assert row(con, ms(14, 21))[0] == "Dana weekly"
    note("Work/Finance", "Dana weekly", "granola", ms(14, 31), ms(14, 50), title="Dana")
    proc.relink_meetings(con, lookup(()))
    primary, notes, _ev, _items = row(con, ms(14, 21))
    assert primary == "Work/Meetings/Dana weekly"
    assert [n["note"] for n in notes] == ["Work/Meetings/Dana weekly", "Work/Finance/Dana weekly"]
    assert "[[Dana weekly]]" not in section(h)
    assert "*Also recorded in [[Work/Meetings/Dana weekly]] (Wispr).*" in section(h)
    assert "*Also recorded in [[Work/Finance/Dana weekly]] (Granola).*" in section(h)


def test_two_same_named_notes_from_one_app_both_link(proc):
    con = proc.db()
    conv(con, ms(15, 0), ms(15, 25), "Sync")
    note("Work/A", "Sync", "wispr", ms(15, 0), ms(15, 25), title="Sync")
    note("Work/B", "Sync", "wispr", ms(15, 1), ms(15, 24), title="Sync")
    proc.relink_meetings(con, lookup(()))
    assert sorted(n["note"] for n in row(con, ms(15, 0))[1]) == ["Work/A/Sync", "Work/B/Sync"]


def test_the_run_asks_lifeos_who_the_wearer_is(proc):
    con = proc.db()
    conv(con, ms(12, 10), ms(12, 30), "Meeting")
    crm = {**CRM, "np@alias.example": [{"entity_id": "e-me", "name": "Noah B. Park", "email": "np@alias.example",
                                      "emails": ["np@alias.example"], "display": None, "label": "Noah B Park"}]}
    alias = attendance.Event("1:1", ms(12, 0), ms(12, 30), ["np@alias.example", "morgan@work.example"], id="a")
    cal = process.CalendarLookup(events_on=lambda day: [alias], people_search=lambda q: crm.get(q, []),
                                 my_person_id=lambda timeout: "e-me", wearer="Noah Park")
    proc.relink_meetings(con, cal)
    assert [a["wearer"] for a in row(con, ms(12, 10))[2]["attendees"]] == [True, False]
