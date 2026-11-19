"""calendar_hint: who a voice probably is, from the conversation's calendar event."""
import json

from pendant_direct import calendar_hint

SP = {"wearer": "Noah Park", "allowed": ["Noah Park", "Alex Rivera"],
      "people": {"Noah Park": "e-me", "Alex Rivera": "e-lex"}}


def event(*people):
    return json.dumps({"id": "e1", "title": "Morgan-Noah week-ahead prep", "start_ms": 0, "end_ms": 1,
                       "attendees": [{"name": n, "email": None, "entity_id": e, "wearer": w} for n, e, w in people]})


ME = ("Noah Park", "e-me", True)
MORGAN = ("Morgan Hale", "e-morgan", False)
DANA = ("Dana Brooks", "e-dana", False)


def clusters(*rows):
    return [{"id": i, "tag": t, "label": lab, "labeled_by": by} for i, (t, lab, by) in enumerate(rows, 1)]


def test_one_attendee_left_is_likely():
    cl = clusters(("Noah Park", "Noah Park", "match"), ("Speaker A", None, None))
    h = calendar_hint.hint(event(ME, MORGAN), cl, 2, SP)
    assert h["candidates"] == []
    lk = h["likely"]
    assert (lk["name"], lk["label"], lk["entity_id"], lk["source"], lk["event"]) == \
        ("Morgan Hale", "Morgan Hale", "e-morgan", "calendar", "Morgan-Noah week-ahead prep")
    assert lk["known"] is False and lk["voiceprint"] is False


def test_people_already_named_are_removed_by_hand_or_by_voiceprint():
    cl = clusters(("Noah Park", "Noah Park", "match"), ("Morgan Hale", "Morgan Hale", "user"),
                  ("Speaker B", None, None))
    assert calendar_hint.hint(event(ME, MORGAN), cl, 3, SP) == {"likely": None, "candidates": []}
    # Morgan's own voice was matched by voiceprint: a guest's voice isn't "probably Morgan"
    cl = clusters(("Morgan Hale", "Morgan Hale", "match"), ("Speaker B", None, None))
    assert calendar_hint.hint(event(ME, MORGAN), cl, 2, SP) == {"likely": None, "candidates": []}
    cl = clusters(("Morgan Hale", "Morgan Hale", "match"), ("Speaker B", None, None))
    assert calendar_hint.hint(event(ME, MORGAN, DANA), cl, 2, SP)["likely"]["name"] == "Dana Brooks"
    # a voice already carrying the name isn't offered it again
    assert calendar_hint.hint(event(ME, MORGAN), cl, 1, SP)["likely"] is None


def test_several_left_become_candidates():
    cl = clusters(("Speaker A", None, None))
    h = calendar_hint.hint(event(ME, MORGAN, DANA), cl, 1, SP)
    assert h["likely"] is None
    assert [c["name"] for c in h["candidates"]] == ["Morgan Hale", "Dana Brooks"]
    assert all(c["detail"] == "from Morgan-Noah week-ahead prep" for c in h["candidates"])


def test_an_unknown_attendee_still_counts_but_is_never_offered():
    cl = clusters(("Speaker A", None, None))
    h = calendar_hint.hint(event(ME, MORGAN, ("brandon@vendor.example", None, False)), cl, 1, SP)
    assert h["likely"] is None and [c["name"] for c in h["candidates"]] == ["Morgan Hale"]


def test_the_wearers_voice_gets_no_suggestion_unless_tapped():
    cl = clusters(("Noah Park", "Noah Park", "match"), ("Speaker A", None, None))
    assert calendar_hint.hint(event(ME, MORGAN), cl, 1, SP)["likely"] is None
    assert calendar_hint.hint(event(ME, MORGAN), cl, 1, SP, tapped=True)["likely"]["name"] == "Morgan Hale"


def test_label_follows_an_existing_link_and_known_voices():
    sp = {**SP, "people": {**SP["people"], "Morgan": "e-morgan"}, "allowed": [*SP["allowed"], "Morgan"]}
    cl = clusters(("Speaker A", None, None))
    lk = calendar_hint.hint(event(ME, MORGAN), cl, 1, sp, enrolled={"Morgan"})["likely"]
    assert lk["label"] == "Morgan" and lk["known"] is True and lk["voiceprint"] is True


