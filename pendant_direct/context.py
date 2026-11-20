"""Conversation context as a soft naming prior (`speakers.context` in config.json).

Who is plausibly speaking depends on the setting: colleagues in meetings and during work hours,
family and friends in the evening and at weekends. Each linked person's LifeOS category (work,
family, personal, self) is set against the conversation's setting, and a person unexpected there
needs a higher raw score to be named (`extra_bars`). It only raises bars: it never names a voice
that wouldn't be named without it, and a barred winner leaves the voice unnamed rather than
handing it to the runner-up.

  work setting:     a calendar event listing someone besides the wearer covers the
                    conversation, or a weekday between work_start and work_end;
  personal setting: everything else.

  work person in a personal setting                     -> + work_off
  family/personal person in a meeting they're not on    -> + personal_in_meeting
`not_on_event` (on even when `enabled` is off; 0 turns it off): any linked person who isn't on
the calendar event covering the conversation, when that event says the wearer was there and
lists who was (attendance.Event.with_owner), needs that much more -> + not_on_event. A small
conversation with its people on the calendar isn't filled with people who weren't there.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

DEFAULTS = {"enabled": False, "work_off": 0.05, "personal_in_meeting": 0.03, "work_start": 9, "work_end": 18,
            "not_on_event": 0.08}
PERSONAL_PEOPLE = {"family", "personal"}


@dataclass
class Context:
    start: dt.datetime                  # local start of the conversation
    meeting: bool = False               # an event listing someone besides the wearer covers it
    attendees: frozenset = frozenset()  # lowercased attendee keys of that event (emails, names, entity ids)


def settings(sp: dict) -> dict:
    return {**DEFAULTS, **(sp.get("context") or {})}


def is_work(ctx: Context, cfg: dict) -> bool:
    if ctx.meeting:
        return True
    return ctx.start.weekday() < 5 and int(cfg["work_start"]) <= ctx.start.hour < int(cfg["work_end"])


def extra_bars(ctx: Context | None, people: dict[str, dict], cfg: dict) -> dict[str, float]:
    """{lowercased name: extra raw score needed} for the people unexpected in this context.
    `people`: {name: {"category": ..., "keys": set of lowercased name/email/entity keys}}."""
    if ctx is None:
        return {}
    not_on = max(0.0, float(cfg.get("not_on_event") or 0))
    out = {}
    if not cfg.get("enabled"):
        if ctx.meeting and not_on:
            for name, p in people.items():
                if (p.get("category") or "").lower() != "self" and not (set(p.get("keys") or ()) & ctx.attendees):
                    out[name.lower()] = not_on
        return out
    work = is_work(ctx, cfg)
    for name, p in people.items():
        cat = (p.get("category") or "").lower()
        if cat != "self" and ctx.meeting and not_on and not (set(p.get("keys") or ()) & ctx.attendees):
            out[name.lower()] = not_on
            continue
        if cat == "work" and not work:
            out[name.lower()] = max(0.0, float(cfg["work_off"]))
        elif cat in PERSONAL_PEOPLE and ctx.meeting and not (set(p.get("keys") or ()) & ctx.attendees):
            out[name.lower()] = max(0.0, float(cfg["personal_in_meeting"]))
    return out


def person_keys(row: dict) -> set[str]:
    """Lowercased keys a calendar attendee may carry for a LifeOS person row."""
    from . import attendance
    keys = attendance._keys({"email": row.get("email"), "canonical_name": row.get("name"),
                             "display_name": row.get("name"), "aliases": row.get("aliases")})
    if isinstance(row.get("entity_id"), str) and row["entity_id"]:
        keys.add(row["entity_id"].lower())
    return keys


def meeting_attendees(attendees) -> frozenset | None:
    """The attendee keys of an event that lists someone besides the wearer, else None (no event,
    or a solo block). `attendees`: raw strings (attendance.Event) or stored dicts
    ({name, email, entity_id, wearer}, conversations.calendar_event)."""
    from . import attendance
    keys, others = set(), False
    for a in attendees or ():
        if isinstance(a, dict):
            ks = {str(a.get(k)).strip().lower() for k in ("name", "email", "entity_id") if a.get(k)}
            wearer = bool(a.get("wearer")) or bool(ks & attendance.WEARER_EMAILS)
        else:
            ks = {str(a).strip().lower()} if str(a).strip() else set()
            wearer = bool(ks & attendance.WEARER_EMAILS)
        keys |= ks
        others = others or (bool(ks) and not wearer)
    return frozenset(keys) if others else None


def context_at(start_ms: int, attendees=None) -> Context:
    from . import attendance
    met = meeting_attendees(attendees)
    return Context(start=dt.datetime.fromtimestamp(start_ms / 1000, attendance.TZ), meeting=met is not None,
                   attendees=met or frozenset())


def bars_for(sp: dict, names, key, segs, run=None) -> dict[str, float]:
    """The extra bars for this conversation (`key` = (device, start_ms), `segs` its transcript
    segments), from the LifeOS categories of the enrolled `names` and the calendar (through a
    closeness.PriorRun, which shares its lookups and time budget); {} when off or unknown."""
    cfg = settings(sp)
    if not (cfg.get("enabled") or cfg.get("not_on_event")) or not key:
        return {}
    from . import closeness, config
    run = run or closeness.PriorRun()
    start_ms = int(key[1])
    end_ms = start_ms + int(max((s[1] for s in segs), default=0) * 1000)
    links = {n: config.linked_entity(sp.get("people"), n) for n in names}
    links = {n: e for n, e in links.items() if e}
    rows = run._people({n: e for n, e in (sp.get("people") or {}).items() if isinstance(e, str) and e}) if links else None
    if not rows:
        return {}
    ok, attendees = run._attendees(start_ms, end_ms)
    if not ok:
        return {}
    people = {n: {"category": (rows.get(e) or {}).get("category"), "keys": person_keys(rows.get(e) or {})}
              for n, e in links.items()}
    return extra_bars(context_at(start_ms, attendees), people, cfg)
