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
