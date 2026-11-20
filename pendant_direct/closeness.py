"""A modest naming prior from how close a person is to the wearer (LifeOS).

    prior = w * strength/100 + w_cal * in_calendar_event - w_age * min(days_since_contact, 365)/365

It is added to a person's margin above their own match threshold, and only to people
who already clear that threshold on their own (`rerank_key`): it orders near-ties,
never lifts a voice over the threshold, and never touches someone with no voiceprint
(only enrolled names are ever passed in). Without a LifeOS link, or when LifeOS is
unreachable, there is no prior and ranking is exactly the unweighted one.
"""
from __future__ import annotations

import time
from pathlib import Path

DEFAULTS = {"weight": 0.10, "weight_calendar": 0.10, "weight_age": 0.05}
QUALIFIED = 1e6        # qualifying candidates always outrank the rest in `rerank_key`
CACHE_S = 600
FAIL_S = 60
FETCH_TIMEOUT = 3.0
BUDGET_S = 3.0         # all LifeOS calls of one run together

_cache: dict[str, tuple[float, dict]] = {}
_down_until = 0.0


def prior(strength, days, in_event: bool, cfg: dict | None = None) -> float:
    c = {**DEFAULTS, **(cfg or {})}
    s = min(max(float(strength or 0.0), 0.0), 100.0)
    d = min(max(float(days if days is not None else 365), 0.0), 365.0)
    return (float(c["weight"]) * s / 100.0 + float(c["weight_calendar"]) * bool(in_event)
            - float(c["weight_age"]) * d / 365.0)


def vector(names, priors: dict[str, float] | None):
    """Array of priors aligned with `names` (0 for a name with none), or None without priors."""
    if not priors:
        return None
    import numpy as np
    low = {k.lower(): v for k, v in priors.items()}
    return np.array([low.get(str(n).lower(), 0.0) for n in names], dtype=float)


def rerank_key(excess, prior_vec):
    """Ranking key per candidate (higher is better). `excess` is each candidate's score minus
    its own threshold (same shape as `prior_vec` or broadcastable). Candidates at or above
    their threshold rank by excess + prior and always outrank the rest, which keep their
    plain excess order. With no prior this is just `excess`."""
    import numpy as np
    if prior_vec is None:
        return excess
    return np.where(excess >= 0, QUALIFIED + excess + prior_vec, excess)


def pair_key(score: float, threshold: float, prior_j: float | None) -> float:
    """`rerank_key` for one (voice, person) pair, as speakers.match sorts them: always by
    margin above the person's own threshold (a shared threshold makes that the score order),
    so zero priors and no prior rank alike."""
    if prior_j is None:
        return float(score) - float(threshold)
    excess = float(score) - float(threshold)
    return QUALIFIED + excess + prior_j if excess >= 0 else excess


