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