def test_named_by_entity_link_under_another_name():
    sp = {**SP, "people": {**SP["people"], "Morgan": "e-morgan"}}
    cl = clusters(("Morgan", "Morgan", "user"), ("Speaker B", None, None))
    assert calendar_hint.hint(event(ME, MORGAN, DANA), cl, 2, sp)["likely"]["name"] == "Dana Brooks"


def test_no_event_no_suggestion():
    cl = clusters(("Speaker A", None, None))
    assert calendar_hint.hint(None, cl, 1, SP) == {"likely": None, "candidates": []}
    assert calendar_hint.hint("", cl, 1, SP) == {"likely": None, "candidates": []}
    assert calendar_hint.hint("not json", cl, 1, SP) == {"likely": None, "candidates": []}
    assert calendar_hint.event_view(None) is None


def test_event_view_flags_a_shared_event():
    raw = json.loads(event(ME, MORGAN))
    raw["via"] = "continuation"
    v = calendar_hint.event_view(json.dumps(raw))
    assert v["shared"] is True and [a["name"] for a in v["attendees"]] == ["Noah Park", "Morgan Hale"]


def test_the_wearer_is_removed_by_email_even_unflagged():
    cl = clusters(("Speaker A", None, None))
    raw = json.dumps({"title": "1:1", "attendees": [
        {"name": "Noah", "email": "noah@work.example", "entity_id": None},
        {"name": "Morgan Hale", "email": None, "entity_id": "e-morgan"}]})
    assert calendar_hint.hint(raw, cl, 1, SP)["likely"]["name"] == "Morgan Hale"


# --- maybe: LifeOS's likely-present people ---------------------------------------------------

def present(*people):
    return [{"entity_id": e, "name": n, "reasons": list(r)} for n, e, *r in people]


ALEX = ("Alex Rivera", "e-lex", "texted on my way")
RILEY = ("Riley Park", "e-riley", "lives with you", "home all evening")
MORGAN_P = ("Morgan Hale", "e-morgan", "on your calendar today")


def test_maybe_keeps_lifeos_order_with_its_reasons():
    cl = clusters(("Speaker A", None, None))
    rows = calendar_hint.maybe(present(RILEY, MORGAN_P), cl, 1, SP)
    assert [r["name"] for r in rows] == ["Riley Park", "Morgan Hale"]
    r = rows[0]
    assert (r["label"], r["entity_id"], r["source"], r["reasons"], r["detail"]) == \
        ("Riley Park", "e-riley", "lifeos", ["lives with you", "home all evening"], "lives with you · home all evening")
    assert r["known"] is False and r["voiceprint"] is False and r["new"] is False


def test_maybe_leaves_out_the_wearer_the_named_the_voices_own_name_and_the_calendars():
    me = ("Noah Park", "e-me", "you")
    cl = clusters(("Alex Rivera", "Alex Rivera", "match"), ("Speaker B", None, None), ("Morgan Hale", "Morgan Hale", None))
    ppl = present(me, ALEX, RILEY, MORGAN_P)
    # Alex is named in the conversation, Noah is the wearer
    assert [r["name"] for r in calendar_hint.maybe(ppl, cl, 2, SP)] == ["Riley Park", "Morgan Hale"]
    # the voice's own current name is never offered back to it
    assert [r["name"] for r in calendar_hint.maybe(ppl, cl, 3, SP)] == ["Riley Park"]
    # people the calendar already offers keep their place ahead and aren't repeated
    ahead = [{"entity_id": "e-riley"}, None]
    assert [r["name"] for r in calendar_hint.maybe(ppl, cl, 2, SP, ahead=ahead)] == ["Morgan Hale"]
    # the wearer by name alone, with no LifeOS link
    sp = {**SP, "people": {}}
    assert "Noah Park" not in [r["name"] for r in calendar_hint.maybe(ppl, cl, 2, sp)]


def test_maybe_for_the_wearers_voice_only_when_tapped():
    cl = clusters(("Noah Park", "Noah Park", "match"))
    assert calendar_hint.maybe(present(RILEY), cl, 1, SP) == []
    assert [r["name"] for r in calendar_hint.maybe(present(RILEY), cl, 1, SP, tapped=True)] == ["Riley Park"]


