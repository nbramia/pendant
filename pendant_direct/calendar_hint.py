"""Who a voice probably is, from the conversation's calendar event.

The processor stores each conversation's calendar match (`conversations.calendar_event`,
see process.relink_meetings): the event's title, times and attendees, resolved to LifeOS
people where it could. For a voice in that conversation, the attendees left after removing
The wearer, people already named in the same conversation (by hand, a meeting or calendar
pass, or a voiceprint match), and the voice's own current name, are who it probably is:

- exactly one left (counting attendees LifeOS doesn't know): `likely`, offered first in
  every picker as one tap to confirm;
- otherwise `candidates`: the remaining LifeOS people, which head the picker list.

After those, `maybe`: the people LifeOS's likely-present endpoint
(`GET /api/crm/people/likely-present?start=&end=&limit=5`) thinks were around for the
conversation's window, in its order and with its reason labels, minus the same people and anyone
the calendar already offers. `likely_present` asks LifeOS once per conversation window under a
short budget, caches the answer and backs off after a failure; with LifeOS unreachable, slow or
without that endpoint there are no `maybe` entries and the calendar's answer stands.
`likely_present_many` asks for a page of windows (the Who's this cards) through the same cache and
back-off: each distinct window once, a few at a time, and the page waits for them no longer than
one call's budget.

Nothing here names anyone. Confirming is an ordinary hand label (source "user") with the
person's entity_id. `hint` is pure data from pages.db and config.json, so pickers behave as
before when LifeOS can't answer. Stdlib only: the feed imports it.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time

from . import attendance as _attendance
from . import config as _config

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


def _context(clusters: list[dict], target: int, sp_cfg: dict, tapped: bool):
    """(links, by_entity, wearer, wearer_ids, named, named_ids, own) for voice `target`: who is left
    out of its suggestions. None when the voice isn't among `clusters`, or carries the wearer's
    name and wasn't `tapped`."""
    links = {str(k): v for k, v in (sp_cfg.get("people") or {}).items() if isinstance(v, str) and v}
    by_entity = {v: k for k, v in links.items()}
    wearer = str(sp_cfg.get("wearer") or "")
    wearer_ids = {links[k] for k in links if k.lower() == wearer.lower()}
    me = next((c for c in clusters if c.get("id") == target), None)
    if me is None:
        return None
    named = {(_name(c) or "").lower() for c in clusters
             if c.get("id") != target and c.get("labeled_by") in CONFIRMED and _name(c)}
    named_ids = {links[k] for k in links if k.lower() in named}
    own = (_name(me) or "").lower()
    if own and own == wearer.lower() and not tapped:
        return None
    return links, by_entity, wearer, wearer_ids, named, named_ids, own


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
    ctx = _context(clusters, target, sp_cfg, tapped)
    if ctx is None:
        return empty
    links, by_entity, wearer, wearer_ids, named, named_ids, own = ctx
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


# LifeOS's "probably present" list (`likely_present`), as closeness.py budgets its calls
PRESENT_PATH = "/api/crm/people/likely-present"
PRESENT_LIMIT = 5
PRESENT_BUDGET_S = 3.0     # the picker is waiting: one call, abandoned at this deadline (as closeness.BUDGET_S)
PRESENT_CACHE_S = 600
PRESENT_FAIL_S = 60
PRESENT_WORKERS = 4        # a page of cards asks about this many windows at once

_present: dict[tuple[int, int], tuple[float, list[dict]]] = {}
_present_down_until = 0.0
_present_lock = threading.Lock()


def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="seconds")


def _present_rows(data) -> list[dict]:
    """LifeOS's people as (entity_id, name, reasons), best first; malformed entries dropped."""
    out = []
    for p in (data.get("people") if isinstance(data, dict) else None) or []:
        if not isinstance(p, dict):
            continue
        eid, name = p.get("person_id"), " ".join(str(p.get("name") or "").split())
        if not isinstance(eid, str) or not _config.ENTITY_RE.match(eid) or not name:
            continue
        reasons = [" ".join(r.split()) for r in p.get("reasons") or [] if isinstance(r, str) and r.strip()]
        out.append({"entity_id": eid, "name": name, "reasons": reasons})
    return out


