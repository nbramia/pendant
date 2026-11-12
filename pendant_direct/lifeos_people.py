"""LifeOS people (localhost:8000) for naming: suggestions and links.

Names come from the CRM people list (`/api/crm/people`, sorted by relationship strength,
with the Dunbar circle and category) and `/api/crm/config` (`my_person_id`, the wearer).
Everything stays on this machine. A LifeOS person is only ever a suggestion: picking
one in the app (source "user") is what allows the name and links it. A person picked
from LifeOS is named by their full canonical name. Stdlib only: the feed imports it.
"""
from __future__ import annotations

import re
import time
import unicodedata

from . import attendance, config

MAX_SUGGESTIONS = 20
SEARCH_LIMIT = 100
DOMINANT_CIRCLE = 2      # a first name links on its own only to someone this close ...
DOMINANT_RATIO = 1.5     # ... and this many times stronger than the next person with that first name
# The LifeOS web app, as the phone reaches it (`lifeos_url` in config.json; person links open there)
WEB = str(config.load(config.data_dir()).get("lifeos_url") or "http://localhost:8000").rstrip("/")
NO_REPLY = re.compile(r"no-?reply", re.I)


def _api(path: str, params: dict, timeout: float = 10.0) -> dict:
    return attendance._get(path, params, timeout=timeout)   # looked up per call (tests block it)


def _caller(get, timeout: float):
    """`get(path, params)`: the injected fake, or the live API with this timeout."""
    return get or (lambda path, params: _api(path, params, timeout))


def _row(p: dict) -> dict | None:
    """The fields the app and the prior need, or None for a record that can't be a speaker name."""
    name = p.get("display_name") or p.get("canonical_name")
    eid = p.get("entity_id")
    try:
        name = config.clean_name(name)
    except ValueError:
        return None
    if not isinstance(eid, str) or not config.ENTITY_RE.match(eid) or NO_REPLY.search(p.get("email") or ""):
        return None
    canon = p.get("canonical_name")
    try:
        canon = config.clean_name(canon)
    except ValueError:
        canon = None
    return {"name": name, "canonical": canon, "company": p.get("company"), "entity_id": eid, "strength": p.get("relationship_strength"),
            "days_since_contact": p.get("days_since_contact"), "category": p.get("category"),
            "email": p.get("email"), "aliases": p.get("aliases") or []}


def speaker_name(*candidates) -> str | None:
    """The first candidate that can be a speaker name (accents folded, other characters dropped)."""
    for c in candidates:
        if not c:
            continue
        folded = unicodedata.normalize("NFKD", str(c)).encode("ascii", "ignore").decode()
        folded = re.sub(r"[^A-Za-z '-]", "", folded)
        try:
            return config.clean_name(folded)
        except ValueError:
            continue
    return None


def crm_row(p: dict) -> dict | None:
    """A CRM people record as the app and the matcher use it, or None when it can't be a speaker."""
    eid = p.get("id")
    full = " ".join(str(p.get("canonical_name") or p.get("display_name") or "").split())
    label = speaker_name(p.get("canonical_name"), p.get("display_name"))
    emails = [e for e in (p.get("emails") or []) if isinstance(e, str)]
    if not full or not label or not isinstance(eid, str) or not config.ENTITY_RE.match(eid) \
            or any(NO_REPLY.search(e) for e in emails):
        return None
    return {"entity_id": eid, "name": full, "label": label, "circle": p.get("dunbar_circle"),
            "strength": p.get("relationship_strength") or 0.0, "category": p.get("category"),
            "company": p.get("company"), "email": emails[0] if emails else None, "emails": emails,
            "display": p.get("display_name")}


def _rank(r: dict) -> tuple:
    return (r["circle"] if isinstance(r["circle"], int) else 99, -(r["strength"] or 0.0), r["name"].lower())


def people(q: str = "", limit: int = SEARCH_LIMIT, get=None, timeout: float = 10.0) -> list[dict]:
    """CRM people matching q (empty q: the whole list's top), closest circle first, then strongest.
    Raises when LifeOS can't answer."""
    data = _caller(get, timeout)("/api/crm/people", {"q": q.strip(), "sort": "strength", "limit": limit})
    return sorted(filter(None, map(crm_row, data.get("people") or [])), key=_rank)