class PriorRun:
    """LifeOS lookups for one processing run (or one feed request), under one budget.

    People for every linked name are fetched in a single pass and kept for the run; the
    calendar is fetched once per day. All calls together get `budget` seconds (each runs
    in a daemon thread that is abandoned at the deadline, so a hung LifeOS can't stall
    transcription), and the failure backoff is checked before any call. After one
    failure or timeout the run is dead: `priors` returns {} (no prior is applied)."""

    def __init__(self, budget: float = BUDGET_S, get=None, clock=time.monotonic):
        self.budget, self.get, self.clock = budget, get, clock
        self.started: float | None = None
        self.dead = False
        self._rows: dict[str, dict] | None = None
        self._days: dict = {}

    def _call(self, fn):
        """fn(timeout) -> value, within the remaining budget; marks the run dead on any failure."""
        global _down_until
        import threading
        if self.dead or (self.get is None and time.time() < _down_until):
            self.dead = True
            raise TimeoutError("LifeOS unavailable")
        now = self.clock()
        if self.started is None:
            self.started = now
        remaining = self.budget - (now - self.started)
        box: dict = {}

        def work():
            try:
                box["v"] = fn(max(0.1, min(FETCH_TIMEOUT, remaining)))
            except BaseException as e:  # noqa: BLE001
                box["e"] = e
        if remaining > 0:
            t = threading.Thread(target=work, daemon=True)
            t.start()
            t.join(remaining)
        if "v" in box:
            return box["v"]
        self.dead = True
        if self.get is None:
            _down_until = time.time() + FAIL_S
        raise box.get("e") or TimeoutError("LifeOS did not answer in time")

    def _people(self, links: dict[str, str]) -> dict[str, dict] | None:
        from . import lifeos_people
        if self._rows is None:
            rows, need = {}, []
            for eid in set(links.values()):
                hit = _cache.get(eid) if self.get is None else None
                if hit and hit[0] > time.time():
                    rows[eid] = hit[1]
                else:
                    need.append(eid)
            if need:
                try:
                    got = self._call(lambda t: {e: lifeos_people.entity(e, get=self.get, timeout=t) for e in need})
                except Exception:
                    return None
                rows.update(got)
                if self.get is None:
                    for e, r in got.items():
                        _cache[e] = (time.time() + CACHE_S, r)
            self._rows = rows
        return self._rows

    def _attendees(self, start_ms: int, end_ms: int):
        """(ok, lowercased attendee strings of the event at that time or None)."""
        import datetime as dt

        from . import attendance
        day = dt.datetime.fromtimestamp(start_ms / 1000, attendance.TZ).date()
        if day not in self._days:
            try:
                self._days[day] = self._call(lambda t: attendance.events_on(day, get=self.get) if self.get
                                             else attendance.events_on(day))
            except Exception:
                self._days[day] = None
        events = self._days[day]
        if events is None:
            return False, None
        ev = attendance.event_for(events, start_ms, end_ms)
        if ev is not None and ev.with_owner is False:
            ev = None       # someone else's event on the calendar: it says nothing about who was there
        return True, ({a.strip().lower() for a in ev.attendees} if ev else None)

    def priors(self, sp_cfg: dict, names, span: tuple[int, int] | None = None) -> dict[str, float]:
        """{name: prior} for the enrolled `names` linked to a LifeOS person, with the calendar
        bonus when `span` (start_ms, end_ms) falls in an event listing them; {} when LifeOS
        can't answer within the run's budget."""
        from . import attendance, config
        links = {n: config.linked_entity(sp_cfg.get("people"), n) for n in names}
        links = {n: e for n, e in links.items() if e}
        if not links:
            return {}
        rows = self._people({n: e for n, e in (sp_cfg.get("people") or {}).items() if isinstance(e, str) and e})
        if rows is None:
            return {}
        attendees = None
        if span is not None:
            ok, attendees = self._attendees(*span)
            if not ok:
                return {}
        cfg = sp_cfg.get("closeness")
        out = {}
        for name, eid in links.items():
            row = rows.get(eid)
            if row is None:
                continue
            keys = attendance._keys({"email": row.get("email"), "canonical_name": row.get("name"),
                                     "display_name": row.get("name"), "aliases": row.get("aliases")})
            out[name] = prior(row.get("strength"), row.get("days_since_contact"), bool(attendees and keys & attendees), cfg)
        return out


def load_priors(sp_cfg: dict, names, attendees: set[str] | None = None, get=None, run: PriorRun | None = None) -> dict[str, float]:
    """{name: prior} for linked enrolled `names` (no calendar lookup; `attendees` may be given
    by a caller that has them); {} when LifeOS can't answer. One-shot form of PriorRun."""
    run = run or PriorRun(get=get)
    out = run.priors(sp_cfg, names)
    if attendees:
        from . import attendance, config
        for n in list(out):
            row = (run._rows or {}).get(config.linked_entity(sp_cfg.get("people"), n)) or {}
            keys = attendance._keys({"email": row.get("email"), "canonical_name": row.get("name"),
                                     "display_name": row.get("name"), "aliases": row.get("aliases")})
            if keys & attendees:
                out[n] += float({**DEFAULTS, **(sp_cfg.get("closeness") or {})}["weight_calendar"])
    return out