def test_maybe_follows_an_existing_link():
    sp = {**SP, "people": {**SP["people"], "Riley": "e-riley"}, "allowed": [*SP["allowed"], "Riley"]}
    rows = calendar_hint.maybe(present(RILEY), clusters(("Speaker A", None, None)), 1, sp, enrolled={"Riley"})
    assert rows[0]["label"] == "Riley" and rows[0]["known"] is True and rows[0]["voiceprint"] is True


def test_no_present_list_no_maybe():
    cl = clusters(("Speaker A", None, None))
    assert calendar_hint.maybe(None, cl, 1, SP) == [] and calendar_hint.maybe([], cl, 1, SP) == []


def lifeos(answer, calls):
    def get(path, params, timeout):
        calls.append((path, params, timeout))
        if isinstance(answer, BaseException):
            raise answer
        return answer
    return get


def test_likely_present_asks_once_per_window_and_caches():
    calls = []
    answer = {"start": "x", "end": "y", "people": [
        {"person_id": "e-lex", "name": "Alex  Rivera", "score": 0.9, "reasons": ["texted on my way", 3, " "]},
        {"person_id": "bad id!", "name": "Morgan Hale", "score": 0.5, "reasons": []},
        {"person_id": "e-morgan", "name": "Morgan Hale", "score": 0.4}]}
    got = calendar_hint.likely_present(1_700_000_000_000, 1_700_000_600_000, get=lifeos(answer, calls))
    assert got == [{"entity_id": "e-lex", "name": "Alex Rivera", "reasons": ["texted on my way"]},
                   {"entity_id": "e-morgan", "name": "Morgan Hale", "reasons": []}]
    path, params, _ = calls[0]
    assert path == "/api/crm/people/likely-present" and params["limit"] == 5
    assert params["start"] == "2023-11-14T22:13:20+00:00" and params["end"] == "2023-11-14T22:23:20+00:00"
    assert calendar_hint.likely_present(1_700_000_000_000, 1_700_000_600_000, get=lifeos(answer, calls)) == got
    assert len(calls) == 1


def test_likely_present_fails_quietly_and_backs_off():
    calls = []
    import urllib.error
    missing = urllib.error.HTTPError("http://lifeos/x", 404, "Not Found", None, None)
    assert calendar_hint.likely_present(0, 1000, get=lifeos(missing, calls)) is None
    # backed off: no second call while LifeOS is down
    assert calendar_hint.likely_present(5, 1000, get=lifeos({"people": []}, calls)) is None
    assert len(calls) == 1


def test_likely_present_gives_up_at_its_budget():
    import time
    release = []

    def slow(path, params, timeout):
        time.sleep(1.0)
        release.append(1)
        return {"people": [{"person_id": "e-lex", "name": "Alex Rivera"}]}
    t0 = time.monotonic()
    assert calendar_hint.likely_present(0, 1000, get=slow, budget=0.1) is None
    assert time.monotonic() - t0 < 0.5 and not release
    assert calendar_hint._present_down_until > time.time()


def test_likely_present_many_asks_each_distinct_window_once():
    import threading
    calls, lock = [], threading.Lock()

    def get(path, params, timeout):
        with lock:
            calls.append(params["start"])
        return {"people": [{"person_id": "e-lex", "name": "Alex Rivera", "reasons": ["texted on my way"]}]}
    windows = [(0, 1000), (5000, 6000), (0, 1000), (9000, 9500), (5000, 6000)]
    got = calendar_hint.likely_present_many(windows, get=get)
    assert list(got) == [(0, 1000), (5000, 6000), (9000, 9500)]
    assert all(v and v[0]["name"] == "Alex Rivera" for v in got.values())
    assert len(calls) == 3 and len(set(calls)) == 3
    # the next page is answered from the cache
    assert calendar_hint.likely_present_many(windows, get=get) == got and len(calls) == 3


def test_likely_present_many_stops_after_one_failure():
    calls = []
    got = calendar_hint.likely_present_many([(0, 1), (10, 11), (20, 21), (30, 31), (40, 41)],
                                            get=lifeos(RuntimeError("down"), calls))
    assert set(got.values()) == {None} and len(got) == 5
    assert len(calls) == 1