def my_person_id(get=None, timeout: float = 10.0) -> str | None:
    """LifeOS's own "self" person (the wearer), or None."""
    pid = _caller(get, timeout)("/api/crm/config", {}).get("my_person_id")
    return pid if isinstance(pid, str) and config.ENTITY_RE.match(pid) else None


def person(entity_id: str, get=None, timeout: float = 10.0) -> dict:
    """One CRM person by id; raises when LifeOS can't answer or the person is unusable."""
    row = crm_row(_caller(get, timeout)(f"/api/crm/people/{entity_id}", {}))
    if row is None:
        raise ValueError("no such person")
    return row


_cache: dict[str, tuple[float, dict]] = {}
_down_until = 0.0
CACHE_S, FAIL_S = 60.0, 30.0


def lookup(entity_ids, get=None) -> dict[str, dict]:
    """{entity_id: row} for those LifeOS answers for (cached a minute; LifeOS down for a
    while: nothing, without waiting again)."""
    global _down_until
    out, now = {}, time.time()
    for eid in set(entity_ids):
        hit = _cache.get(eid) if get is None else None
        if hit and hit[0] > now:
            out[eid] = hit[1]
        elif now >= _down_until or get is not None:
            try:
                row = person(eid, get=get, timeout=3.0)
            except ValueError:
                continue
            except Exception:
                if get is None:
                    _down_until = now + FAIL_S
                continue
            if get is None:
                _cache[eid] = (now + CACHE_S, row)
            out[eid] = row
    return out


def entity(entity_id: str, get=None, timeout: float = 10.0) -> dict:
    """One LifeOS person by entity_id; raises when LifeOS can't answer."""
    p = _caller(get, timeout)(f"/api/people/entity/{entity_id}", {})
    row = _row(p)
    if row is None:
        raise ValueError("no such person")
    return row


def distinct_name(row: dict, taken) -> str:
    """The speaker name for a LifeOS person: their full name, unless someone else holds it
    (`taken`: lowercased name -> entity_id). Then the name plus the first letters
    of their company (or email domain), else a letter."""
    label = row["label"]

    def free(n):
        return taken.get(n.lower()) in (None, row["entity_id"])
    if free(label):
        return label
    src = re.sub(r"[^A-Za-z]", "", row.get("company") or "") or \
        re.sub(r"[^A-Za-z]", "", (row.get("email") or "").split("@")[-1].split(".")[0])
    for n in range(1, len(src) + 1):
        cand = f"{label} {src[:n].capitalize() if n > 1 else src[:1].upper()}"
        if len(cand) <= 40 and free(cand):
            return cand
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if free(f"{label} {letter}"):
            return f"{label} {letter}"
    return label


def detail(row: dict) -> str | None:
    """What tells two people of one name apart in the picker: company, else email domain."""
    if row.get("company"):
        return row["company"]
    e = row.get("email") or ""
    return e.split("@")[-1] if "@" in e else None


