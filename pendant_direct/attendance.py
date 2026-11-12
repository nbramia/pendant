"""Calendar events and attendees via the LifeOS API on this machine (localhost:8000).

Every conversation is matched to the calendar event that best overlaps it
(`event_for`), and the event's attendees are resolved to LifeOS people
(`resolve_attendees`): the app shows the match and offers an attendee as the likely
name of a voice nobody has named. That is only ever a suggestion.

The same events are used to learn coworkers' voices only from meetings whose attendance is known
(the wearer's rule): the calendar event must list every attendee as a real person
(emails or names LifeOS resolves to people), and the meeting must be small
(3 people or fewer, the wearer included). Group invites ("Leads", "Project HQ")
mean attendance is unknown, so those meetings are skipped.
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from . import config as _config

TZ = ZoneInfo("America/New_York")
API = "http://localhost:8000"
# The wearer's own calendar addresses (`speakers.wearer_emails` in config.json): an attendee with
# one of them is the wearer, never someone to name a voice after.
WEARER_EMAILS = frozenset(str(e).strip().lower() for e in
                          (_config.load(_config.data_dir()).get("speakers", {}).get("wearer_emails") or [])
                          if str(e).strip())
WEARER_NAME = _config.wearer(_config.load(_config.data_dir()).get("speakers"))   # `speakers.wearer`
EARLY_MS = 5 * 60_000        # a conversation may start this long before an event ...
OVERRUN_MS = 30 * 60_000     # ... and run this long past its booked end (meetings overrun)
MIN_SHARE = 0.5              # share of the conversation inside that window
ACCOUNTS = ("work", "personal")


@dataclass
class Event:
    title: str
    start_ms: int
    end_ms: int
    attendees: list[str]
    id: str | None = None

    @property
    def has_people(self) -> bool:
        """Lists someone besides the wearer (a personal block lists nobody)."""
        return any(a.strip().lower() not in WEARER_EMAILS for a in self.attendees)


def _get(path: str, params: dict, timeout: float = 10.0) -> dict:
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _ms(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso).timestamp() * 1000)


def events_on(day: dt.date, get=_get) -> list[Event]:
    """Every calendar event (work and personal) on `day`."""
    today = dt.datetime.now(TZ).date()
    back = max(1, (today - day).days + 1)
    events, seen = [], set()
    for account in ACCOUNTS:  # without `account`, LifeOS searches only the personal calendar
        for e in get("/api/calendar/search", {"q": " ", "days_back": back, "days_forward": 1,
                                              "account": account}).get("events") or []:
            key = (e.get("title"), e.get("start_time"))
            if key not in seen:
                seen.add(key)
                events.append(e)
    out = []
    for e in events:
        if e.get("is_all_day") or not e.get("start_time") or not e.get("end_time"):
            continue
        ev = Event(e.get("title") or "", _ms(e["start_time"]), _ms(e["end_time"]), list(e.get("attendees") or []),
                   e.get("event_id") or e.get("id"))
        if dt.datetime.fromtimestamp(ev.start_ms / 1000, TZ).date() == day:
            out.append(ev)
    return out


def event_for(events: list[Event], start_ms: int, end_ms: int) -> Event | None:
    """The calendar event a pendant span belongs to (for display and suggestions), or None.

    The span must start no more than EARLY_MS before the event, always. Then the event
    qualifies when more than MIN_SHARE of the span falls between its start (less EARLY_MS)
    and its end plus OVERRUN_MS (no overrun for an event listing nobody else), or when the
    span covers more than half of the event's booked time. Among those, events with people
    rank above ones without (personal blocks), then the largest overlap with the booked
    time, then with the overrun window."""
    span = max(1, end_ms - start_ms)
    best, best_key = None, None
    for ev in events:
        if start_ms < ev.start_ms - EARLY_MS:
            continue
        booked = max(0, min(end_ms, ev.end_ms) - max(start_ms, ev.start_ms))
        overrun = OVERRUN_MS if ev.has_people else 0     # a personal block doesn't overrun
        window = max(0, min(end_ms, ev.end_ms + overrun) - max(start_ms, ev.start_ms - EARLY_MS))
        covers = booked / max(1, ev.end_ms - ev.start_ms) > 0.5
        if not (window / span > MIN_SHARE or covers):
            continue
        key = (ev.has_people, booked, window)
        if best_key is None or key > best_key:
            best, best_key = ev, key
    return best


def event_within(events: list[Event], start_ms: int, end_ms: int) -> Event | None:
    """The event a span lies inside: it starts no more than EARLY_MS before the event and ends
    by the event's booked end (no overrun). The strict rule for learning a voiceprint without
    a tap; `event_for`'s overrun is for display and suggestions only."""
    inside = [ev for ev in events if start_ms >= ev.start_ms - EARLY_MS and end_ms <= ev.end_ms]
    return max(inside, key=lambda ev: (ev.has_people, -(ev.end_ms - ev.start_ms)), default=None)


def _keys(p: dict) -> set[str]:
    keys = {p.get("email"), p.get("canonical_name"), p.get("display_name"), *(p.get("aliases") or [])}
    return {k.lower() for k in keys if isinstance(k, str) and k}


def resolve_people(attendees: list[str], get=_get) -> list[str] | None:
    """Canonical names for every attendee, or None if any isn't a known person
    (a group alias, a room, an unknown address): attendance then isn't known."""
    names = []
    for a in attendees:
        a = a.strip()
        if a.lower() in WEARER_EMAILS:
            names.append(WEARER_NAME)
            continue
        try:
            people = get("/api/people/search", {"q": a, "limit": 2}).get("people") or []
        except Exception:
            return None
        exact = [p for p in people if a.lower() in _keys(p)]
        # A real person has an email; group calendars ("Project HQ") don't.
        if len(exact) != 1 or not exact[0].get("email"):
            return None
        names.append(exact[0]["canonical_name"])
    return sorted(set(names))


def resolve_attendees(attendees: list[str], people_search=None, me: str | None = None,
                      wearer: str | None = None) -> list[dict]:
    """Each attendee as {name, email, entity_id, wearer}: the LifeOS person (full name and
    entity id) when the CRM knows exactly that email or name, else the raw string as the
    name and no entity id. `people_search(q)` returns CRM rows (lifeos_people.people).
    The wearer is any of: one of WEARER_EMAILS, LifeOS's own person `me` (an alias address
    resolves to it), or the configured speaker name `wearer` (or WEARER_NAME). Raises when
    LifeOS can't answer."""
    names = {n.lower() for n in (wearer, WEARER_NAME) if n}
    if people_search is None:
        from . import lifeos_people
        people_search = lifeos_people.people
    out = []
    for raw in attendees:
        a = " ".join(str(raw or "").split())
        if not a:
            continue
        email = a.lower() if "@" in a else None
        hit = None
        for r in people_search(a):
            keys = {k.lower() for k in (r.get("email"), r.get("name"), r.get("display"), r.get("label"),
                                       *(r.get("emails") or [])) if isinstance(k, str) and k}
            if a.lower() in keys:
                hit = r
                break
        is_me = bool(email and email in WEARER_EMAILS) or bool(hit and me and hit["entity_id"] == me) \
            or a.lower() in names or bool(hit and {str(hit.get(k) or "").lower()
                                                   for k in ("name", "display", "label")} & names)
        if hit:
            out.append({"name": hit["name"], "email": email or hit.get("email"), "entity_id": hit["entity_id"],
                        "wearer": is_me})
        else:
            out.append({"name": (wearer or WEARER_NAME) if is_me else a, "email": email, "entity_id": None,
                        "wearer": is_me})
    return out