def test_likely_present_many_waits_one_budget_in_all():
    import time

    def slow(path, params, timeout):
        time.sleep(1.0)
        return {"people": []}
    t0 = time.monotonic()
    got = calendar_hint.likely_present_many([(i, i + 1) for i in range(0, 80, 10)], get=slow, budget=0.2)
    assert time.monotonic() - t0 < 0.6
    assert set(got.values()) == {None}


# ---------------------------------------------------------------------------
# people named in the event title

TITLE_SP = {"title_names": True, "wearer": "Noah Park", "allowed": ["Noah Park", "Alex Rivera", "Riley Park", "Morgan Hale", "Morgan Lee"],
            "people": {"Noah Park": "e-me", "Alex Rivera": "e-lex", "Riley Park": "e-riley", "Morgan Hale": "e-morgan"}}


def titled(title, *people):
    return json.dumps({"id": "e2", "title": title, "start_ms": 0, "end_ms": 1,
                       "attendees": [{"name": n, "email": None, "entity_id": e, "wearer": w} for n, e, w in people]})


def test_title_people_full_names_and_unshared_first_names_only():
    assert calendar_hint.title_people("Dinner with Alex", TITLE_SP) == [("Alex Rivera", "e-lex")]
    assert calendar_hint.title_people("riley park / noah sync", TITLE_SP) == [("Riley Park", "e-riley")]
    # first names: written capitalized, and only when nobody else on the list shares them
    assert calendar_hint.title_people("Lunch with alex", TITLE_SP) == []
    assert calendar_hint.title_people("Call with Morgan", TITLE_SP) == []          # Morgan Hale or Morgan Lee
    assert calendar_hint.title_people("Call with Morgan Lee", TITLE_SP) == [("Morgan Lee", None)]
    assert calendar_hint.title_people("Noah's dentist", TITLE_SP) == []              # the wearer is never one
    assert calendar_hint.title_people("Alexander's party", TITLE_SP) == []           # whole words only
    assert calendar_hint.title_people("Riley and Alex", TITLE_SP) == [("Riley Park", "e-riley"), ("Alex Rivera", "e-lex")]


def test_a_person_named_only_in_the_title_is_offered():
    cl = clusters(("Noah Park", "Noah Park", "match"), ("Speaker A", None, None))
    h = calendar_hint.hint(titled("Dinner with Alex", ME), cl, 2, TITLE_SP)
    lk = h["likely"]
    assert (lk["name"], lk["label"], lk["entity_id"], lk["source"], lk["detail"]) == \
        ("Alex Rivera", "Alex Rivera", "e-lex", "calendar", "named in Dinner with Alex")


def test_a_title_name_joins_the_invitees_once():
    cl = clusters(("Speaker A", None, None))
    h = calendar_hint.hint(titled("Alex / Dana sync", ME, ("Alex Rivera", "e-lex", False), DANA), cl, 1, TITLE_SP)
    assert h["likely"] is None
    assert [(c["name"], c["detail"]) for c in h["candidates"]] == \
        [("Alex Rivera", "from Alex / Dana sync"), ("Dana Brooks", "from Alex / Dana sync")]
    h = calendar_hint.hint(titled("Coffee with Riley", ME, DANA), cl, 1, TITLE_SP)
    assert [(c["name"], c["detail"]) for c in h["candidates"]] == \
        [("Dana Brooks", "from Coffee with Riley"), ("Riley Park", "named in Coffee with Riley")]


def test_a_title_name_already_named_in_the_conversation_is_left_out():
    cl = clusters(("Alex Rivera", "Alex Rivera", "user"), ("Speaker A", None, None))
    assert calendar_hint.hint(titled("Dinner with Alex", ME), cl, 2, TITLE_SP) == {"likely": None, "candidates": []}


# ---------------------------------------------------------------------------
# kept likely-present answers

def answer(eid, name, reasons=()):
    """LifeOS's likely-present reply with one person."""
    return {"people": [{"person_id": eid, "name": name, "reasons": list(reasons)}]}