def likely_present(start_ms: int, end_ms: int, get=None, budget: float | None = None) -> list[dict] | None:
    """Who LifeOS thinks was around from `start_ms` to `end_ms`: [{entity_id, name, reasons}],
    best first, or None when it can't say. One call per window, cached for PRESENT_CACHE_S; it runs
    in a daemon thread abandoned after `budget` seconds (default PRESENT_BUDGET_S), and after a
    failure, a timeout or a missing endpoint no call is made for PRESENT_FAIL_S. `get(path, params, timeout)` defaults to the live
    LifeOS API (looked up per call, so tests can block it)."""
    global _present_down_until
    budget = PRESENT_BUDGET_S if budget is None else budget
    key = (int(start_ms), int(end_ms))
    now = time.time()
    with _present_lock:
        hit = _present.get(key)
    if hit and hit[0] > now:
        return hit[1]
    if now < _present_down_until:
        return None
    call = get or (lambda path, params, timeout: _attendance._get(path, params, timeout=timeout))
    params = {"start": _iso(start_ms), "end": _iso(max(end_ms, start_ms)), "limit": PRESENT_LIMIT}
    box: dict = {}

    def work():
        try:
            box["v"] = _present_rows(call(PRESENT_PATH, params, budget))
        except BaseException as e:  # noqa: BLE001 - any failure means no maybe entries
            box["e"] = e
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(budget)
    if "v" not in box:
        _present_down_until = time.time() + PRESENT_FAIL_S
        return None
    now = time.time()
    with _present_lock:
        for k in [k for k, (until, _) in _present.items() if until <= now]:
            del _present[k]
        _present[key] = (now + PRESENT_CACHE_S, box["v"])
    return box["v"]


def likely_present_many(windows, get=None, budget: float | None = None) -> dict[tuple[int, int], list[dict] | None]:
    """`likely_present` for each distinct (start_ms, end_ms) in `windows`, in their order:
    {window: people or None}. The first window is asked alone, so LifeOS being down costs one
    call and its back-off answers the rest; then PRESENT_WORKERS daemon threads ask at once, each
    call with its own budget, cache and back-off. The caller waits at most `budget` seconds
    (default PRESENT_BUDGET_S) in all; no call starts after that, and a window still unanswered is
    None (a call already running finishes in the background and fills the cache for the next page)."""
    budget = PRESENT_BUDGET_S if budget is None else budget
    todo = list(dict.fromkeys((int(s), int(e)) for s, e in windows))
    out: dict[tuple[int, int], list[dict] | None] = {}
    lock = threading.Lock()
    queue = list(reversed(todo))
    first_done = threading.Event()
    deadline = time.monotonic() + budget

    def run(first: bool):
        if not first and not first_done.wait(max(0.0, deadline - time.monotonic())):
            return
        while True:
            with lock:
                if not queue or time.monotonic() >= deadline:
                    break
                w = queue.pop()
            v = likely_present(*w, get=get)
            with lock:
                out[w] = v
            if first:
                break
        if first:
            first_done.set()
            run(False)
    threads = [threading.Thread(target=run, args=(i == 0,), daemon=True)
               for i in range(min(PRESENT_WORKERS, len(todo)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    with lock:
        return {w: out.get(w) for w in todo}


def maybe(present: list[dict] | None, clusters: list[dict], target: int, sp_cfg: dict,
          enrolled: set[str] | None = None, ahead: list[dict | None] = (), tapped: bool = False) -> list[dict]:
    """The `maybe` rows for voice `target`: LifeOS's likely-present people in its order, minus the
    wearer, people already named in the conversation, the voice's own name and anyone in `ahead`
    (the calendar's likely and candidates). Rows look like the name picker's (name, label,
    entity_id, known, voiceprint, detail) plus source "lifeos" and `reasons`; `detail` is the
    reasons joined, the label the picker shows under the name."""
    from . import lifeos_people
    if not present:
        return []
    ctx = _context(clusters, target, sp_cfg, tapped)
    if ctx is None:
        return []
    links, by_entity, wearer, wearer_ids, named, named_ids, own = ctx
    allowed = {str(a).lower() for a in sp_cfg.get("allowed") or []}
    enrolled = {n.lower() for n in (enrolled or set())}
    taken = {k.lower(): v for k, v in links.items()}
    seen = {r["entity_id"] for r in ahead if r and r.get("entity_id")}
    rows = []
    for p in present:
        eid, name = p["entity_id"], p["name"]
        if eid in seen or eid in wearer_ids or eid in named_ids or name.lower() == wearer.lower():
            continue
        label = by_entity.get(eid)
        if label is None:
            base = lifeos_people.speaker_name(name)
            label = lifeos_people.distinct_name({"label": base, "entity_id": eid, "company": None,
                                                 "email": None}, taken) if base else None
        if not label or label.lower() == wearer.lower():
            continue
        if {name.lower(), label.lower()} & (named | {own}):
            continue
        seen.add(eid)
        rows.append({"name": name, "label": label, "entity_id": eid, "circle": None, "category": None,
                     "strength": None, "detail": " · ".join(p["reasons"]) or None, "reasons": p["reasons"],
                     "voiceprint": label.lower() in enrolled, "known": label.lower() in allowed | enrolled,
                     "new": False, "source": "lifeos"})
    return rows
