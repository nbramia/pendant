"""Who a voice probably is, from the conversation's calendar event.

The processor stores each conversation's calendar match (`conversations.calendar_event`,
see process.relink_meetings): the event's title, times and attendees, resolved to LifeOS
people where it could. For a voice in that conversation, the attendees left after removing
The wearer, people already named in the same conversation (by hand, a meeting or calendar
pass, or a voiceprint match), and the voice's own current name, are who it probably is:

- exactly one left (counting attendees LifeOS doesn't know): `likely`, offered first in
  every picker as one tap to confirm;
- otherwise `candidates`: the remaining LifeOS people, which head the picker list.

Nothing here names anyone. Confirming is an ordinary hand label (source "user") with the
person's entity_id. Pure data from pages.db and config.json: no LifeOS call, so pickers
behave as before when the calendar couldn't be read. Stdlib only: the feed imports it.
"""
from __future__ import annotations

import json
import re

from . import attendance as _attendance

CONFIRMED = ("user", "meeting", "calendar", "match")    # a voiceprint match counts as present
GENERIC = re.compile(r"^Speaker [A-Z]{1,2}$")


def stored(raw) -> dict | None:
    """The conversation's stored calendar match, or None."""
    try:
        v = json.loads(raw) if raw else None
    except ValueError:
        return None
    return v if isinstance(v, dict) and isinstance(v.get("title"), str) else None


def event_view(raw) -> dict | None:
    """The calendar match as the day feed shows it: id, title, start_ms, end_ms, attendees
    (name, email, entity_id, wearer) and `shared` when it came from another part of the
    same meeting."""
    ev = stored(raw)
    if ev is None:
        return None
    people = [{"name": str(a.get("name") or ""), "email": a.get("email"), "entity_id": a.get("entity_id"),
               "wearer": bool(a.get("wearer"))}
              for a in ev.get("attendees") or [] if isinstance(a, dict) and a.get("name")]
    return {"id": ev.get("id"), "title": ev["title"], "start_ms": ev.get("start_ms"), "end_ms": ev.get("end_ms"),
            "attendees": people, "shared": ev.get("via") == "continuation"}


def _name(c: dict) -> str | None:
    """The name a voice carries now (its label, or a tag that isn't `Speaker X`/Someone)."""
    n = c.get("label") or c.get("tag")
    if not n or GENERIC.match(n) or n.lower() == "someone":
        return None
    return n


def hint(raw_event, clusters: list[dict], target: int, sp_cfg: dict, enrolled: set[str] | None = None,
         tapped: bool = False) -> dict:
    """{"likely": row | None, "candidates": [rows]} for voice cluster `target` among the
    conversation's `clusters` (dicts with id, tag, label, labeled_by). Rows look like the
    name picker's (name, label, entity_id, known, voiceprint, detail) plus source
    "calendar" and the event's title. A voice the wearer's name is on gets none, unless
    the wearer `tapped` it to change who it is (the reassign sheet)."""
    from . import lifeos_people
    empty = {"likely": None, "candidates": []}
    ev = stored(raw_event)
    if ev is None:
        return empty
    links = {str(k): v for k, v in (sp_cfg.get("people") or {}).items() if isinstance(v, str) and v}
    by_entity = {v: k for k, v in links.items()}
    wearer = str(sp_cfg.get("wearer") or "")
    wearer_ids = {links[k] for k in links if k.lower() == wearer.lower()}
    me = next((c for c in clusters if c.get("id") == target), None)
    if me is None:
        return empty
    named = {(_name(c) or "").lower() for c in clusters
             if c.get("id") != target and c.get("labeled_by") in CONFIRMED and _name(c)}
    named_ids = {links[k] for k in links if k.lower() in named}
    own = (_name(me) or "").lower()
    if own and own == wearer.lower() and not tapped:
        return empty
    allowed = {str(a).lower() for a in sp_cfg.get("allowed") or []}
    enrolled = {n.lower() for n in (enrolled or set())}
    taken = {k.lower(): v for k, v in links.items()}

    left = []
    for a in ev.get("attendees") or []:
        if not isinstance(a, dict) or not a.get("name"):
            continue
        eid = a.get("entity_id")
        name = str(a["name"])
        if a.get("wearer") or (eid and eid in wearer_ids) or name.lower() == wearer.lower() \
                or str(a.get("email") or "").lower() in _attendance.WEARER_EMAILS:
            continue
        label = by_entity.get(eid) if eid else None
        if eid and label is None:
            base = lifeos_people.speaker_name(name)
            label = lifeos_people.distinct_name({"label": base, "entity_id": eid, "company": None,
                                                 "email": a.get("email")}, taken) if base else None
        if (eid and eid in named_ids) or name.lower() in named or (label and label.lower() in named):
            continue
        if name.lower() == own or (label and label.lower() == own):
            continue
        left.append({"name": name, "label": label, "entity_id": eid if label else None})
    rows = [{"name": p["name"], "label": p["label"], "entity_id": p["entity_id"], "circle": None,
             "category": None, "strength": None, "detail": f"from {ev['title']}",
             "voiceprint": p["label"].lower() in enrolled, "known": p["label"].lower() in allowed | enrolled,
             "new": False, "source": "calendar", "event": ev["title"]}
            for p in left if p["entity_id"]]
    if len(left) == 1 and rows:
        return {"likely": rows[0], "candidates": []}
    return {"likely": None, "candidates": rows}