def suggest(q: str, enrolled: list[dict], allowed: list[str], people_links: dict, get=None) -> dict:
    """Name-picker rows for `q`: LifeOS people first (closest circle, then strongest), then known
    voices not among them, then what was typed as a new name. With nothing typed the people
    already tagged (known voices, enrolled most-recognized first, then allowed) come first, then
    the closest LifeOS circles. Each row: name (full), label (the
    speaker name they would get: their full name, or the name they are already linked under),
    entity_id, circle, category, strength, detail, voiceprint, known, new. `known` marks a
    voice already allowed or enrolled under that label. With LifeOS unreachable only the known
    voices and the typed name come back (`lifeos` false)."""
    ql = q.strip().lower()
    links = {str(k): v for k, v in (people_links or {}).items() if isinstance(v, str)}
    by_entity = {v: k for k, v in links.items()}
    voices: dict[str, tuple[str, bool]] = {}      # lowered name -> (spelling, has voiceprint)
    for name, vp in [(e["name"], True) for e in enrolled] + [(a, False) for a in allowed]:
        spelling, had = voices.get(name.lower(), (name, False))
        voices[name.lower()] = (spelling, had or vp)
    taken = {k.lower(): v for k, v in links.items()}
    used: set[str] = set()
    out: list[dict] = []

    def blank(**kw):
        return {"name": None, "label": None, "entity_id": None, "circle": None, "category": None, "strength": None,
                "detail": None, "voiceprint": False, "known": False, "new": False, **kw}
    lifeos = True
    try:
        rows = people(q, get=get)[:MAX_SUGGESTIONS]
    except Exception:
        rows, lifeos = [], False
    for r in rows:
        label = by_entity.get(r["entity_id"])
        if label is None:
            label = distinct_name(r, {**taken, **{u: "-" for u in used}})
        if label.lower() in used:
            continue
        used.add(label.lower())
        spelling, vp = voices.get(label.lower(), (label, False))
        out.append(blank(name=r["name"], label=label, entity_id=r["entity_id"], circle=r["circle"], category=r["category"],
                         strength=r["strength"], detail=detail(r), voiceprint=vp, known=label.lower() in voices))
    for key, (name, vp) in voices.items():
        if key in used or (ql and ql not in key):
            continue
        used.add(key)
        out.append(blank(name=name, label=name, entity_id=config.linked_entity(links, name), voiceprint=vp, known=True))
    if not ql:      # nothing typed: who is already tagged first, in the roster's order
        order = {k: i for i, k in enumerate(voices)}
        out.sort(key=lambda r: order[r["label"].lower()] if r["known"] else len(order))
    try:
        typed = config.clean_name(q)
    except ValueError:
        typed = None
    if typed and typed.lower() not in used and typed.lower() not in {x["name"].lower() for x in out}:
        out.append(blank(name=typed, label=typed, new=True))
    return {"people": out, "lifeos": lifeos}


def _dominant(rows: list[dict]) -> dict | None:
    """The one person who clearly wins among people sharing a name: in a close circle and
    well ahead (by relationship strength) of the next, or the only one."""
    rows = sorted(rows, key=lambda r: -(r["strength"] or 0.0))
    if not rows:
        return None
    top = rows[0]
    runner_up = (rows[1]["strength"] or 0.0) if len(rows) > 1 else 0.0
    lead = top["strength"] or 0.0
    # a strict lead: with no strength at all (or a tie) nobody wins
    if isinstance(top["circle"], int) and top["circle"] <= DOMINANT_CIRCLE \
            and lead > 0 and lead > runner_up and lead >= DOMINANT_RATIO * runner_up:
        return top
    return None


def choose_links(names: list[str], wearer: str | None = None, get=None) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """({name: person}, {name: candidates}) for names with no link yet. The wearer's name links to
    LifeOS's own "self" person. A name with a surname links on a full-name match; a first name
    only when one person with that first name clearly wins (_dominant). Anything else is
    ambiguous: no link, and the closest candidates are returned so they can be shown."""
    chosen: dict[str, dict] = {}
    ambiguous: dict[str, list[dict]] = {}
    me = my_person_id(get) if wearer else None
    for name in names:
        key = name.lower()
        if wearer and key == wearer.lower() and me:
            chosen[name] = person(me, get=get)
            continue
        cands = people(name, get=get)
        if len(key.split()) > 1:
            pool = [r for r in cands if key in (r["name"].lower(), (r["display"] or "").lower(), r["label"].lower())]
            win = pool[0] if len(pool) == 1 else _dominant(pool)
        else:
            pool = [r for r in cands if r["name"].lower().split()[0] == key]
            win = _dominant(pool)
        if win:
            chosen[name] = win
        else:
            ambiguous[name] = (pool or cands)[:3]
    return chosen, ambiguous


def person_url(entity_id: str) -> str:
    """The person's page in the LifeOS web app."""
    return f"{WEB}/crm/{entity_id}"