def kept_db():
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE speaker_clusters (id INTEGER PRIMARY KEY, device TEXT, start_ms INT, tag TEXT, "
                "labeled_by TEXT, noise INT, speech_s REAL)")
    con.execute("CREATE TABLE conversations (device TEXT, start_ms INT, end_ms INT)")
    for start, by in ((0, None), (100, "match"), (200, "user"), (300, None)):
        con.execute("INSERT INTO conversations VALUES ('D', ?, ?)", (start, start + 50))
        con.execute("INSERT INTO speaker_clusters (device, start_ms, tag, labeled_by, noise, speech_s) "
                    "VALUES ('D', ?, 'Speaker A', ?, ?, 5)", (start, by, 1 if start == 300 else 0))
    return con


def test_warm_present_keeps_an_answer_for_every_card_window_newest_first():
    con = kept_db()
    calls = []

    def get(path, params, timeout):
        calls.append(params["start"])
        return answer("e-lex", "Alex Rivera", ["texted on my way"])
    assert calendar_hint.warm_present(con, get=get, now=100_000.0) == 2   # answered and noise voices aren't cards
    assert calls == [calendar_hint._iso(100), calendar_hint._iso(0)]
    kept = calendar_hint.stored_present(con, [(0, 50), (100, 150), (200, 250)])
    assert set(kept) == {(0, 50), (100, 150)}
    assert kept[(0, 50)] == [{"entity_id": "e-lex", "name": "Alex Rivera", "reasons": ["texted on my way"]}]
    # settled answers are never asked for again
    assert calendar_hint.warm_present(con, get=get, now=100_000.0 + 3600) == 0 and len(calls) == 2


def test_warm_present_refreshes_an_unsettled_window_and_stops_at_a_failure():
    con = kept_db()
    end_s = 0.15                                   # the newest window ends at 150 ms
    calendar_hint.warm_present(con, get=lambda *a: answer("e-lex", "Alex Rivera"), now=end_s + 60)
    assert calendar_hint.warm_present(con, get=lambda *a: 1 / 0, now=end_s + 120) == 0      # not due yet
    logged = []

    def down(*a):
        raise OSError("down")
    assert calendar_hint.warm_present(con, get=down, now=end_s + 60 + calendar_hint.PRESENT_REFRESH_S,
                                      log=logged.append) == 0
    assert logged and "stopped" in logged[0]
    assert calendar_hint.stored_present(con, [(100, 150)])[(100, 150)][0]["name"] == "Alex Rivera"   # kept as it was
    got = calendar_hint.warm_present(con, get=lambda *a: answer("e-morgan", "Morgan Hale"),
                                     now=end_s + 60 + calendar_hint.PRESENT_REFRESH_S)
    assert got == 2 and calendar_hint.stored_present(con, [(100, 150)])[(100, 150)][0]["name"] == "Morgan Hale"


def test_present_due():
    assert calendar_hint.present_due(None, 0, 0)
    assert not calendar_hint.present_due(calendar_hint.PRESENT_SETTLE_S, 0, 10**9)
    assert not calendar_hint.present_due(100, 0, 100 + calendar_hint.PRESENT_REFRESH_S - 1)
    assert calendar_hint.present_due(100, 0, 100 + calendar_hint.PRESENT_REFRESH_S)


def test_stored_present_without_the_table_is_empty():
    import sqlite3
    assert calendar_hint.stored_present(sqlite3.connect(":memory:"), [(0, 1)]) == {}


def test_title_names_are_off_by_default():
    off = {k: v for k, v in TITLE_SP.items() if k != "title_names"}
    cl = clusters(("Speaker A", None, None))
    assert calendar_hint.hint(titled("Dinner with Alex", ME), cl, 1, off) == {"likely": None, "candidates": []}
    named_only = [{"entity_id": "e-riley", "name": "Riley Park", "reasons": ["named in an event at 19:00"]},
                  {"entity_id": "e-lex", "name": "Alex Rivera", "reasons": ["named in an event at 19:00",
                                                                             "texted on my way"]}]
    assert [m["name"] for m in calendar_hint.maybe(named_only, cl, 1, off)] == ["Alex Rivera"]
    assert [m["name"] for m in calendar_hint.maybe(named_only, cl, 1, TITLE_SP)] == ["Riley Park", "Alex Rivera"]
