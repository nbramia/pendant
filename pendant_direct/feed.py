"""JSON feed for the iPhone app: days, conversations, talk-time stats, and
speaker labeling.

GET  /pendant/feed/days                    -> recent days with conversation counts and minutes
GET  /pendant/feed/day/YYYY-MM-DD          -> that day's conversations (title, overview,
                                              action items, notable, kind, importance, stars as HH:MM,
                                              `meetings`: every linked meeting note (`shared` when only another
                                              part of the meeting overlaps it), `calendar`: the matched event
                                              with attendees resolved to LifeOS people)
                                              + talk minutes per hour
GET  /pendant/feed/people?days=7           -> named speakers (wearer and generic tags excluded) with talk
                                              minutes, conversations, last heard, share, and entity_id +
                                              lifeos_url (+ lifeos_name, lifeos_circle, lifeos_category when LifeOS
                                              answers) for people linked to LifeOS (speakers.people); days 1..90
GET  /pendant/feed/people/suggest?q=       -> name-picker rows: LifeOS people first (CRM list, closest circle then
                                              strength; empty q lists the closest circles), then known voices not
                                              among them, then the typed name (`new`: true). Each has name (full),
                                              label (the speaker name they'd get), entity_id, circle, category,
                                              strength, detail, voiceprint, known, new; `lifeos` false when LifeOS
                                              is down (known voices and the typed name only)
POST /pendant/feed/people/<name>/link      {"entity_id"} -> point that speaker name at a different LifeOS person
                                              (the explicit relink; labels and voiceprints keep the name)
GET  /pendant/feed/people/<name>?days=30   -> that person's conversations, newest first (404 if never heard)
GET  /pendant/feed/interactions?from=YYYY-MM-DD&to=YYYY-MM-DD -> a text-free view of who the wearer spent time
                                              with, for LifeOS (400 on a malformed date, from after to, or more than
                                              31 days). {from, to, scoring, match_threshold, conversations: [{device, start_ms, end_ms, day,
                                              kind, calendar_event: null | {id, start_ms, end_ms, inherited,
                                              attendees: [{entity_id, wearer}]}, meeting_notes: [note], speakers:
                                              [{entity_id, name, labeled_by, label_kind, speech_s, score, wearer}]}]},
                                              one row per conversation (deleted ones excluded), oldest first. A
                                              speaker is one named person (several voices of one person merge;
                                              `labeled_by` is the strongest of user, meeting, calendar, match;
                                              `entity_id` null without a LifeOS link; `score` the best stored
                                              voiceprint score among the person's auto-matched voices, else null).
                                              `scoring` is the configured mode (mean, topk or calibrated) and
                                              `match_threshold` the cutoff the matcher applies on that mode's scale
                                              (s-norm for calibrated, a cosine otherwise), so a consumer's score
                                              floor can be chosen relative to it; scores are on that scale too
                                              (a person with a small cohort scores as a raw cosine).
                                              Unnamed, stranger, noise and dismissed voices are left out. Never
                                              carries transcript text, titles, overviews, action items or snippets
GET  /pendant/feed/status                  -> per pendant (with its name): last audio; last
                                              upload, backlog, the {id: name} map, and llm_today / llm_7d
                                              (DeepSeek calls, tokens, estimated USD)
GET  /pendant/feed/speakers                -> enrolled voiceprints (name, count, recognized), most
                                              recognized first, + allowed names
GET  /pendant/feed/speakers/unknown        -> Voice Scoring candidates, least confident first
                                              (pendant_direct.voices): current_label, confidence,
                                              excerpt, when, clip_id; `suggested` for the likely wearer
GET  /pendant/feed/speakers/clip/<id>      -> that speaker's snippet as audio/wav
GET  /pendant/feed/conversations/<device>/<start_ms>/speaker/<tag>/suggest -> who that voice probably is from the
                                              conversation's calendar event (pendant_direct.calendar_hint):
                                              `likely` (name, label, entity_id, source "calendar", event) when
                                              exactly one attendee is left, else `candidates`; and the `event`.
                                              Then `maybe` (name, label, entity_id, reasons, source "lifeos"):
                                              LifeOS's likely-present people for the conversation's window, minus
                                              the wearer, people named in it, the voice's name and the calendar's;
                                              empty when LifeOS can't answer within 3 s
                                              Voice Scoring / Who's this cards carry the same likely/candidates,
                                              and `conversation` {device, start_ms, tag} with `hints` {likely,
                                              candidates, maybe}: LifeOS asked once per distinct window for the
                                              page, which waits for it 3 s at most (calendar hints only after)
GET  /pendant/feed/conversations/<device>/<start_ms>/audio -> the conversation's clip as audio/mp4 (AAC/M4A
                                              transcoded from the Ogg/Opus clip once and cached, pendant_direct.audio);
                                              honours Range (206, 416); 404 once the clip is gone
POST /pendant/feed/speakers/<id>/label     {"name": "Alex", "source": "user"?} -> confirm or
                                              correct: moves the sample between voiceprints and
                                              relabels the transcript (403 unless the name is in
                                              speakers.allowed or source is "user")
     The label, reassign and enroll bodies may carry `entity_id` (a LifeOS person the wearer picked):
     with source "user" it is stored in speakers.people, nothing else.
POST /pendant/feed/speakers/<id>/stranger  -> Remove name: kept as a stranger to block matches; the transcript
                                              calls the voice a fresh "Speaker X" (`name`)
POST /pendant/feed/speakers/<id>/noise     -> not speech: a noise exemplar, never offered again
POST /pendant/feed/speakers/<id>/mixed     -> two people in one clip: never offered again, taken out of any
                                              voiceprint and never a sample, stranger or noise exemplar; the
                                              transcript keeps its label
POST /pendant/feed/speakers/<id>/skip      -> never offered again (no other change)
POST /pendant/feed/speakers/<id>/not       -> "Not <name>" on a voice a sitting or bulk answer named: the name comes
                                              off (a fresh "Speaker X"), never offered again, nothing learned
POST /pendant/feed/speakers/<id>/dismiss   -> stop asking about that speaker
POST /pendant/feed/tasks                   {"device_id", "start_ms", "item"} -> creates a LifeOS task
                                              linked to that conversation (once per item; 502 and
                                              kept for retry if LifeOS is down); the day feed lists
                                              each conversation's `tasked` items
     A reassignment's answer carries `change_id` and `undo_until` (epoch seconds).
POST /pendant/feed/reassign/<change_id>/undo -> put back everything that reassignment changed
                                              (pendant_direct.reassign_undo): {"undone": true,
                                              "already"}; 409 when its conversation or voices changed
                                              since, 410 after 24 h, 503 while the processor runs
POST /pendant/feed/conversations/<device>/<start_ms>/star   {"on": true|false} -> star it from the app
                                              (a notable override; button-press stars stay), Event note
                                              and Highlights updated
POST /pendant/feed/conversations/<device>/<start_ms>/delete -> move it to the 7-day trash (pendant_direct.trash)
POST /pendant/feed/conversations/<device>/<start_ms>/restore -> put it back (no-op if not deleted)
GET  /pendant/feed/trash                   -> recently deleted recordings with deletion and purge times
POST /pendant/feed/enroll                  {"name", "device", "start_ms", "end_ms"} -> {"id"}: the app's "Record a
                                              voice sample" (pendant_direct.enroll); the processor judges the
                                              pendant's audio for that window and keeps it out of the vault
POST /pendant/feed/enroll/<id>/phone       raw WAV/M4A body (<= 60 s): the phone-mic copy of that reading, judged at
                                              once -> the status with `phone: {status, samples|reason}`
GET  /pendant/feed/enroll/<id>             -> {"status": "waiting"} | {"status": "added", "samples": n} |
                                              {"status": "rejected", "reason": ...}
GET  /pendant/feed/day-signals?day=YYYY-MM-DD -> the day's quiet Dailies signal (pendant_direct.day_signals):
                                              {day, emotional_moment: null | {path, valence, reason, confidence, at},
                                              resonant, words, computed_at}; 404 when not computed; 400 on a bad day.
                                              Read-only, shown nowhere in the app
POST /pendant/feed/conversations/<device>/<start_ms>/calendar/mute   -> "Not with them": the conversation's
                                              calendar event series says nothing about who the wearer was
                                              with (no calendar or LifeOS guess from it, no voiceprint learned
                                              from it): {"muted", "series", "event"}; .../calendar/unmute undoes it
POST /pendant/feed/health                  {"pendants": [{"device", "battery", "free_pages",
                                              "total_pages"}]} -> low-battery / full-memory
                                              Telegram alerts (pendant_direct.health)

Same bearer token as ingest. Binds to localhost; Tailscale Serve exposes it at
:8443/pendant/feed. Reads the vault's daily files (the source of truth for
text) and pages.db; the only writes are speaker answers (pages.db
`speaker_clusters` and the Voice Scoring tables, `enrollments` rows for voice-sample readings, `voiceprints/`, and speaker
tags in a conversation's Raw section, applied under the processor's lock), app stars
(`notable_overrides.json`, Event notes, Highlights), delete/restore (`trash.py`) and reassignment undo records (`reassign_undo/`, `reassign_undo.py`), both under that lock. The speaker endpoints import numpy and
PyAV lazily, so the service must run from .venv:

  .venv/bin/python -m pendant_direct.feed
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from . import audio
from . import calendar_hint
from . import config as cfgmod
from . import enroll
from . import health
from . import lifeos_people
from . import names as pnames
from . import reassign_undo
from . import tasks
from . import trash

TZ = ZoneInfo("America/New_York")
FRAME_S = 0.02
STAMP = re.compile(r"\[\d{1,2}:\d{2}\]|\*\*[^*\n]{1,40}:\*\*|⭐")  # not words: [HH:MM], **Speaker:**, stars
# `## HH:MM — Title (XmYYs) · <pendant name or, in older files, its hex ID>`
HEADING = re.compile(
    r"^## (?P<time>\d{1,2}:\d{2}) — (?P<title>.+?) \((?P<m>\d+)m(?P<s>\d{2})s\)(?: · (?P<device>.+?))?\s*$")
MAX_BODY = 4096
MAX_PHONE_BODY = 8 * 1024 * 1024   # 60 s of 16 kHz mono WAV is about 2 MB
SNIPPET_MAX_S = 15.0


def data_dir() -> Path:
    return Path(os.environ.get("PENDANT_DATA", Path.home() / "pendant-direct-data")).expanduser()


def notes_dir() -> Path:
    return cfgmod.notes_dir(data_dir())


def token_file() -> Path:
    return Path(os.environ.get("PENDANT_TOKEN_FILE",
                               Path.home() / ".config" / "pendant-direct" / "token")).expanduser()


def parse_daily(text: str) -> list[dict]:
    """Split a daily file into conversations written by pendant_direct.transcribe. The
    `*Calendar:*` line is left out of the text: the feed gives the match as `calendar`."""
    convs: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        m = HEADING.match(line)
        if m:
            cur = {"time": m["time"], "title": m["title"], "device": m["device"],
                   "duration_s": int(m["m"]) * 60 + int(m["s"]), "heading": line[3:].strip(), "lines": []}
            convs.append(cur)
        elif cur is not None and line.strip() not in ("", "---") and not line.startswith("*Calendar: "):
            cur["lines"].append(line.strip())
    for c in convs:
        c["text"] = "\n".join(c.pop("lines"))
        c["words"] = len(STAMP.sub("", c["text"]).split())
    return convs


def hourly_minutes(day: dt.date) -> list[float]:
    """Captured audio minutes per local hour, from pages.db."""
    db = data_dir() / "pages.db"
    hours = [0.0] * 24
    if not db.exists():
        return hours
    start = dt.datetime.combine(day, dt.time(), TZ)
    lo, hi = int(start.timestamp() * 1000), int((start + dt.timedelta(days=1)).timestamp() * 1000)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT ts_ms, COUNT(*) FROM chunks WHERE ts_ms >= ? AND ts_ms < ? GROUP BY ts_ms",
                           (lo, hi)).fetchall()
    finally:
        con.close()
    for ts, n in rows:
        hours[dt.datetime.fromtimestamp(ts / 1000, TZ).hour] += n * FRAME_S / 60
    return [round(h, 2) for h in hours]


def connect(write: bool = False) -> sqlite3.Connection | None:
    db = data_dir() / "pages.db"
    if not db.exists():
        return None
    if write:
        return sqlite3.connect(db, timeout=15)
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15)


def conversation_rows(day: dt.date) -> dict[str, dict]:
    """pages.db `conversations` rows for a day, keyed by their Raw heading text."""
    con = connect()
    if con is None:
        return {}
    con.row_factory = sqlite3.Row
    rows, stars, tasked, tags, overrides = [], {}, {}, {}, {}
    from . import highlights as hl
    app_stars = hl.load_overrides(data_dir() / "notable_overrides.json")
    try:
        rows = con.execute("SELECT * FROM conversations WHERE day = ?", (f"{day:%Y-%m-%d}",)).fetchall()
        for r in con.execute(
                "SELECT s.conv_device, s.conv_start_ms, s.ts_ms FROM stars s JOIN conversations c "
                "ON c.device = s.conv_device AND c.start_ms = s.conv_start_ms WHERE c.day = ? ORDER BY s.ts_ms",
                (f"{day:%Y-%m-%d}",)):
            stars.setdefault((r[0], r[1]), []).append(r[2])
        for r in con.execute("SELECT t.device, t.start_ms, t.item FROM task_requests t JOIN conversations c "
                             "ON c.device = t.device AND c.start_ms = t.start_ms WHERE c.day = ? "
                             "AND t.status IN ('created','pending') ORDER BY t.created_at",
                             (f"{day:%Y-%m-%d}",)):
            tasked.setdefault((r[0], r[1]), []).append(r[2])
        for r in con.execute("SELECT s.device, s.start_ms, s.tag, s.id FROM speaker_clusters s JOIN conversations c "
                             "ON c.device = s.device AND c.start_ms = s.start_ms WHERE c.day = ? ORDER BY s.id",
                             (f"{day:%Y-%m-%d}",)):
            tags.setdefault((r[0], r[1]), {}).setdefault(r[2], []).append(r[3])
        for r in con.execute("SELECT DISTINCT t.device, t.start_ms, t.para_index FROM transcript_relabels t "
                             "JOIN conversations c ON c.device = t.device AND c.start_ms = t.start_ms "
                             "WHERE c.day = ? AND t.para_index IS NOT NULL", (f"{day:%Y-%m-%d}",)):
            overrides.setdefault((r[0], r[1]), []).append(r[2])
    except sqlite3.OperationalError:  # older database without the tables
        pass
    finally:
        con.close()
    # a tag shared by two clusters can't be told apart in the text: it maps to no cluster
    return {r["raw_heading"]: {**dict(r), "stars": stars.get((r["device"], r["start_ms"]), []),
                               "tasked": tasked.get((r["device"], r["start_ms"]), []),
                               "line_overrides": sorted(overrides.get((r["device"], r["start_ms"]), [])),
                               "app_star": app_stars.get(f"{r['device']}-{r['start_ms']}") is True,
                               "speaker_tags": {t: ids[0] for t, ids in tags.get((r["device"], r["start_ms"]), {}).items()
                                                if t and len(ids) == 1}} for r in rows}


def _offsets(raw) -> list | None:
    """[[start_s, end_s], ...] per stamped paragraph, or None when not recorded."""
    try:
        v = json.loads(raw) if raw else None
    except ValueError:
        return None
    return v if isinstance(v, list) else None


def _reasons(raw) -> list[str]:
    try:
        v = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [str(i) for i in v] if isinstance(v, list) else []


def _meetings(r: dict) -> list[dict]:
    """[{note, source, shared}]: every meeting note linked to the conversation (`shared`: only
    another part of the same meeting overlaps it)."""
    try:
        v = json.loads(r.get("meeting_notes") or "null")
    except ValueError:
        v = None
    if isinstance(v, list):
        return [{"note": n["note"], "source": n.get("source") or "", "shared": n.get("via") == "continuation"}
                for n in v if isinstance(n, dict) and isinstance(n.get("note"), str)]
    return [{"note": r["meeting_note"], "source": r.get("meeting_source") or "", "shared": False}] \
        if r.get("meeting_note") else []


def enrich(convs: list[dict], rows: dict[str, dict]) -> list[dict]:
    """Add title/overview/action_items/notable from the DB (empty for sections
    written before summaries existed). `device` stays as written in the heading."""
    for c in convs:
        r = rows.get(c.pop("heading"), {})
        try:
            items = json.loads(r.get("action_items") or "[]")
        except ValueError:
            items = []
        c.update({"overview": r.get("overview") or "", "action_items": items,
                  "notable": bool(r.get("notable")), "category": r.get("category") or "",
                  "kind": r.get("kind") or "conversation", "importance": r.get("importance") or 0,
                  "kind_reasons": _reasons(r.get("kind_reasons")),
                  "device_id": r.get("device"), "start_ms": r.get("start_ms"),
                  "tasked": r.get("tasked", []),
                  "speaker_tags": r.get("speaker_tags", {}),
                  "line_overrides": r.get("line_overrides", []),
                  "app_star": bool(r.get("app_star")),
                  "paragraph_offsets": _offsets(r.get("paragraph_offsets")),
                  "event": (r.get("event_file") or "").removesuffix(".md") or None,
                  "meeting": r.get("meeting_note"), "meeting_source": r.get("meeting_source"),
                  "meetings": _meetings(r),
                  "calendar": calendar_hint.event_view(r.get("calendar_event")),
                  "stars": [f"{dt.datetime.fromtimestamp(ts / 1000, TZ):%H:%M}" for ts in r.get("stars", [])]})
    return convs


INTERACTIONS_MAX_DAYS = 31
LABEL_RANK = {"user": 0, "meeting": 1, "calendar": 2, "match": 3}   # strongest first


def _interaction_event(raw) -> dict | None:
    """The stored calendar match without its title: id, times, whether another part of the
    meeting supplied it, and attendees as LifeOS ids."""
    ev = calendar_hint.stored(raw)
    if ev is None:
        return None
    return {"id": ev.get("id"), "start_ms": ev.get("start_ms"), "end_ms": ev.get("end_ms"),
            "inherited": ev.get("via") == "continuation",
            "attendees": [{"entity_id": a.get("entity_id"), "wearer": bool(a.get("wearer"))}
                          for a in ev.get("attendees") or [] if isinstance(a, dict) and a.get("name")]}


def _interaction_speakers(clusters: list[dict], links, wearer: str) -> list[dict]:
    """One entry per named person among a conversation's voices."""
    people: dict[str, dict] = {}
    for c in clusters:
        label = (c["label"] or "").strip()
        if (not label or c["noise"] or c["label_kind"] in ("stranger", "noise")
                or c["labeled_by"] == "dismissed" or c["labeled_by"] not in LABEL_RANK):
            continue
        p = people.setdefault(label.lower(), {
            "entity_id": cfgmod.linked_entity(links, label), "name": label, "labeled_by": c["labeled_by"],
            "label_kind": c["label_kind"], "speech_s": 0.0, "score": None,
            "wearer": label.lower() == wearer.lower()})
        p["speech_s"] += float(c["speech_s"] or 0)
        if LABEL_RANK[c["labeled_by"]] < LABEL_RANK[p["labeled_by"]]:
            p["labeled_by"], p["label_kind"] = c["labeled_by"], c["label_kind"]
        if c["labeled_by"] == "match" and c["match_score"] is not None:
            p["score"] = c["match_score"] if p["score"] is None else max(p["score"], c["match_score"])
    out = list(people.values())
    for p in out:
        p["speech_s"] = round(p["speech_s"], 1)
        if p["labeled_by"] != "match":   # a score describes an auto-match, not a hand or calendar label
            p["score"] = None
    return sorted(out, key=lambda p: (-p["speech_s"], p["name"].lower()))


def interactions_payload(start: dt.date, end: dt.date) -> dict:
    """Conversations whose day is in start..end with their named speakers; no text of any kind.

    A database that can't be read raises sqlite3.Error rather than answering with an empty
    range, which a consumer would take for days with nobody in them."""
    sp = cfgmod.load(data_dir())["speakers"]
    out = {"from": f"{start:%Y-%m-%d}", "to": f"{end:%Y-%m-%d}", "scoring": cfgmod.scoring_mode(sp),
           "match_threshold": cfgmod.effective_match_threshold(sp), "conversations": []}
    con = connect()
    if con is None:  # no database yet: unknown, not empty
        raise sqlite3.OperationalError("no database")
    con.row_factory = sqlite3.Row
    try:
        convs = con.execute("SELECT device, start_ms, end_ms, day, kind, calendar_event, meeting_notes, "
                            "meeting_note, meeting_source FROM conversations WHERE day BETWEEN ? AND ? "
                            "ORDER BY start_ms, device", (out["from"], out["to"])).fetchall()
        clusters: dict[tuple, list[dict]] = {}
        cols = {c[1] for c in con.execute("PRAGMA table_info(speaker_clusters)")}
        score = "s.match_score" if "match_score" in cols else "NULL"   # until the processor migrates
        for r in con.execute("SELECT s.device, s.start_ms, s.label, s.labeled_by, s.label_kind, s.speech_s, "
                             f"{score} AS match_score, COALESCE(s.noise, 0) AS noise FROM speaker_clusters s "
                             "JOIN conversations c ON c.device = s.device AND c.start_ms = s.start_ms "
                             "WHERE c.day BETWEEN ? AND ? ORDER BY s.id", (out["from"], out["to"])):
            clusters.setdefault((r["device"], r["start_ms"]), []).append(dict(r))
    finally:
        con.close()
    for r in convs:
        r = dict(r)
        out["conversations"].append({
            "device": r["device"], "start_ms": r["start_ms"], "end_ms": r["end_ms"], "day": r["day"],
            "kind": r["kind"] or "conversation",
            "calendar_event": _interaction_event(r["calendar_event"]),
            "meeting_notes": [m["note"] for m in _meetings(r)],
            "speakers": _interaction_speakers(clusters.get((r["device"], r["start_ms"]), []),
                                              sp.get("people"), cfgmod.wearer(sp))})
    return out


def day_payload(day: dt.date) -> dict:
    f = notes_dir() / f"{day:%Y-%m-%d}.md"
    convs = enrich(parse_daily(f.read_text()), conversation_rows(day)) if f.exists() else []
    return {"date": f"{day:%Y-%m-%d}", "conversations": convs, "hourly_minutes": hourly_minutes(day),
            "talk_minutes": round(sum(c["duration_s"] for c in convs) / 60, 1),
            "words": sum(c["words"] for c in convs)}


SPEAKER_LABEL = re.compile(r"\*\*([^*\n]{1,40}):\*\*")


def snippet(text: str, terms: list[str], width: int = 180) -> str:
    """Text around the first match of any term, with ellipses. Speaker labels keep their
    `**Name:**` markup (the app shows them as chips); a cut never lands inside one."""
    plain = re.sub(r"\[\d{1,2}:\d{2}\]\s*(?:⭐\s*)?", "", text).replace("\n", " ")
    low = plain.lower()
    hits = [(low.find(t), len(t)) for t in terms if t and low.find(t) >= 0]
    at, n = min(hits) if hits else (0, 0)
    start = max(0, at - width // 3)
    end = min(len(plain), start + width)
    for m in SPEAKER_LABEL.finditer(plain):
        hit = m.start() < at + n and at < m.end()   # the match touches this label: keep it whole
        if m.start() < start < m.end(): start = m.start() if hit else m.end()
        if m.start() < end < m.end(): end = m.end() if hit else m.start()
    return ("…" if start else "") + plain[start:end].strip() + ("…" if end < len(plain) else "")


def search_payload(q: str = "", *, start: dt.date | None = None, end: dt.date | None = None,
                   device: str | None = None, speaker: str | None = None, notable: bool = False,
                   actions: bool = False, min_minutes: float = 0, limit: int = 100) -> dict:
    """Conversations matching every word of `q` (title, overview, transcript,
    action items) and the filters, newest first. Days default to the last 90."""
    end = end or dt.datetime.now(TZ).date()
    start = start or end - dt.timedelta(days=90)
    terms = [t for t in q.lower().split() if t]
    results = []
    for f in sorted(notes_dir().glob("????-??-??.md"), reverse=True):
        try:
            day = dt.date.fromisoformat(f.stem)
        except ValueError:
            continue
        if not start <= day <= end:
            continue
        for c in enrich(parse_daily(f.read_text()), conversation_rows(day)):
            if device and (c.get("device") or "").lower() != device.lower():
                continue
            if speaker and f"**{speaker.lower()}:**" not in c["text"].lower():
                continue
            if notable and not c["notable"]:
                continue
            if actions and not c["action_items"]:
                continue
            if c["duration_s"] < min_minutes * 60:
                continue
            hay = " ".join([c["title"], c["overview"], c["text"], *c["action_items"]]).lower()
            if not all(t in hay for t in terms):
                continue
            results.append({**c, "date": f.stem, "snippet": snippet(c["text"], terms)})
        results.sort(key=lambda r: (r["date"], r["time"]), reverse=True)
        if len(results) >= limit:
            break
    return {"query": q, "results": results[:limit], "count": len(results[:limit])}


def days_payload(limit: int = 30) -> dict:
    out = []
    for f in sorted(notes_dir().glob("????-??-??.md"), reverse=True)[:limit]:
        convs = parse_daily(f.read_text())
        if not convs:
            continue
        out.append({"date": f.stem, "conversations": len(convs),
                    "talk_minutes": round(sum(c["duration_s"] for c in convs) / 60, 1),
                    "words": sum(c["words"] for c in convs)})
    return {"days": out}


def llm_totals(con, since: float, rates: dict) -> dict:
    """Calls (failed ones included) and tokens since `since` (epoch s), with an estimated
    cost. Completion tokens already include reasoning tokens."""
    calls, p, c, r, k = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0), "
        "COALESCE(SUM(reasoning_tokens),0), COALESCE(SUM(cached_tokens),0) FROM llm_usage WHERE ts >= ?",
        (since,)).fetchone()
    usd = (p * float(rates.get("usd_per_m_prompt", 0.30)) + c * float(rates.get("usd_per_m_completion", 1.20))) / 1e6
    return {"calls": calls, "prompt_tokens": p, "completion_tokens": c, "reasoning_tokens": r,
            "cached_tokens": k, "usd": round(usd, 4)}


def status_payload() -> dict:
    db = data_dir() / "pages.db"
    names = pnames.load(data_dir())
    pendants: list[dict] = []
    pending_s = 0.0
    midnight = dt.datetime.combine(dt.datetime.now(TZ).date(), dt.time(), TZ)
    rates = cfgmod.load(data_dir()).get("llm", {})
    llm = {}
    if db.exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            for device, last_ts, n in con.execute(
                    "SELECT device, MAX(ts_ms), COUNT(*) FROM chunks GROUP BY device").fetchall():
                pendants.append({"device": device, "name": pnames.name_for(device, names),
                                 "last_audio": last_ts, "total_minutes": round(n * FRAME_S / 60, 1)})
            pending_s = con.execute("SELECT COUNT(*) FROM chunks WHERE done = 0").fetchone()[0] * FRAME_S
            try:
                llm = {"llm_today": llm_totals(con, midnight.timestamp(), rates),
                       "llm_7d": llm_totals(con, (midnight - dt.timedelta(days=6)).timestamp(), rates)}
            except sqlite3.Error:   # database from before the usage log
                pass
        finally:
            con.close()
    spool = sorted((data_dir() / "spool").glob("*.pmsg"), key=lambda p: p.stat().st_mtime)
    last_upload = int(spool[-1].stat().st_mtime * 1000) if spool else None
    zero = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
            "cached_tokens": 0, "usd": 0.0}
    return {"pendants": pendants, "last_upload": last_upload, "untranscribed_minutes": round(pending_s / 60, 1),
            "names": names, "llm_today": llm.get("llm_today", zero), "llm_7d": llm.get("llm_7d", zero)}


# ---------------------------------------------------------------------------
# people: who the wearer talks with, from the named speaker paragraphs

PARA = re.compile(r"^\[(\d{1,2}):(\d{2})\]\s*(?:⭐\s*)?(?:\*\*([^*\n]{1,40}):\*\*)?")
GENERIC_TAG = re.compile(r"^(speaker\s+\w+|me|them|unknown|someone)$", re.I)
PARA_CAP_MIN = 10.0
PEOPLE_MAX_DAYS = 90


def person_turns(c: dict) -> dict[str, dict]:
    """Per named speaker (keyed by lowercased name, spellings counted) in one parsed conversation: talk minutes and last paragraph
    stamp (minutes after the conversation start). A paragraph runs from its stamp to
    the next paragraph's stamp, the last one to the conversation end, at most 10 min.
    Generic tags (Speaker A, Me, Them) and untagged paragraphs earn no one time."""
    h, m = c["time"].split(":")
    start = int(h) * 60 + int(m)
    end = start + c["duration_s"] / 60
    paras, prev = [], start
    for line in c["text"].splitlines():
        pm = PARA.match(line)
        if not pm:
            continue
        t = int(pm[1]) * 60 + int(pm[2])
        while t < prev:   # past midnight
            t += 1440
        prev = t
        paras.append((t, (pm[3] or "").strip()))
    out: dict[str, dict] = {}   # keyed by lowercased name
    for i, (t, name) in enumerate(paras):
        if not name or GENERIC_TAG.match(name):
            continue
        nxt = min(paras[i + 1][0], end) if i + 1 < len(paras) else end   # never past the conversation end
        mins = min(max(nxt - t, 0.0), PARA_CAP_MIN)
        e = out.setdefault(name.lower(), {"minutes": 0.0, "last": t - start, "spellings": {}})
        e["minutes"] += mins
        e["last"] = max(e["last"], t - start)
        e["spellings"][name] = e["spellings"].get(name, 0) + 1
    return out


def _people_scan(days: int):
    """(date, parsed conversation, {name: turns}) for the last `days` local days, wearer excluded."""
    today = dt.datetime.now(TZ).date()
    wearer = str(cfgmod.load(data_dir())["speakers"].get("wearer") or "").lower()
    for back in range(days):
        day = today - dt.timedelta(days=back)
        f = notes_dir() / f"{day:%Y-%m-%d}.md"
        if not f.exists():
            continue
        for c in parse_daily(f.read_text()):
            turns = {n: t for n, t in person_turns(c).items() if n != wearer}
            if turns:
                yield day, c, turns


def _stamp_iso(day: dt.date, c: dict, offset_min: float) -> str:
    h, m = c["time"].split(":")
    t = dt.datetime.combine(day, dt.time(), TZ) + dt.timedelta(minutes=int(h) * 60 + int(m) + offset_min)
    return t.isoformat()


def people_payload(days: int = 7) -> list[dict]:
    people: dict[str, dict] = {}
    for day, c, turns in _people_scan(days):
        for key, t in turns.items():
            p = people.setdefault(key, {"spellings": {}, "minutes": 0.0, "conversations": 0, "last": ""})
            p["minutes"] += t["minutes"]
            p["conversations"] += 1
            p["last"] = max(p["last"], _stamp_iso(day, c, t["last"]))
            for sp, n in t["spellings"].items():
                p["spellings"][sp] = p["spellings"].get(sp, 0) + n
    total = sum(p["minutes"] for p in people.values())
    links = cfgmod.load(data_dir())["speakers"].get("people")
    rows = [{"name": max(p["spellings"], key=lambda k: (p["spellings"][k], k)), "talk_minutes": round(p["minutes"], 1), "conversations": p["conversations"],
             "last_heard": p["last"], "share": round(p["minutes"] / total, 3) if total else 0.0}
            for p in people.values()]
    for r in rows:
        eid = cfgmod.linked_entity(links, r["name"])
        if eid:
            r["entity_id"], r["lifeos_url"] = eid, lifeos_people.person_url(eid)
    found = lifeos_people.lookup([r["entity_id"] for r in rows if "entity_id" in r])
    for r in rows:
        p = found.get(r.get("entity_id"))
        if p:
            r["lifeos_name"], r["lifeos_circle"], r["lifeos_category"] = p["name"], p["circle"], p["category"]
    return sorted(rows, key=lambda r: (-r["talk_minutes"], r["name"].lower()))


def person_payload(name: str, days: int = 30) -> list[dict]:
    """The person's conversations in the last `days`, newest first. 404 only when they
    were never heard in the last 90 days."""
    key = name.strip().lower()
    today = dt.datetime.now(TZ).date()
    mine = []
    for day, c, turns in _people_scan(PEOPLE_MAX_DAYS):
        t = turns.get(key)
        if t:
            mine.append((day, c, t))
    if not mine:
        raise HTTPError(404, "not found")
    rows = [{"date": f"{day:%Y-%m-%d}", "time": c["time"], "title": c["title"], "device": c["device"],
             "their_minutes": round(t["minutes"], 1), "total_minutes": round(c["duration_s"] / 60, 1)}
            for day, c, t in mine if (today - day).days < days]
    return sorted(rows, key=lambda r: (r["date"], r["time"].zfill(5)), reverse=True)


# ---------------------------------------------------------------------------
# speakers (numpy / PyAV imported lazily: run the service from .venv)

class HTTPError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code, self.msg = code, msg


def speakers_payload() -> dict:
    """Enrolled voiceprints, most recognized first (`recognized` = clusters
    carrying that name, automatic or confirmed), then the allowed names."""
    from . import speakers
    cfg = cfgmod.load(data_dir())["speakers"]
    vps = speakers.load_voiceprints(speakers.voiceprint_dir(data_dir()))
    recognized: dict[str, int] = {}
    con = connect()
    if con is not None:
        try:
            for label, n in con.execute("SELECT label, COUNT(*) FROM speaker_clusters WHERE label IS NOT NULL "
                                        "AND COALESCE(labeled_by, '') != 'dismissed' GROUP BY label"):
                recognized[label] = n
        except sqlite3.OperationalError:
            pass
        finally:
            con.close()
    rows = [{"name": n, "count": c, "recognized": recognized.get(n, 0)} for n, (_v, c) in vps.items()]
    rows.sort(key=lambda r: (-r["recognized"], -r["count"], r["name"].lower()))
    return {"enrolled": rows, "allowed": list(cfg.get("allowed", []))}


def suggest_payload(q: str) -> dict:
    """Name-picker rows (lifeos_people.suggest). LifeOS suggestions create nothing:
    a voiceprint or allowed name only follows when the wearer picks one."""
    cfg = cfgmod.load(data_dir())["speakers"]
    return lifeos_people.suggest(q[:60], speakers_payload()["enrolled"], list(cfg.get("allowed", [])),
                                 cfg.get("people"))


HINT_SQL = "SELECT id, tag, label, labeled_by FROM speaker_clusters WHERE device = ? AND start_ms = ? ORDER BY id"


def _hints(con, keys: list[tuple[str, int, int]], sp: dict, enrolled: set[str], tapped: bool = False) -> dict[int, dict]:
    """{cluster id: calendar_hint.hint} for (device, start_ms, cluster id) keys."""
    out: dict[int, dict] = {}
    convs: dict[tuple, tuple] = {}
    muted = calendar_hint.muted_ids(con)
    for device, start_ms, cid in keys:
        k = (device, start_ms)
        if k not in convs:
            try:
                row = con.execute("SELECT calendar_event FROM conversations WHERE device = ? AND start_ms = ?",
                                  k).fetchone()
            except sqlite3.OperationalError:     # a database from before calendar matches
                row = None
            clusters = [dict(zip(("id", "tag", "label", "labeled_by"), r)) for r in con.execute(HINT_SQL, k)] \
                if row and row[0] else []
            convs[k] = (row[0] if row else None, clusters)
        raw, clusters = convs[k]
        out[cid] = calendar_hint.hint(raw, clusters, cid, sp, enrolled, tapped, muted=muted)
    return out


def speaker_hint(device: str, start_ms: int, tag: str) -> dict:
    """Who the voice tagged `tag` in a conversation probably is (the reassign sheet): the
    calendar's likely and candidates, the event, then LifeOS's `maybe` people for the
    conversation's window (calendar_hint.maybe). Empty when the tag isn't one voice; no `maybe`
    when LifeOS can't answer in time."""
    from . import speakers
    sp = cfgmod.load(data_dir())["speakers"]
    con = connect()
    if con is None:
        raise HTTPError(404, "no such conversation")
    try:
        try:
            conv = con.execute("SELECT calendar_event, end_ms FROM conversations WHERE device = ? AND start_ms = ?",
                               (device, start_ms)).fetchone()
        except sqlite3.OperationalError:
            conv = con.execute("SELECT NULL, end_ms FROM conversations WHERE device = ? AND start_ms = ?",
                               (device, start_ms)).fetchone()
        if conv is None:
            raise HTTPError(404, "no such conversation")
        ids = [r[0] for r in con.execute("SELECT id FROM speaker_clusters WHERE device = ? AND start_ms = ? "
                                         "AND tag = ?", (device, start_ms, tag))]
        enrolled = set(speakers.load_voiceprints(speakers.voiceprint_dir(data_dir())))
        h = _hints(con, [(device, start_ms, ids[0])], sp, enrolled, tapped=True)[ids[0]] if len(ids) == 1 \
            else {"likely": None, "candidates": []}
        clusters = [dict(zip(("id", "tag", "label", "labeled_by"), r)) for r in con.execute(HINT_SQL, (device, start_ms))] \
            if len(ids) == 1 else []
    finally:
        con.close()
    maybe = []
    if clusters:     # LifeOS is asked with the database closed
        w = (start_ms, conv[1] or start_ms)
        present = _stored_present([w]).get(w)
        if present is None:
            muted = _muted()
            present = calendar_hint.likely_present(*w, muted=muted)
            if present is not None and _muted() == muted:
                _keep_present({w: present})
        maybe = calendar_hint.maybe(present, clusters, ids[0], sp, enrolled, [h["likely"], *h["candidates"]],
                                    tapped=True)
    return {**h, "maybe": maybe, "event": calendar_hint.event_view(conv[0])}


def check_link(name, entity_id, source) -> None:
    """409 when `name` is already linked to a different LifeOS person: relinking needs the
    explicit action (`relink`), never a pick that happens to share a name."""
    if not entity_id or source != "user" or not isinstance(name, str) or not isinstance(entity_id, str):
        return
    have = cfgmod.linked_entity(cfgmod.load(data_dir())["speakers"].get("people"), name)
    if have and have != entity_id:
        raise HTTPError(409, "that name is linked to a different LifeOS person")


def relink(name: str, entity_id) -> dict:
    """Point the speaker name `name` at another LifeOS person (the "Change LifeOS person" action).
    Only the link changes: transcript labels, voiceprints and the voice library keep the name.
    404 for an unknown name or LifeOS person, 502 when LifeOS can't be asked."""
    if not isinstance(entity_id, str) or not cfgmod.ENTITY_RE.match(entity_id):
        raise HTTPError(400, "need entity_id")
    sp = cfgmod.load(data_dir())["speakers"]
    key = str(name or "").strip().lower()
    known = {str(n).lower(): str(n) for n in [*sp.get("allowed", []), *(sp.get("people") or {})]}
    spelling = known.get(key)
    if spelling is None:
        spelling = next((p["name"] for p in people_payload(PEOPLE_MAX_DAYS) if p["name"].lower() == key), None)
    if spelling is None:
        raise HTTPError(404, "no such speaker")
    try:
        row = lifeos_people.person(entity_id)
    except ValueError:
        raise HTTPError(404, "no such LifeOS person") from None
    except urllib.error.HTTPError as e:
        raise HTTPError(404 if e.code in (404, 422) else 502, "no such LifeOS person") from None
    except Exception:  # noqa: BLE001 - LifeOS down or refusing
        raise HTTPError(502, "LifeOS is unreachable") from None
    try:
        cfgmod.link_people(data_dir(), {spelling: entity_id}, replace=True)
    except (ValueError, OSError):
        raise HTTPError(500, "could not save the link") from None
    return {"name": spelling, "entity_id": entity_id, "lifeos_url": lifeos_people.person_url(entity_id),
            "lifeos_name": row["name"], "lifeos_circle": row["circle"], "lifeos_category": row["category"]}


def with_link(fn, name, entity_id, source, *args):
    """Run an answer that names a voice; refuse a conflicting link before it changes anything,
    and store the link once it has succeeded."""
    check_link(name, entity_id, source)
    out = fn(*args)
    link_picked(name, entity_id, source)
    return out


def link_picked(name, entity_id, source) -> None:
    """Remember the LifeOS person the wearer picked for a name (source "user" only). Metadata:
    never allows a name or touches a voiceprint; a bad id or config is ignored."""
    if not entity_id or source != "user" or not isinstance(name, str) or not isinstance(entity_id, str):
        return
    try:
        from . import speakers
        sp = cfgmod.load(data_dir())["speakers"]
        who = speakers.allowed_name(name, sp.get("allowed", [])) or cfgmod.clean_name(name)
        cfgmod.link_people(data_dir(), {who: entity_id})
    except (ValueError, OSError):
        pass


CHECKS_PER_REQUEST = 5   # unchecked clusters whose audio is voice-checked per request
# Unanswered and automatically named voices, and voices one of the wearer's "this conversation" answers
# reached without his tap (`named_via = 'sitting'`): a confirm is his own answer and teaches the voiceprint.
# Voices an "all past conversations" answer reached aren't asked again: he reviewed them in its preview.
CANDIDATE_SQL = ("SELECT id, device, start_ms, tag, embedding, start_s, end_s, speech_s, excerpt, voiced, clip, "
                 "label, labeled_by, COALESCE(mixed, 0), named_via FROM speaker_clusters "
                 "WHERE (labeled_by IS NULL OR labeled_by IN ('match', 'meeting', 'calendar') "
                 "OR (labeled_by = 'user' AND named_via = 'sitting')) "
                 "AND COALESCE(noise, 0) = 0 ORDER BY start_ms DESC, speech_s DESC LIMIT 500")


def _ensure_schema() -> None:
    """Voice Scoring tables and columns, when the feed outruns the processor's migration."""
    from . import voices
    con = connect(write=True)
    if con is None:
        return
    try:
        voices.ensure_schema(con)
    finally:
        con.close()


def _voice_check(rows: list, todo: list[int]) -> None:
    """Voice-check up to CHECKS_PER_REQUEST of rows[i] (i in todo): pick the
    cluster's most speech-dense window inside its span, store the window and the
    voiced fraction, and update the row in place."""
    from . import speakers
    audio: dict[str, object] = {}
    results = []
    for i in todo[:CHECKS_PER_REQUEST]:
        r = rows[i]
        clip = r[10] or ""
        try:
            if not Path(clip).exists():
                results.append((i, r[5], r[6], 0.0))
                continue
            if clip not in audio:
                audio[clip] = speakers.load_audio(Path(clip))
            s, e, v = speakers.best_voice_window(audio[clip], r[5], r[6])
            results.append((i, s, e, v))
        except ImportError:
            raise
        except Exception:  # noqa: BLE001 - unreadable clip: never offer it, and don't
            results.append((i, r[5], r[6], 0.0))  # let it block later candidates
            continue
    if not results:
        return
    con = connect(write=True)
    try:
        for i, s, e, v in results:
            con.execute("UPDATE speaker_clusters SET start_s = ?, end_s = ?, voiced = ? WHERE id = ?",
                        (s, e, v, rows[i][0]))
            rows[i][5], rows[i][6], rows[i][9] = s, e, v
        con.commit()
    finally:
        con.close()


def _auto_label(tag: str | None, label: str | None, labeled_by: str | None) -> str | None:
    """The name an automatic pass gave a cluster (meeting names may be text-only tags)."""
    if labeled_by not in ("match", "meeting", "calendar"):
        return None
    if label:
        return label
    return tag if tag and not GENERIC_TAG.match(tag) else None


def _calibration(con, cfg: dict):
    """calibration.Calibration for live scoring, or None (nobody enrolled, or it fails:
    scoring then uses the mean voiceprints and the old threshold)."""
    try:
        from . import calibration, speakers
        return calibration.load(con, speakers.voiceprint_dir(data_dir()), cfg)
    except Exception:
        return None


def unknown_payload(limit: int = 50, now: dt.datetime | None = None) -> dict:
    """Voice Scoring candidates, least confident first (see pendant_direct.voices):
    unlabeled clusters and clusters named automatically (match, meeting,
    calendar), never ones the wearer answered or marked noise. Each carries its
    `current_label` (the automatic name, or the closest voiceprint when it is at
    or above match_threshold) and `confidence`. Until the wearer has a
    voiceprint, the clusters that look like the wearer (each conversation's
    biggest talker, consistent across conversations) carry `suggested: <wearer>`
    and come first. Only clusters whose snippet is speech (>= MIN_VOICED voiced
    per Silero VAD, >= MIN_WORDS words) are offered; the check is stored per
    cluster and run lazily, CHECKS_PER_REQUEST at a time, in priority order.
    New voices are always offered; the rest share the daily budget."""
    import numpy as np

    from . import speakers, voices
    cfg = cfgmod.load(data_dir())["speakers"]
    match_threshold = float(cfg.get("match_threshold", 0.5))
    con = connect()
    if con is None:
        return {"speakers": []}
    try:
        try:
            rows = con.execute(CANDIDATE_SQL).fetchall()
        except sqlite3.OperationalError:
            _ensure_schema()
            rows = con.execute(CANDIDATE_SQL).fetchall()
        strangers, noise = voices.load_strangers(con), voices.load_noise(con)
        cal = _calibration(con, cfg)
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    rows = [list(r) for r in rows]
    if not rows:
        return {"speakers": []}
    embs = speakers.normalize(np.stack([np.frombuffer(r[4], dtype=np.float32) for r in rows]))
    vps = speakers.load_voiceprints(speakers.voiceprint_dir(data_dir()))
    scores = voices.score(embs, vps, strangers, noise,
                          float(cfg.get("noise_similarity", voices.NOISE_SIMILARITY)),
                          calibration=cal, exclude=[(r[1], r[2]) for r in rows],
                          self_ids=[r[0] for r in rows], threshold=match_threshold,
                          prior=_prior(cfg, vps))
    new_ratio = float(cfg.get("new_voice_threshold", match_threshold)) / max(match_threshold, 1e-9)

    suggested = np.zeros(len(rows), dtype=bool)
    wearer = speakers.allowed_name(cfgmod.wearer(cfg), cfg.get("allowed", []))
    if wearer and wearer not in vps:
        dominant: dict[tuple, int] = {}
        for i, r in enumerate(rows):
            k = (r[1], r[2])
            if k not in dominant or r[7] > rows[dominant[k]][7]:
                dominant[k] = i
        dom = np.array(sorted(dominant.values()))
        if len(dom) >= 3:
            centroid = speakers.normalize(embs[dom].mean(0))
            dom = dom[embs[dom] @ centroid >= 0.3]
        suggested[dom] = True

    threshold = float(cfg.get("new_voice_threshold", match_threshold))
    min_samples = speakers.naming_settings(cfg)["min_samples"]
    names = pnames.load(data_dir())
    cands: dict[int, dict] = {}
    for i, r in enumerate(rows):
        if len((r[8] or "").split()) < speakers.MIN_WORDS:
            continue
        sc = scores[i]
        reached = bool(r[14]) and bool(r[11])
        if reached:
            current = r[11]     # the name his sitting or bulk answer gave it: confirm or not
        elif r[12] == "match" and sc.stranger_closer:
            current = None      # a stranger's voice is closer than the matched voiceprint
        elif r[12]:
            current = _auto_label(r[3], r[11], r[12])
        elif sc.matched:
            current = sc.best_name
        else:
            current = wearer if suggested[i] else None
        cands[i] = {"id": r[0], "clip_id": r[0], "when": int(r[2] + r[5] * 1000), "conversation_start": r[2],
                    "device": pnames.name_for(r[1], names), "tag": r[3],
                    "duration_s": round(r[6] - r[5], 1), "speech_s": round(r[7], 1),
                    "excerpt": r[8] or "", "suggested": wearer if suggested[i] else None,
                    "current_label": current, "labeled_by": r[12],
                    "confidence": round(sc.confidence, 3), "similarity": round(sc.similarity, 3),
                    "margin": round(sc.margin, 3), "noise_like": sc.noise_like,
                    "score": round(sc.match_score, 3), "calibrated": sc.calibrated, "mixed": bool(r[13]),
                    "reached": reached,
                    # reached voices: people with the fewest samples first, then the longest voice
                    "reached_rank": ((speakers.sample_count(r[11], vps), -float(r[7] or 0)) if reached else None),
                    # a voice scoring below this is probably someone new (on the score's own scale)
                    "new_cut": (sc.threshold * new_ratio if sc.calibrated else threshold)}
        # its likely person is too thinly sampled to be named automatically: ask about it first
        cands[i]["thin"] = bool(sc.best_name and sc.match_score >= cands[i]["new_cut"]
                                and speakers.thin_names([sc.best_name], vps, cal, min_samples, cfgmod.wearer(cfg)))

    order = sorted(cands, key=lambda i: voices.priority(cands[i]))
    _voice_check(rows, [i for i in order if rows[i][9] is None])
    ready = []
    for i in order:
        if rows[i][9] is None or rows[i][9] < speakers.MIN_VOICED:
            continue
        if not rows[i][10] or not Path(rows[i][10]).exists():
            continue      # audio no longer kept (retention.py): nothing to play, so no question
        c = cands[i]
        c["when"] = int(rows[i][2] + rows[i][5] * 1000)
        c["duration_s"] = round(rows[i][6] - rows[i][5], 1)
        ready.append(c)

    day = voices.local_day(now)
    con = connect(write=True)
    try:
        try:
            offered = voices.offered_on(con, day)
        except sqlite3.OperationalError:
            voices.ensure_schema(con)
            offered = voices.offered_on(con, day)
        out, new = voices.select(ready, offered, budget=int(cfg.get("daily_budget", voices.DAILY_BUDGET)),
                                 new_voice_threshold=threshold, limit=limit)
        voices.record_offers(con, day, new)
        con.commit()
    finally:
        con.close()
    for c in out:
        c["new_voice"] = c["score"] < c.pop("new_cut") and not c["reached"]
        c.pop("thin", None)
        c.pop("reached_rank", None)
    _add_hints(out, {r[0]: (r[1], r[2]) for r in rows}, cfg, set(vps))
    return {"speakers": out}


def _add_hints(cards: list[dict], keys: dict[int, tuple], sp: dict, enrolled: set[str]) -> None:
    """Each Voice Scoring / Who's this card gets its `conversation` key (device, start_ms, tag) and
    `hints` as the speaker suggest route gives them: the calendar's `likely` and `candidates`
    (calendar_hint.hint, also at the top level as before), then LifeOS's `maybe` people for the
    conversation's window: the answers the processor keeps in pages.db (calendar_hint.warm_present),
    and LifeOS is asked live only for windows without one, once per distinct window for the whole
    page, with the suggest route's cache and back-off; the page waits for those no longer than one
    call's budget (calendar_hint.likely_present_many), and their answers are kept too. A failure
    leaves calendar hints only, or the cards as they were."""
    for c in cards:
        c.setdefault("likely", None)
        c.setdefault("candidates", [])
        c["hints"] = {"likely": None, "candidates": [], "maybe": []}
        if c["id"] in keys:
            device, start_ms = keys[c["id"]]
            c["conversation"] = {"device": device, "start_ms": start_ms, "tag": c.get("tag")}
    con = connect()
    if con is None:
        return
    try:
        mine = [(*keys[c["id"]], c["id"]) for c in cards if c["id"] in keys]
        hints = _hints(con, mine, sp, enrolled)
        convs: dict[tuple, tuple] = {}     # (device, start_ms) -> (end_ms, clusters)
        for device, start_ms, _cid in mine:
            k = (device, start_ms)
            if k not in convs:
                row = con.execute("SELECT end_ms FROM conversations WHERE device = ? AND start_ms = ?", k).fetchone()
                convs[k] = ((row[0] if row else None) or start_ms,
                            [dict(zip(("id", "tag", "label", "labeled_by"), r)) for r in con.execute(HINT_SQL, k)])
        kept = calendar_hint.stored_present(con, [(start_ms, convs[(device, start_ms)][0])
                                                  for device, start_ms, _cid in mine])
    except Exception as e:  # noqa: BLE001 - a suggestion never costs the cards
        print(f"calendar hints unavailable ({type(e).__name__})", flush=True)
        return
    finally:
        con.close()
    for c in cards:
        h = hints.get(c["id"])
        if h:
            c.update(h)
            c["hints"] = {**h, "maybe": []}
    try:     # LifeOS is asked with the database closed
        windows = [(start_ms, convs[(device, start_ms)][0]) for device, start_ms, _cid in mine]
        missing = [w for w in windows if w not in kept]
        muted = _muted()
        live = calendar_hint.likely_present_many(missing, muted=muted) if missing else {}
        present = {**kept, **live}
        if live and _muted() == muted:    # a mute meanwhile cleared the kept answers: don't put old ones back
            _keep_present({w: v for w, v in live.items() if v is not None})
        by_id = {c["id"]: c for c in cards}
        for device, start_ms, cid in mine:
            end_ms, clusters = convs[(device, start_ms)]
            h = hints[cid]
            by_id[cid]["hints"]["maybe"] = calendar_hint.maybe(present.get((start_ms, end_ms)), clusters, cid, sp,
                                                               enrolled, [h["likely"], *h["candidates"]])
    except Exception as e:  # noqa: BLE001 - LifeOS's guess never costs the calendar's
        print(f"lifeos hints unavailable ({type(e).__name__})", flush=True)


def _muted() -> set[str]:
    con = connect()
    if con is None:
        return set()
    try:
        return calendar_hint.muted_ids(con)
    finally:
        con.close()


def calendar_mute(device: str, start_ms: int, on: bool = True) -> dict:
    """"Not with them" on a calendar guess: the conversation's event series says nothing about who
    the wearer was with, from now on (calendar_hint.mute; `on` False undoes it). 404 without a
    calendar match."""
    con = connect(write=True)
    if con is None:
        raise HTTPError(404, "not found")
    try:
        row = con.execute("SELECT calendar_event FROM conversations WHERE device = ? AND start_ms = ?",
                          (device, start_ms)).fetchone()
        ev = calendar_hint.stored(row[0]) if row else None
        if ev is None:
            raise HTTPError(404, "no calendar event")
        key = (calendar_hint.mute if on else calendar_hint.unmute)(con, ev)
        con.commit()
    finally:
        con.close()
    return {"muted": on, "series": key, "event": ev["title"]}


def _stored_present(windows) -> dict:
    con = connect()
    if con is None:
        return {}
    try:
        return calendar_hint.stored_present(con, windows)
    finally:
        con.close()


def _keep_present(answers: dict) -> None:
    """Keeps LifeOS answers fetched live (calendar_hint.save_present), so the next page has them."""
    if not answers:
        return
    db = data_dir() / "pages.db"
    if not db.exists():
        return
    con = sqlite3.connect(db, timeout=0.5)     # never holds the page up behind a processor write
    try:
        for w, people in answers.items():
            calendar_hint.save_present(con, w, people)
        con.commit()
    except sqlite3.Error as e:   # a busy database only costs the next page a live call
        print(f"likely-present not kept ({type(e).__name__})", flush=True)
    finally:
        con.close()


def cluster_row(cid: int):
    con = connect()
    if con is None:
        raise HTTPError(404, "not found")
    try:
        row = con.execute("SELECT clip, start_s, end_s, embedding, labeled_by FROM speaker_clusters "
                          "WHERE id = ?", (cid,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        con.close()
    if row is None:
        raise HTTPError(404, "not found")
    return row


def clip_wav(cid: int) -> bytes:
    """The cluster's snippet as 16 kHz mono 16-bit WAV (AVPlayer plays it)."""
    import io
    import wave

    import numpy as np

    from . import speakers
    clip, start_s, end_s, _e, _l = cluster_row(cid)
    if not Path(clip).exists():
        raise HTTPError(404, "clip missing")
    end_s = min(end_s, start_s + SNIPPET_MAX_S)
    audio = speakers.load_audio(Path(clip))
    sr = speakers.SAMPLE_RATE
    clip_s = len(audio) / sr
    if start_s >= clip_s:  # stored span past the end: play the clip's last seconds
        start_s, end_s = max(0.0, clip_s - SNIPPET_MAX_S), clip_s
    pcm = (np.clip(audio[int(start_s * sr):int(end_s * sr)], -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


ANSWER_COLUMNS = ("id, device, start_ms, tag, label, labeled_by, embedding, enrolled_in, COALESCE(mixed, 0) AS mixed, "
                  "COALESCE(speech_s, 0) AS speech_s, named_via")
ANSWER_KEYS = ("id", "device", "start_ms", "tag", "label", "labeled_by", "embedding", "enrolled_in", "mixed", "speech_s",
               "named_via")


def _answer_con():
    """(write connection, schema ensured) for an answer, or 404 without a database."""
    from . import voices
    con = connect(write=True)
    if con is None:
        raise HTTPError(404, "not found")
    try:
        voices.ensure_schema(con)
    except sqlite3.OperationalError:
        con.close()
        raise HTTPError(404, "not found") from None
    return con


def _answer_row(con, cid: int) -> dict:
    row = con.execute(f"SELECT {ANSWER_COLUMNS} FROM speaker_clusters WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise HTTPError(404, "not found")
    return dict(zip(ANSWER_KEYS, row))


def _take_back(r: dict):
    """Remove this cluster's sample from the voiceprint it was folded into (if
    any). Idempotent per cluster id, so a retried answer can't remove it twice;
    samples inside a voiceprint's legacy history can't be removed."""
    from . import speakers, voices
    prior = voices.prior_enrollment(r["enrolled_in"], r["labeled_by"], r["label"])
    if prior:
        speakers.remove(speakers.voiceprint_dir(data_dir()), prior, r["id"])
    return prior


def _library(fn, *args, **kw):
    """Voice library bookkeeping (voice_library.py): never lets a failure undo or block an answer."""
    try:
        return fn(*args, **kw)
    except Exception as e:  # noqa: BLE001
        print(f"voice library: {type(e).__name__}: {str(e)[:100]}", flush=True)


def _keep_voices(con, device: str, start_ms: int) -> None:
    """Kept whole-voice audio (voice_audio.py) follows the conversation's hand-named voices; never blocks an answer."""
    from . import voice_audio
    _library(voice_audio.sync_conversation, con, data_dir(), notes_dir(), cfgmod.load(data_dir()), device, start_ms)


def _finish(con, cid: int, relabeled: bool) -> None:
    from . import voices
    voices.mark(con, voices.local_day(), cid, "answered")
    con.commit()
    if relabeled:
        voices.try_apply_relabels(data_dir(), notes_dir(), con)


def _prior(cfg: dict, vps: dict) -> dict | None:
    """Closeness priors for the enrolled people (no calendar context here), or None."""
    from . import closeness
    return closeness.load_priors(cfg, list(vps)) or None


def _guess(r: dict, con) -> str | None:
    """What the card asked about: the automatic name, else the closest voiceprint
    at or above match_threshold (no stranger closer)."""
    auto = _auto_label(r["tag"], r["label"], r["labeled_by"])
    if auto:
        return auto
    import numpy as np

    from . import speakers, voices
    vps = speakers.load_voiceprints(speakers.voiceprint_dir(data_dir()))
    cfg = cfgmod.load(data_dir())["speakers"]
    sc = voices.score(np.frombuffer(r["embedding"], dtype=np.float32)[None, :], vps, voices.load_strangers(con),
                      calibration=_calibration(con, cfg), exclude=(r["device"], r["start_ms"]),
                      self_ids=[r["id"]] if r.get("id") is not None else None,
                      threshold=float(cfg.get("match_threshold", 0.5)), prior=_prior(cfg, vps))
    return sc[0].best_name if sc and sc[0].matched else None


def _name_voice(con, r: dict, canonical: str, allowed: list, sample: bool = True) -> tuple[int, bool]:
    """Name one voice by hand (rows and voiceprints; the caller commits): its sample leaves the
    voiceprint it was in and joins `canonical`'s unless mixed or too short, its lines are queued
    for relabelling, and any stranger or noise record of it goes. `sample` False (a voice a
    sitting answer reached, not the one the wearer tapped): named, its lines relabelled, but never a
    voiceprint sample; only voices the wearer named himself teach voiceprints.
    (voiceprint samples, relabel queued)."""
    import numpy as np

    from . import speakers, voices
    cid = r["id"]
    vdir = speakers.voiceprint_dir(data_dir())
    prior = voices.prior_enrollment(r["enrolled_in"], r["labeled_by"], r["label"])
    have = speakers.load_voiceprints(vdir).get(canonical)
    enrolled = "" if r["mixed"] else canonical
    if r["mixed"]:
        # two people in one voice: named, but its embedding never joins a voiceprint
        if prior:
            _take_back(r)
        count = have[1] if have is not None else 0
    elif not sample:
        if prior:
            _take_back(r)
        count, enrolled = (have[1] if have is not None else 0), ""
    elif prior == canonical and have is not None:
        count = have[1]
    elif not voices.sample_ok(r["speech_s"], cfgmod.load(data_dir())["speakers"]):
        # too little speech to teach the voiceprint (speakers.min_sample_speech_s): named only
        if prior:
            _take_back(r)
        count, enrolled = (have[1] if have is not None else 0), ""
    else:
        if prior and prior != canonical:
            _take_back(r)
        count = speakers.enroll(vdir, canonical, np.frombuffer(r["embedding"], dtype=np.float32), allowed,
                                cluster_id=cid)
    relabeled = voices.queue_relabel(con, cid, r["device"], r["start_ms"], r["tag"], canonical)
    con.execute("DELETE FROM strangers WHERE cluster_id = ?", (cid,))
    con.execute("DELETE FROM noise_exemplars WHERE cluster_id = ?", (cid,))
    con.execute("UPDATE speaker_clusters SET label = ?, tag = ?, labeled_by = 'user', enrolled_in = ?, "
                "label_kind = 'person', noise = 0, named_via = ? WHERE id = ?",
                (canonical, canonical, enrolled, None if sample else "sitting", cid))
    return count, relabeled


def _sitting_plan(con, cid: int) -> dict | None:
    """The sitting of voice `cid` (sitting.py), or None when it can't be read."""
    from . import sitting
    try:
        return sitting.load(con, cfgmod.load(data_dir())["speakers"], cid)
    except (KeyError, sqlite3.OperationalError):
        return None


def label_cluster(cid: int, name: str, source: str | None = None, reassign: bool = False,
                  sitting: bool = False, within: set | None = None) -> dict:
    """Confirm or correct who a cluster is. Consent-gated: only names in
    speakers.allowed can be enrolled; with source "user" (the wearer picked or typed
    the name in the app) an unlisted name is added to speakers.allowed first.

    The cluster's sample leaves the voiceprint it was folded into (if that is a
    different name) and joins the named one (a confirm of an already enrolled
    sample changes nothing), the conversation's transcript paragraphs move from
    the old tag to the name, and the cluster is labeled_by 'user'. Voiceprint
    files change first and per cluster id, idempotently; if the database update
    then fails, retrying the answer leaves the voiceprints as one success would.

    reassign=True (tapping a name in a transcript) also accepts a cluster that
    was already answered or marked noise: any stranger or noise record of it is
    dropped, and the sample leaves the voiceprint it was in.

    sitting=True names the voice for the whole sitting (sitting.py): every voice the
    sitting matches in the neighbouring pieces is named the same way (its lines relabelled, but
    only the tapped voice becomes a voiceprint sample), and the
    answer gains `sitting` (its parts, span and what else was named). `within` (the voices an
    undo record keeps) limits the other voices it may name."""
    from . import sitting as sittingmod
    from . import speakers, voices
    allowed = cfgmod.load(data_dir())["speakers"].get("allowed", [])
    canonical = speakers.allowed_name(str(name or ""), allowed)
    if canonical is None and source != "user":
        raise HTTPError(403, "name not allowed")
    con = _answer_con()
    try:
        r = _answer_row(con, cid)
        if r["labeled_by"] in ("user", "dismissed") and not reassign and not r["named_via"]:
            raise HTTPError(409, "already labeled")      # a voice a sitting or bulk answer reached is still his to answer
        plan = _sitting_plan(con, cid) if sitting else None
        if canonical is None:
            try:
                canonical = cfgmod.allow_name(data_dir(), str(name or ""))
            except ValueError:
                raise HTTPError(400, "invalid name") from None
            allowed = cfgmod.load(data_dir())["speakers"].get("allowed", [])
        guess = _guess(r, con)
        count, relabeled = _name_voice(con, r, canonical, allowed)
        others = []
        for oid in sittingmod.ids(plan) if plan else []:
            if within is not None and oid not in within:
                continue                   # changed since the undo record listed its voices: left alone
            o = _answer_row(con, oid)
            n, queued = _name_voice(con, o, canonical, allowed, sample=False)
            count = max(count, n)
            voices.mark(con, voices.local_day(), oid, "answered")
            others.append(o)
            relabeled = relabeled or queued
        _finish(con, cid, relabeled)
        from . import voice_library
        for v in [r, *others]:
            _library(voice_library.drop, data_dir(), f"cluster:{v['id']}")
        if not r["mixed"]:      # only the voice the wearer tapped joins the library
            _library(voice_library.add_cluster, con, data_dir(), cfgmod.load(data_dir()), r["id"], canonical, "hand")
        for key in dict.fromkeys((v["device"], v["start_ms"]) for v in [r, *others]):
            _keep_voices(con, *key)
    finally:
        con.close()
    out = {"id": cid, "name": canonical, "count": count,
           "action": "confirm" if guess and guess.lower() == canonical.lower() else "correct",
           "relabeled": relabeled}
    if plan:
        out["sitting"] = {**sittingmod.summary(plan), "ids": [o["id"] for o in others]}
    return out


def sitting_payload(device: str, start_ms: int, tag: str) -> dict:
    """What "This conversation" covers for the voice behind `tag` (`GET .../speaker/<tag>/sitting`):
    parts, span and how many voices in how many other parts it would name. One part when the tag
    names no single voice here or the sitting is off. Reads only."""
    from . import sitting
    con = connect()
    if con is None:
        raise HTTPError(404, "not found")
    try:
        rows = con.execute("SELECT id FROM speaker_clusters WHERE device = ? AND start_ms = ? AND tag = ?",
                           (device, start_ms, tag)).fetchall()
        conv = con.execute("SELECT end_ms FROM conversations WHERE device = ? AND start_ms = ?",
                           (device, start_ms)).fetchone()
        plan = _sitting_plan(con, rows[0][0]) if len(rows) == 1 else None
    except sqlite3.OperationalError:
        raise HTTPError(404, "not found") from None
    finally:
        con.close()
    if conv is None:
        raise HTTPError(404, "no such conversation")
    if plan is None:
        end = conv[0]
        return {"parts": 1, "span_s": round((end - start_ms) / 1000) if end is not None else None,
                "start_ms": start_ms, "end_ms": end, "named": 0, "named_parts": 0, "before": 0, "after": 0}
    return sitting.summary(plan)


def label_answer(cid: int, name, source: str | None = None, entity_id=None) -> dict:
    """A Who's this or Voice Scoring answer naming a voice (`POST speakers/<id>/label`): for the
    whole sitting (label_cluster). When the sitting names voices in other pieces too, the answer
    is recorded for undo (reassign_undo.py) and gains `change_id` and `undo_until`; an answer that
    names only its own voice changes as it always did (no processor lock, no record)."""
    keyed: set[int] = set()                # the sitting's voices the undo record keeps

    def run():
        return with_link(label_cluster, name, entity_id, source, cid, name, source, False, True, keyed)
    check_link(name, entity_id, source)        # a conflicting link is refused before anything else
    con = connect()
    if con is None:
        raise HTTPError(404, "not found")
    try:
        row = con.execute("SELECT device, start_ms FROM speaker_clusters WHERE id = ?", (cid,)).fetchone()
        plan = _sitting_plan(con, cid) if row else None
    except sqlite3.OperationalError:
        row = plan = None
    finally:
        con.close()
    if row is None:
        raise HTTPError(404, "not found")
    if not plan or not plan["named"]:
        return with_link(label_cluster, name, entity_id, source, cid, name, source)

    def keys(con):
        p = _sitting_plan(con, cid)
        keyed.update(n["id"] for n in (p or {}).get("named", []))
        return [cid, *sorted(keyed)]
    try:
        return reassign_undo.record(data_dir(), "conversation", row[0], row[1], {}, run, extra=keys)
    except reassign_undo.UndoError as e:
        raise HTTPError(e.code, e.msg) from None


def _section(con, device: str, start_ms: int) -> str | None:
    """This conversation's Raw section text, or None."""
    from . import voices
    row = con.execute("SELECT day, raw_heading FROM conversations WHERE device = ? AND start_ms = ?",
                      (device, start_ms)).fetchone()
    if not row or not row[0] or not row[1]:
        return None
    daily = notes_dir() / f"{row[0]}.md"
    return voices.section_text(daily.read_text(), row[1]) if daily.exists() else None


def stranger_cluster(cid: int, reassign: bool = False) -> dict:
    """Remove name ("back to an unnamed speaker"): the embedding is kept in `strangers`
    (one row per answer, never merged into an identity) so similar voices aren't
    matched to known people; similar voices are still asked about. The sample
    leaves any voiceprint it was in, its voice library clip goes, and the transcript
    calls the voice a "Speaker X" no one else in the conversation uses (voices.fresh_tag;
    its own tag when that already is one), returned as `name`. When another voice of the
    conversation shows the same tag, their lines can't be told apart: the voice keeps that
    tag, so its row and the note still agree."""
    from . import voices
    con = _answer_con()
    try:
        r = _answer_row(con, cid)
        if r["labeled_by"] in ("user", "dismissed") and not reassign and not r["named_via"]:
            raise HTTPError(409, "already labeled")
        shared = r["tag"] and con.execute(
            "SELECT 1 FROM speaker_clusters WHERE device = ? AND start_ms = ? AND tag = ? AND id != ? LIMIT 1",
            (r["device"], r["start_ms"], r["tag"], cid)).fetchone()
        tag = r["tag"] if shared else voices.fresh_tag(con, r["device"], r["start_ms"],
                                                         _section(con, r["device"], r["start_ms"]),
                                                         own=r["tag"], cid=cid)
        _take_back(r)
        con.execute("DELETE FROM noise_exemplars WHERE cluster_id = ?", (cid,))
        if not r["mixed"]:   # a mixed voice is partly someone else: it would veto them too
            con.execute("INSERT OR IGNORE INTO strangers (cluster_id, embedding, created_at) VALUES (?,?,?)",
                        (cid, r["embedding"], dt.datetime.now().timestamp()))
        relabeled = voices.queue_relabel(con, cid, r["device"], r["start_ms"], r["tag"], tag)
        con.execute("UPDATE speaker_clusters SET label = NULL, tag = ?, labeled_by = 'user', enrolled_in = '', "
                    "label_kind = 'stranger', noise = 0, named_via = NULL WHERE id = ?", (tag, cid))
        _finish(con, cid, relabeled)
        from . import voice_library
        _library(voice_library.drop, data_dir(), f"cluster:{cid}")
        _keep_voices(con, r["device"], r["start_ms"])
    finally:
        con.close()
    return {"id": cid, "stranger": True, "name": tag, "relabeled": relabeled}


def dismiss_cluster(cid: int) -> dict:
    cluster_row(cid)
    con = connect(write=True)
    try:
        con.execute("UPDATE speaker_clusters SET labeled_by = 'dismissed' WHERE id = ? AND labeled_by IS NULL",
                    (cid,))
        con.commit()
    finally:
        con.close()
    return {"id": cid, "dismissed": True}


def skip_cluster(cid: int) -> dict:
    """Swipe up: never offered again (voices.offered_on), and nothing else changes. Still counts
    against today's budget."""
    from . import voices
    con = _answer_con()
    try:
        _answer_row(con, cid)
        voices.mark(con, voices.local_day(), cid, "skipped")
        con.commit()
    finally:
        con.close()
    return {"id": cid, "skipped": True}


def not_them(cid: int) -> dict:
    """"Not <name>" on a voice a "this conversation" answer named: it isn't that person, and nothing else
    is known. The name comes off (the transcript calls the voice a fresh "Speaker X", as Remove name
    does), it is never offered again, and nothing is learned from it: no voiceprint sample, no
    stranger or noise record."""
    from . import voices
    con = _answer_con()
    try:
        r = _answer_row(con, cid)
        if not r["label"]:
            raise HTTPError(409, "no name to take off")
        shared = r["tag"] and con.execute(
            "SELECT 1 FROM speaker_clusters WHERE device = ? AND start_ms = ? AND tag = ? AND id != ? LIMIT 1",
            (r["device"], r["start_ms"], r["tag"], cid)).fetchone()
        tag = r["tag"] if shared else voices.fresh_tag(con, r["device"], r["start_ms"],
                                                         _section(con, r["device"], r["start_ms"]),
                                                         own=r["tag"], cid=cid)
        _take_back(r)
        relabeled = voices.queue_relabel(con, cid, r["device"], r["start_ms"], r["tag"], tag)
        con.execute("UPDATE speaker_clusters SET label = NULL, tag = ?, labeled_by = 'dismissed', enrolled_in = '', "
                    "label_kind = NULL, named_via = NULL WHERE id = ?", (tag, cid))
        _finish(con, cid, relabeled)
        from . import voice_library
        _library(voice_library.drop, data_dir(), f"cluster:{cid}")
        _keep_voices(con, r["device"], r["start_ms"])
    finally:
        con.close()
    return {"id": cid, "not": True, "name": tag, "relabeled": relabeled}


def mark_mixed(cid: int) -> dict:
    """Two people in one clip ("Two people"): a bad sample, so never offered again, taken out of
    any voiceprint it was folded into, its voice library clip dropped, and no stranger or noise
    exemplar kept (it would veto both people). The transcript keeps whatever label it shows;
    `mixed` keeps it out of voiceprints, enrollment checks and bulk relabels for good."""
    con = _answer_con()
    try:
        r = _answer_row(con, cid)
        _take_back(r)
        con.execute("DELETE FROM strangers WHERE cluster_id = ?", (cid,))
        con.execute("DELETE FROM noise_exemplars WHERE cluster_id = ?", (cid,))
        con.execute("UPDATE speaker_clusters SET mixed = 1, labeled_by = 'dismissed', enrolled_in = '', "
                    "label_kind = 'mixed', noise = 0, named_via = NULL WHERE id = ?", (cid,))
        _finish(con, cid, False)
        from . import voice_library
        _library(voice_library.drop, data_dir(), f"cluster:{cid}")
        _keep_voices(con, r["device"], r["start_ms"])
    finally:
        con.close()
    return {"id": cid, "mixed": True}


def mark_noise(cid: int) -> dict:
    """Not a voice: never offered again, taken out of any voiceprint, and kept
    as a noise exemplar that pushes similar clusters to the back of the queue."""
    con = _answer_con()
    try:
        r = _answer_row(con, cid)
        if r["labeled_by"] not in ("user", "dismissed") or r["named_via"]:
            _take_back(r)
            if not r["mixed"]:   # partly a real voice: never a noise exemplar
                con.execute("INSERT OR IGNORE INTO noise_exemplars (cluster_id, embedding, created_at) "
                            "VALUES (?,?,?)", (cid, r["embedding"], dt.datetime.now().timestamp()))
            con.execute("UPDATE speaker_clusters SET noise = 1, labeled_by = 'dismissed', label = NULL, "
                        "enrolled_in = '', label_kind = 'noise', named_via = NULL WHERE id = ?", (cid,))
            _finish(con, cid, False)
            from . import voice_library
            _library(voice_library.drop, data_dir(), f"cluster:{cid}")
            _keep_voices(con, r["device"], r["start_ms"])
    finally:
        con.close()
    return {"id": cid, "noise": True}


_LINE_LOCK = threading.Lock()   # the feed is one process: serializes a line's holder read -> move -> record
LINE_SAMPLE_MAX_S = 20.0   # embed at most this much of a line's audio


def _embed_span(clip: str, start_s: float, end_s: float):
    """Unit embedding of clip[start_s:end_s] (the voice model loads on first use)."""
    from . import speakers
    return speakers.get_embedder(data_dir())(Path(clip), [(start_s, end_s)])[0]


def _removes(req: dict) -> bool:
    """Whether a reassignment asks for Remove name: `remove: true`, `name: null`, or the
    name "Someone" (what older app versions sent for a random person)."""
    from . import voices
    name = req.get("name")
    return (req.get("remove") is True or ("name" in req and name is None)
            or (isinstance(name, str) and name.strip().lower() == voices.SOMEONE.lower()))


def _person(name: str, source: str | None) -> str:
    """Canonical name for an assignment. The #13 consent rule: an unlisted name needs
    source "user" and is then added to speakers.allowed."""
    from . import speakers
    name = str(name or "").strip()
    allowed = cfgmod.load(data_dir())["speakers"].get("allowed", [])
    canonical = speakers.allowed_name(name, allowed)
    if canonical is None:
        if source != "user":
            raise HTTPError(403, "name not allowed")
        try:
            canonical = cfgmod.allow_name(data_dir(), name)
        except ValueError:
            raise HTTPError(400, "invalid name") from None
    return canonical


def _line_sample(con, device: str, start_ms: int, index: int, offsets, clip: str | None, who: str | None) -> dict:
    """Move this paragraph's audio sample to `who` (a person, or None: Remove name, a stranger). Keyed by
    `para:<device>:<start_ms>:<index>`: assigning the same line again changes
    nothing, assigning it to someone else takes it out of the first voiceprint (or
    stranger list). Without recorded offsets (conversations written before they
    were kept) or usable audio the voice is left alone."""
    import numpy as np

    from . import speakers, voices
    sid = f"para:{device}:{start_ms}:{index}"
    if not offsets or index >= len(offsets) or not offsets[index]:
        print(f"reassign {sid}: no clip offsets for this paragraph; text only", flush=True)
        return {"sample": "skipped", "reason": "no offsets"}
    row = con.execute("SELECT key, holder, embedding FROM paragraph_samples WHERE id = ?", (sid,)).fetchone()
    key = row[0] if row else speakers.sample_key(sid)
    if row and row[2]:
        emb = np.frombuffer(row[2], dtype=np.float32)
    else:
        start_s, end_s = float(offsets[index][0]), min(float(offsets[index][1]),
                                                       float(offsets[index][0]) + LINE_SAMPLE_MAX_S)
        if end_s - start_s < speakers.MIN_SPAN_S or not clip or not Path(clip).exists():
            print(f"reassign {sid}: audio span unusable; text only", flush=True)
            return {"sample": "skipped", "reason": "no audio"}
        try:
            emb = np.asarray(_embed_span(clip, start_s, end_s), dtype=np.float32)
        except Exception as e:  # noqa: BLE001 - the text edit still stands
            print(f"reassign {sid}: embedding failed ({type(e).__name__}); text only", flush=True)
            return {"sample": "skipped", "reason": "embedding failed"}
    holder = row[1] if row else None
    target = voices.STRANGER_HOLDER if who is None else who
    vdir = speakers.voiceprint_dir(data_dir())
    allowed = cfgmod.load(data_dir())["speakers"].get("allowed", [])
    if holder and holder != target and holder != voices.STRANGER_HOLDER:
        speakers.remove(vdir, holder, key)
    if holder == voices.STRANGER_HOLDER and target != holder:
        con.execute("DELETE FROM strangers WHERE cluster_id = ?", (key,))
    if target == voices.STRANGER_HOLDER:
        con.execute("INSERT OR IGNORE INTO strangers (cluster_id, embedding, created_at) VALUES (?,?,?)",
                    (key, emb.tobytes(), dt.datetime.now().timestamp()))
    else:
        speakers.enroll(vdir, who, emb, allowed, cluster_id=key)
    con.execute("INSERT OR REPLACE INTO paragraph_samples (id, key, holder, embedding, updated_at) VALUES (?,?,?,?,?)",
                (sid, key, target, emb.tobytes(), time.time()))
    return {"sample": "added" if holder != target else "unchanged"}


def reassign_speaker(device: str, start_ms: int, req: dict, within: set | None = None) -> dict:
    """Tap a speaker name in a conversation. scope "conversation": that speaker
    tag's whole voice cluster is relabeled exactly like a Voice Scoring answer
    (voiceprint add/remove by cluster id, transcript relabel, labeled_by 'user').
    scope "line" (with `paragraph`, counting stamped paragraphs from 0): only that
    paragraph's label changes in the Raw section and its audio span becomes a
    voiceprint sample of its own. scope "all" relabels every past voice that sounds
    like the tapped one (bulk_apply; bulk_preview says what it would touch).

    Remove name (`remove: true`, `name: null`, or the old "Someone"; scope line or
    conversation only, else 400) makes the speaker unknown again: the voice (or line)
    becomes a stranger exemplar, leaves the old person's voiceprint and voice library,
    and is labeled a "Speaker X" unused in this conversation (`name`; `removed: true`)."""
    from . import voices
    name, source, scope = req.get("name"), req.get("source"), req.get("scope")
    remove = _removes(req)
    if scope not in ("line", "conversation", "all") or not (remove or isinstance(name, str)):
        raise HTTPError(400, "need name and scope line|conversation|all")
    if remove and scope == "all":
        raise HTTPError(400, "Remove name applies to this line or this conversation")
    who = None if remove else _person(name, source)
    if scope == "all":
        return bulk_apply(device, start_ms, req.get("tag"), who, req.get("ids"), req.get("digest"))
    if scope == "conversation":
        tag = req.get("tag")
        if not isinstance(tag, str) or not tag:
            raise HTTPError(400, "need tag")
        con = _answer_con()
        try:
            ids = [r[0] for r in con.execute("SELECT id FROM speaker_clusters WHERE device = ? AND start_ms = ? "
                                              "AND tag = ? ORDER BY id", (device, start_ms, tag))]
        finally:
            con.close()
        if not ids:
            raise HTTPError(404, "no voice for that name in this conversation")
        if len(ids) > 1:
            raise HTTPError(409, "several voices share that name; use this line only")
        if remove:
            out = stranger_cluster(ids[0], reassign=True)
            return {"scope": "conversation", "cluster": ids[0], "name": out["name"], "removed": True,
                    "relabeled": out["relabeled"]}
        out = label_cluster(ids[0], who, "user", reassign=True, sitting=True, within=within)
        res = {"scope": "conversation", "cluster": ids[0], "name": who, "relabeled": out["relabeled"]}
        return {**res, "sitting": out["sitting"]} if "sitting" in out else res
    index = req.get("paragraph")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise HTTPError(400, "need paragraph")
    con = _answer_con()
    try:
        conv = con.execute("SELECT day, raw_heading, clip, paragraph_offsets FROM conversations "
                           "WHERE device = ? AND start_ms = ?", (device, start_ms)).fetchone()
        if conv is None:
            raise HTTPError(404, "no such conversation")
        day, heading, clip, offsets = conv
        daily = notes_dir() / f"{day}.md"
        section = None
        if heading and daily.exists():
            from .process import section_bounds
            lines = daily.read_text().split("\n")
            b = section_bounds(lines, heading)
            if b and index >= len(voices.paragraph_lines(lines[b[0]:b[1]])):
                raise HTTPError(404, "no such paragraph")
            section = "\n".join(lines[b[0]:b[1]]) if b else None
        with _LINE_LOCK:
            from . import voice_library
            tag = who if who is not None else voices.fresh_tag(con, device, start_ms, section)
            sample = _line_sample(con, device, start_ms, index, _offsets(offsets), clip, who)
            if who is None:
                _library(voice_library.drop, data_dir(), f"para:{device}:{start_ms}:{index}")
            else:
                _library(voice_library.add_line, con, data_dir(), cfgmod.load(data_dir()), device, start_ms, index,
                         _offsets(offsets), who)
            voices.queue_paragraph_relabel(con, device, start_ms, index, tag)
            con.commit()
        applied = voices.try_apply_relabels(data_dir(), notes_dir(), con)
        _keep_voices(con, device, start_ms)
    finally:
        con.close()
    out = {"scope": "line", "paragraph": index, "name": tag, "applied": applied, **sample}
    return {**out, "removed": True} if who is None else out

def reassign_recorded(device: str, start_ms: int, req: dict) -> dict:
    """reassign_speaker (with its LifeOS link) with an undo record: the answer gains
    `change_id` and `undo_until` (reassign_undo.py)."""
    keyed: set[int] = set()                # the sitting's voices the undo record keeps

    def run():
        if _removes(req):                  # Remove name names no one: no LifeOS link, name and entity_id ignored
            return reassign_speaker(device, start_ms, req)
        return with_link(reassign_speaker, req.get("name"), req.get("entity_id"), req.get("source"),
                         device, start_ms, req, keyed)
    if req.get("scope") not in ("line", "conversation", "all") or (req.get("scope") == "all" and _removes(req)):
        return run()                       # refused before anything changes
    extra = None
    if req.get("scope") == "conversation" and not _removes(req):
        def extra(con):                    # the sitting's other voices (computed under the same lock as the change)
            tag = req.get("tag")
            rows = con.execute("SELECT id FROM speaker_clusters WHERE device = ? AND start_ms = ? AND tag = ?",
                               (device, start_ms, tag)).fetchall() if isinstance(tag, str) else []
            p = _sitting_plan(con, rows[0][0]) if len(rows) == 1 else None
            keyed.update(n["id"] for n in (p or {}).get("named", []))
            return sorted(keyed)
    try:
        return reassign_undo.record(data_dir(), req["scope"], device, start_ms, req, run, extra=extra)
    except reassign_undo.UndoError as e:
        raise HTTPError(e.code, e.msg) from None


def undo_reassign(change_id: str) -> dict:
    try:
        return reassign_undo.undo(data_dir(), notes_dir(), change_id)
    except reassign_undo.UndoError as e:
        raise HTTPError(e.code, e.msg) from None


BULK_SAMPLE_MAX = 20       # conversations listed in a preview
BULK_EXCERPT_CHARS = 110


def _check_person(name, source: str | None) -> str:
    """Like _person, but changes nothing: the canonical name, or an HTTPError (403 unlisted
    name without source "user", 400 invalid name or Remove name, which applies to one
    conversation at most)."""
    from . import speakers, voices
    name = str(name or "").strip()
    if name.lower() == voices.SOMEONE.lower():
        raise HTTPError(400, "Remove name applies to this line or this conversation")
    canonical = speakers.allowed_name(name, cfgmod.load(data_dir())["speakers"].get("allowed", []))
    if canonical is None:
        if source != "user":
            raise HTTPError(403, "name not allowed")
        try:
            canonical = cfgmod.clean_name(name)
        except ValueError:
            raise HTTPError(400, "invalid name") from None
    return canonical


def _tapped_voice(con, device: str, start_ms: int, tag) -> dict:
    """The one voice cluster behind `tag` in this conversation (404 none, 409 several)."""
    if not isinstance(tag, str) or not tag:
        raise HTTPError(400, "need tag")
    rows = con.execute(f"SELECT {ANSWER_COLUMNS} FROM speaker_clusters WHERE device = ? AND start_ms = ? "
                       "AND tag = ? ORDER BY id", (device, start_ms, tag)).fetchall()
    if not rows:
        raise HTTPError(404, "no voice for that name in this conversation")
    if len(rows) > 1:
        raise HTTPError(409, "several voices share that name; use this line only")
    return dict(zip(ANSWER_KEYS, rows[0]))


def _voice_paragraphs(lines: list[str], heading: str, tag: str, skip: set[int]) -> tuple[list[int], int, str]:
    """(paragraph indexes, words, first excerpt) of the paragraphs labeled `**tag:**` in
    `## heading`, leaving out the `skip` ones (reassigned line by line)."""
    from . import voices
    from .process import section_bounds
    b = section_bounds(lines, heading) if heading else None
    if not b:
        return [], 0, ""
    body = lines[b[0]:b[1]]
    found, words, excerpt = [], 0, ""
    for n, k in enumerate(voices.paragraph_lines(body)):
        m = voices._PARA.match(body[k])
        if n in skip or not m.group(0).endswith(f"**{tag}:** "):
            continue
        spoken = body[k][m.end():].strip()
        found.append(n)
        words += len(spoken.split())
        if not excerpt:
            excerpt = spoken if len(spoken) <= BULK_EXCERPT_CHARS else spoken[:BULK_EXCERPT_CHARS].rstrip() + "…"
    return found, words, excerpt


def _unit_vec(v):
    import numpy as np
    v = np.asarray(v, dtype=np.float32)
    return v / max(float(np.linalg.norm(v)), 1e-9)


def bulk_digest(hits: list[dict]) -> str:
    """Short fingerprint of a matched set: each voice id with the paragraphs and words it
    would relabel, so a new voice, a changed override or a changed threshold all change it."""
    import hashlib
    raw = ",".join(f"{h['id']}:{'.'.join(map(str, sorted(h['paragraphs'])))}:{h['words']}"
                   for h in sorted(hits, key=lambda h: h["id"]))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def bulk_match(con, tapped: dict, who: str) -> dict:
    """The past voices that sound like `who` and would be relabeled `who`: every voice
    of every other conversation that scores at least the threshold for `speakers.scoring`:
    "mean" the cosine to the tapped voice against match_threshold; "topk" and "calibrated"
    the score against `who`'s samples plus the tapped voice (calibration.py) against
    match_threshold or match_threshold_calibrated, except voices the wearer answered by hand (user/dismissed),
    voices marked as noise, voices already carrying the name, and voices whose tag
    another voice of their own conversation shares (`skipped`: their lines can't be
    told apart). Reads only; the preview and the apply both use this."""
    import numpy as np

    from . import calibration, voices
    cfg = cfgmod.load(data_dir())["speakers"]
    ref = np.frombuffer(tapped["embedding"], dtype=np.float32)
    ref = ref / max(float(np.linalg.norm(ref)), 1e-9)
    rows = con.execute(
        "SELECT s.id, s.device, s.start_ms, s.tag, s.label, s.embedding, s.labeled_by, s.label_kind, "
        "s.enrolled_in, COALESCE(s.noise, 0), c.day, c.raw_heading, c.title, COALESCE(s.mixed, 0) "
        "FROM speaker_clusters s JOIN conversations c ON c.device = s.device AND c.start_ms = s.start_ms "
        "WHERE NOT (s.device = ? AND s.start_ms = ?) AND s.embedding IS NOT NULL "
        "ORDER BY c.day, s.start_ms, s.id", (tapped["device"], tapped["start_ms"])).fetchall()
    shared = {(r[0], r[1], r[2]): r[3] for r in con.execute(
        "SELECT device, start_ms, tag, COUNT(*) FROM speaker_clusters GROUP BY device, start_ms, tag")}
    try:
        overrides: dict = {}
        for d, ms, para in con.execute("SELECT DISTINCT device, start_ms, para_index FROM transcript_relabels "
                                       "WHERE para_index IS NOT NULL"):
            overrides.setdefault((d, ms), set()).add(para)
    except sqlite3.OperationalError:
        overrides = {}
    from . import speakers
    prints = {k.lower(): _unit_vec(v[0]) for k, v in speakers.load_voiceprints(
        speakers.voiceprint_dir(data_dir())).items()}
    days: dict[str, list[str] | None] = {}
    hits = []
    if tapped.get("mixed"):
        # a voice holding two people matches both of them: relabel only its own conversation
        return {"voices": [], "skipped": 0, "conversations": 0, "days": 0, "words": 0}
    rows = [r for r in rows if r[3] and r[3].lower() != who.lower() and r[6] not in ("user", "dismissed")
            and not r[9] and r[7] != "noise" and len(r[5]) == len(tapped["embedding"])]
    skipped = 0
    mode = cfgmod.scoring_mode(cfg)
    raw_thr = float(cfg.get("match_threshold", 0.5))
    res = None
    if mode != "mean":
        # score against who's samples plus the tapped voice, with the cohort of everyone else
        # (the tapped voice's own conversation is left out of it)
        tapped_key = (tapped["device"], tapped["start_ms"])
        all_samples = speakers.load_samples(speakers.voiceprint_dir(data_dir()))
        target = next((k for k in all_samples if k.lower() == who.lower()), who)
        old_e, old_ids = all_samples.get(target, (np.zeros((0, len(ref)), np.float32), []))
        keep = [i for i, c in enumerate(old_ids) if c != tapped["id"]]
        target_samples = {target: (np.vstack([old_e[keep], ref[None, :]]),
                                   [old_ids[i] for i in keep] + [tapped["id"]])}
        cohort = (calibration.build_cohort(con, all_samples).without(tapped_key) if mode == "calibrated"
                  else calibration.Cohort.empty(len(ref)))
        cal = calibration.Calibration(
            target_samples, cohort, mode=mode, raw_threshold=raw_thr,
            threshold=float(cfg.get("match_threshold_calibrated",
                                    cfgmod.DEFAULTS["speakers"]["match_threshold_calibrated"])))
        res = cal.scores(np.stack([np.frombuffer(r[5], dtype=np.float32) for r in rows]) if rows
                         else np.zeros((0, len(ref))), exclude=[(r[1], r[2]) for r in rows])
    for n, (cid, device, start_ms, tag, label, emb, labeled_by, label_kind, enrolled_in, noise,
            day, heading, title, mixed) in enumerate(rows):
        v = np.frombuffer(emb, dtype=np.float32)
        sim = float(v @ ref / max(float(np.linalg.norm(v)), 1e-9))
        # "mean": the cosine to the tapped voice against match_threshold
        score_, thr_, cal_ = ((sim, raw_thr, False) if res is None else
                              (float(res.match[n, 0]), float(res.threshold[n, 0]), bool(res.calibrated[n, 0])))
        if score_ < thr_:
            continue
        if mixed:              # two people in one voice: never relabeled in bulk
            skipped += 1
            continue
        # an automatic name from a voiceprint that fits better than the tapped voice does
        # is probably right (the wearer's own lines): leave it, unless it is the name being corrected
        own = prints.get((label or tag).lower()) if labeled_by in voices.AUTO_LABELS else None
        if (own is not None and (label or tag).lower() != tapped["tag"].lower() and len(own) == len(v)
                and float(own @ (v / max(float(np.linalg.norm(v)), 1e-9))) >= sim):
            skipped += 1
            continue
        if shared.get((device, start_ms, tag), 1) > 1:
            skipped += 1
            continue
        if day not in days:
            f = notes_dir() / f"{day}.md"
            days[day] = f.read_text().split("\n") if f.exists() else None
        paras, words, excerpt = (_voice_paragraphs(days[day], heading, tag, overrides.get((device, start_ms), set()))
                                 if days[day] is not None else ([], 0, ""))
        stamp = (heading or "").split(" ")[0]
        if not re.fullmatch(r"\d{1,2}:\d{2}", stamp):
            stamp = f"{dt.datetime.fromtimestamp(start_ms / 1000, TZ):%H:%M}"
        hits.append({"id": cid, "device": device, "start_ms": start_ms, "day": day, "time": stamp,
                     "title": title or "", "tag": tag, "label": label, "labeled_by": labeled_by,
                     "label_kind": label_kind, "enrolled_in": enrolled_in, "similarity": round(sim, 4),
                     "score": round(score_, 4), "calibrated": cal_,
                     "paragraphs": paras, "words": words, "excerpt": excerpt})
    convs = {(h["device"], h["start_ms"]) for h in hits}
    return {"voices": hits, "skipped": skipped, "conversations": len(convs),
            "days": len({h["day"] for h in hits}), "words": sum(h["words"] for h in hits)}


def bulk_sample(hits: list[dict], limit: int = BULK_SAMPLE_MAX) -> list[dict]:
    """At most `limit` conversations, spread across days (newest day first, one
    conversation per day per round) and best match first within a day; a
    conversation with several matched voices is one entry, led by its best voice."""
    by_conv: dict[tuple, dict] = {}
    for h in sorted(hits, key=lambda h: -h.get("score", h["similarity"])):
        e = by_conv.get((h["device"], h["start_ms"]))
        if e is None:
            by_conv[(h["device"], h["start_ms"])] = {**h, "paragraphs": list(h["paragraphs"])}
        else:
            e["paragraphs"] = sorted({*e["paragraphs"], *h["paragraphs"]})
            e["words"] += h["words"]
    per_day: dict[str, list[dict]] = {}
    for e in sorted(by_conv.values(), key=lambda e: (-e["similarity"], e["start_ms"])):
        per_day.setdefault(e["day"], []).append(e)
    order = sorted(per_day, reverse=True)
    picked: list[dict] = []
    for rnd in range(max((len(v) for v in per_day.values()), default=0)):
        for d in order:
            if rnd < len(per_day[d]) and len(picked) < limit:
                picked.append(per_day[d][rnd])
    picked.sort(key=lambda e: e["day"], reverse=True)
    return [{"date": e["day"], "time": e["time"], "title": e["title"], "device": e["device"],
             "device_id": e["device"], "start_ms": e["start_ms"], "label": e["tag"],
             "similarity": e["similarity"], "excerpt": e["excerpt"], "words": e["words"],
             "paragraphs": e["paragraphs"]} for e in picked]


def bulk_preview(device: str, start_ms: int, tag, name, source: str | None = None) -> dict:
    """What `scope: "all"` would do for this voice and name, without changing anything."""
    who = _check_person(name, source)
    con = connect()
    if con is None:
        raise HTTPError(404, "not found")
    try:
        try:
            tapped = _tapped_voice(con, device, start_ms, tag)
        except sqlite3.OperationalError:
            _ensure_schema()     # the feed outran the processor's migration (e.g. `mixed`)
            tapped = _tapped_voice(con, device, start_ms, tag)
        m = bulk_match(con, tapped, who)
    except sqlite3.OperationalError:
        raise HTTPError(404, "not found") from None
    finally:
        con.close()
    return {"conversations": m["conversations"], "days": m["days"], "words": m["words"],
            "skipped": m["skipped"], "name": who, "ids": sorted(h["id"] for h in m["voices"]),
            "digest": bulk_digest(m["voices"]), "sample": bulk_sample(m["voices"])}


def bulk_apply(device: str, start_ms: int, tag, who: str, ids, digest) -> dict:
    """scope "all": the tapped voice is answered like scope "conversation" (the only one
    added to a voiceprint); every voice bulk_match reports is relabeled through the
    relabel queue (lines reassigned one by one stay), recorded as answered by the wearer, and
    taken out of any other voiceprint its sample was in. `ids` and `digest` are the preview's:
    the set is recomputed and applied only when identical (else 409 "preview changed"). Matches are never folded into
    a voiceprint or the strangers list, so a wrong match can't snowball."""
    from . import speakers, voices
    with _LINE_LOCK:
        con = _answer_con()
        try:
            tapped = _tapped_voice(con, device, start_ms, tag)
            m = bulk_match(con, tapped, who)
        finally:
            con.close()
        if (not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)
                or not isinstance(digest, str)):
            raise HTTPError(400, "need the preview's ids and digest")
        if sorted(ids) != sorted(h["id"] for h in m["voices"]) or digest != bulk_digest(m["voices"]):
            raise HTTPError(409, "preview changed")
        out = label_cluster(tapped["id"], who, "user", reassign=True)
        con = _answer_con()
        try:
            batch, now, day = f"{time.time():.6f}:{tapped['id']}", time.time(), voices.local_day()
            queued = 0
            for h in m["voices"]:
                prior = voices.prior_enrollment(h["enrolled_in"], h["labeled_by"], h["label"])
                keep = prior == who
                if prior and not keep:
                    speakers.remove(speakers.voiceprint_dir(data_dir()), prior, h["id"])
                con.execute("INSERT INTO bulk_relabels (batch, cluster_id, old_tag, old_label, old_labeled_by, "
                            "old_label_kind, old_enrolled_in, new_tag, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (batch, h["id"], h["tag"], h["label"], h["labeled_by"], h["label_kind"],
                             h["enrolled_in"], who, now))
                queued += voices.queue_relabel(con, h["id"], h["device"], h["start_ms"], h["tag"], who)
                con.execute("UPDATE speaker_clusters SET label = ?, tag = ?, labeled_by = 'user', enrolled_in = ?, "
                            "label_kind = 'person', noise = 0, named_via = 'bulk' WHERE id = ?",
                            (who, who, who if keep else "", h["id"]))
                voices.mark(con, day, h["id"], "answered")
                from . import voice_library
                _library(voice_library.drop, data_dir(), f"cluster:{h['id']}")   # no longer that person's clip
            con.commit()
            applied = voices.try_apply_relabels(data_dir(), notes_dir(), con) if queued else True
            for key in dict.fromkeys((h["device"], h["start_ms"]) for h in m["voices"]):
                _keep_voices(con, *key)
        finally:
            con.close()
    return {"scope": "all", "cluster": tapped["id"], "name": who, "relabeled": out["relabeled"] or bool(queued),
            "conversations": m["conversations"], "days": m["days"], "words": m["words"],
            "voices": len(m["voices"]), "skipped": m["skipped"], "applied": applied}


def set_star(device: str, start_ms: int, on: bool) -> dict:
    """Star a conversation from the app (swipe right), or take that star off.
    The star is a `notable_overrides.json` entry (true); un-starring removes only
    a true entry, so button-press stars and explicit false overrides stay. The
    Event note and Highlights follow, under the processor's lock."""
    from . import highlights as hl
    from . import process
    with trash.processor_lock(data_dir()):
        con = trash.open_db(data_dir())
        try:
            trash.recover_or_refuse(con, data_dir(), notes_dir())
            if not con.execute("SELECT 1 FROM conversations WHERE device=? AND start_ms=?",
                               (device, start_ms)).fetchone():
                raise HTTPError(404, "no such conversation")
            path = data_dir() / "notable_overrides.json"
            try:
                raw = json.loads(path.read_text()) if path.exists() else {}
            except ValueError:
                raise HTTPError(500, "notable_overrides.json is not valid JSON") from None
            if not isinstance(raw, dict):
                raise HTTPError(500, "notable_overrides.json is not an object")
            key = hl.event_id(device, start_ms)
            before = dict(raw)
            if on:
                raw[key] = True
            elif raw.get(key) is True:
                del raw[key]
            if raw != before:
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
                os.replace(tmp, path)
            con.isolation_level = ""   # implicit transactions, like the processor
            changed = process.reconcile_notable(con, device, start_ms, hl.load_overrides(path),
                                                cfgmod.load(data_dir()), notes_dir())
            con.commit()
            day, notable = con.execute("SELECT day, notable FROM conversations WHERE device=? AND start_ms=?",
                                       (device, start_ms)).fetchone()
            if changed:
                process.write_highlights(con, day, notes_dir())
            pressed = con.execute("SELECT COUNT(*) FROM stars WHERE conv_device=? AND conv_start_ms=?",
                                  (device, start_ms)).fetchone()[0]
        finally:
            con.close()
    return {"app_star": raw.get(key) is True, "notable": bool(notable), "stars": pressed}


def enroll_create(req: dict) -> dict:
    """Record a voice-sample reading window; the processor judges it later."""
    con = connect(write=True)
    if con is None:
        raise HTTPError(404, "not found")
    try:
        try:
            eid = enroll.create(con, req.get("name"), req.get("device"), req.get("start_ms"), req.get("end_ms"))
        except enroll.BadRequest as e:
            raise HTTPError(400, str(e)) from None
        except enroll.Conflict as e:
            raise HTTPError(409, str(e)) from None
        return {"id": eid, **enroll.status(con, eid)}
    finally:
        con.close()


def enroll_phone(eid: int, body: bytes) -> dict:
    """Judge the phone-mic recording of reading `eid` now; the verdict is the answer."""
    con = connect(write=True)
    if con is None:
        raise HTTPError(404, "not found")
    try:
        try:
            out = enroll.add_phone(con, data_dir(), cfgmod.load(data_dir()), eid, body)
        except enroll.BadRequest as e:
            raise HTTPError(400, str(e)) from None
    finally:
        con.close()
    if out is None:
        raise HTTPError(404, "not found")
    return out


def enroll_status(eid: int) -> dict:
    con = connect(write=True)
    if con is None:
        raise HTTPError(404, "not found")
    try:
        enroll.ensure_schema(con)
        out = enroll.status(con, eid)
    finally:
        con.close()
    if out is None:
        raise HTTPError(404, "not found")
    return out


def _trash_call(fn, *args):
    try:
        return fn(*args)
    except trash.TrashError as e:
        raise HTTPError(e.code, e.msg) from None


class Handler(BaseHTTPRequestHandler):
    token = ""

    def _json(self, code: int, obj) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        return hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {self.token}")

    def _audio(self, device: str, start_ms: int) -> None:
        con = connect()
        clip = None
        if con is not None:
            try:
                r = con.execute("SELECT clip FROM conversations WHERE device = ? AND start_ms = ?",
                                (device, start_ms)).fetchone()
                clip = r[0] if r else None
            except sqlite3.OperationalError:
                pass
            finally:
                con.close()
        try:
            path = audio.playable(data_dir(), clip)
        except ImportError:
            return self._json(503, {"error": "audio needs the .venv interpreter"})
        except Exception as e:  # noqa: BLE001 - keep the server up
            return self._json(500, {"error": type(e).__name__})
        if path is None:
            return self._json(404, {"error": "audio missing"})
        size = path.stat().st_size
        rng = audio.parse_range(self.headers.get("Range"), size)
        if rng == "bad":
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            return self.end_headers()
        first, last = rng if rng else (0, size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "audio/mp4")
        self.send_header("Accept-Ranges", "bytes")
        if rng:
            self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
        self.send_header("Content-Length", str(last - first + 1))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(first)
                left = last - first + 1
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):   # the player closes connections as it seeks
            pass

    def _speakers(self, fn, *args):
        try:
            return fn(*args)
        except HTTPError as e:
            self._json(e.code, {"error": e.msg})
        except ImportError:
            self._json(503, {"error": "speaker features need the .venv interpreter"})
        except Exception as e:  # noqa: BLE001 - keep the server up
            self._json(500, {"error": type(e).__name__})
        return None

    def _health(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY * 4:
            return self._json(413, {"error": "too large"})
        try:
            reports = health.parse_report(json.loads(self.rfile.read(n) or b"{}"))
        except (ValueError, health.BadReport):  # JSONDecodeError is a ValueError
            return self._json(400, {"error": "bad health report"})
        sent = health.process(data_dir(), reports)
        return self._json(200, {"ok": True, "alerts_sent": len(sent)})

    def _tasks(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._json(413, {"error": "too large"})
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
            device, start_ms, item = req["device_id"], int(req["start_ms"]), str(req["item"])
            if not isinstance(device, str) or not tasks.clean_item(item):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            return self._json(400, {"error": "need device_id, start_ms, item"})
        con = connect(write=True)
        if con is None:
            return self._json(404, {"error": "no such conversation"})
        try:
            out = tasks.request_task(con, device, start_ms, item, "app")
        except KeyError:
            return self._json(404, {"error": "no such conversation"})
        except sqlite3.Error:
            return self._json(500, {"error": "database"})
        finally:
            con.close()
        if out["status"] == "created":
            return self._json(200, {"task_id": out["task_id"], "item": tasks.clean_item(item)})
        return self._json(502, {"error": "LifeOS is unreachable; the request is kept and will be retried"
                                if out["status"] == "pending" else "LifeOS refused the task"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        path = self.path.split("?")[0].rstrip("/")
        if path == "/pendant/feed/health":
            return self._health()
        if path == "/pendant/feed/tasks":
            return self._tasks()
        if path == "/pendant/feed/enroll":
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._json(413, {"error": "too large"})
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "bad json"})
            out = self._speakers(with_link, enroll_create, req.get("name"), req.get("entity_id"),
                                 req.get("source"), req)
            return None if out is None else self._json(200, out)
        m = re.fullmatch(r"/pendant/feed/conversations/([0-9A-Fa-f]+)/(\d+)/calendar/(mute|unmute)", path)
        if m:
            out = self._speakers(calendar_mute, m[1].upper(), int(m[2]), m[3] == "mute")
            return None if out is None else self._json(200, out)
        m = re.fullmatch(r"/pendant/feed/enroll/(\d+)/phone", path)
        if m:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_PHONE_BODY:
                return self._json(413, {"error": "too large"})
            if n <= 0:
                return self._json(400, {"error": "no audio"})
            out = self._speakers(enroll_phone, int(m[1]), self.rfile.read(n))
            return None if out is None else self._json(200, out)
        m = re.fullmatch(r"/pendant/feed/people/([^/]+)/link", path)
        if m:
            from urllib.parse import unquote
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._json(413, {"error": "too large"})
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "bad json"})
            out = self._speakers(relink, unquote(m[1]), req.get("entity_id"))
            return None if out is None else self._json(200, out)
        m = re.fullmatch(r"/pendant/feed/conversations/([0-9A-Fa-f]+)/(\d+)/speaker", path)
        if m:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._json(413, {"error": "too large"})
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "bad json"})
            out = self._speakers(reassign_recorded, m[1].upper(), int(m[2]), req)
            return None if out is None else self._json(200, out)
        m = re.fullmatch(r"/pendant/feed/reassign/([0-9a-f]{32})/undo", path)
        if m:
            out = self._speakers(undo_reassign, m[1])
            return None if out is None else self._json(200, out)
        m = re.fullmatch(r"/pendant/feed/conversations/([0-9A-Za-z]+)/(\d+)/(star|delete|restore)", path)
        if m:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._json(413, {"error": "too large"})
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "bad json"})
            device, start_ms = m[1].upper(), int(m[2])
            if m[3] == "star":
                if not isinstance(req.get("on"), bool):
                    return self._json(400, {"error": "need on: true|false"})
                out = self._speakers(_trash_call, set_star, device, start_ms, req["on"])
            elif m[3] == "delete":
                out = self._speakers(_trash_call, trash.delete, data_dir(), notes_dir(), device, start_ms)
            else:
                out = self._speakers(_trash_call, trash.restore, data_dir(), notes_dir(), device, start_ms)
            return None if out is None else self._json(200, out)
        m = re.fullmatch(r"/pendant/feed/speakers/(\d+)/(label|dismiss|noise|mixed|stranger|skip|not)", path)
        if not m:
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._json(413, {"error": "too large"})
        body = self.rfile.read(n) if n else b""
        if m[2] == "dismiss":
            out = self._speakers(dismiss_cluster, int(m[1]))
        elif m[2] == "noise":
            out = self._speakers(mark_noise, int(m[1]))
        elif m[2] == "mixed":
            out = self._speakers(mark_mixed, int(m[1]))
        elif m[2] == "not":
            out = self._speakers(not_them, int(m[1]))
        elif m[2] == "stranger":
            out = self._speakers(stranger_cluster, int(m[1]))
        elif m[2] == "skip":
            out = self._speakers(skip_cluster, int(m[1]))
        else:
            try:
                req = json.loads(body or b"{}")
                name, source = req.get("name"), req.get("source")
            except (ValueError, AttributeError):
                return self._json(400, {"error": "bad json"})
            out = self._speakers(label_answer, int(m[1]), name, source, req.get("entity_id"))
        if out is not None:
            self._json(200, out)

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        path = self.path.split("?")[0].rstrip("/")
        if path == "/pendant/feed/speakers":
            out = self._speakers(speakers_payload)
            return None if out is None else self._json(200, out)
        m = re.fullmatch(r"/pendant/feed/enroll/(\d+)", path)
        if m:
            out = self._speakers(enroll_status, int(m[1]))
            return None if out is None else self._json(200, out)
        if path == "/pendant/feed/speakers/unknown":
            out = self._speakers(unknown_payload)
            return None if out is None else self._json(200, out)
        m = re.fullmatch(r"/pendant/feed/conversations/([0-9A-Fa-f]+)/(\d+)/speaker/preview", path)
        if m:
            from urllib.parse import parse_qs, urlsplit
            qs = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
            out = self._speakers(bulk_preview, m[1].upper(), int(m[2]), qs.get("tag"), qs.get("name"),
                                 qs.get("source"))
            return None if out is None else self._json(200, out)
        m = re.fullmatch(r"/pendant/feed/conversations/([0-9A-Fa-f]+)/(\d+)/speaker/([^/]+)/sitting", path)
        if m:
            from urllib.parse import unquote
            out = self._speakers(sitting_payload, m[1].upper(), int(m[2]), unquote(m[3]))
            return None if out is None else self._json(200, out)
        m = re.fullmatch(r"/pendant/feed/conversations/([0-9A-Fa-f]+)/(\d+)/speaker/([^/]+)/suggest", path)
        if m:
            from urllib.parse import unquote
            out = self._speakers(speaker_hint, m[1].upper(), int(m[2]), unquote(m[3]))
            return None if out is None else self._json(200, out)
        m = re.fullmatch(r"/pendant/feed/conversations/([0-9A-Fa-f]+)/(\d+)/audio", path)
        if m:
            return self._audio(m[1].upper(), int(m[2]))
        m = re.fullmatch(r"/pendant/feed/speakers/clip/(\d+)", path)
        if m:
            data = self._speakers(clip_wav, int(m[1]))
            if data is not None:
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            return None
        if path == "/pendant/feed/people/suggest":
            from urllib.parse import parse_qs, urlsplit
            qs = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
            out = self._speakers(suggest_payload, qs.get("q", ""))
            return None if out is None else self._json(200, out)
        if path == "/pendant/feed/people" or path.startswith("/pendant/feed/people/"):
            from urllib.parse import parse_qs, unquote, urlsplit
            qs = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
            try:
                n = int(qs.get("days", 7 if path == "/pendant/feed/people" else 30))
                if not 1 <= n <= PEOPLE_MAX_DAYS:
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "bad days"})
            if path == "/pendant/feed/people":
                return self._json(200, people_payload(n))
            out = self._speakers(person_payload, unquote(path[len("/pendant/feed/people/"):]), n)
            return None if out is None else self._json(200, out)
        if path == "/pendant/feed/interactions":
            from urllib.parse import parse_qs, urlsplit
            qs = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
            try:
                start, end = dt.date.fromisoformat(qs.get("from", "")), dt.date.fromisoformat(qs.get("to", ""))
                if not 0 <= (end - start).days < INTERACTIONS_MAX_DAYS:
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "bad range"})
            try:
                return self._json(200, interactions_payload(start, end))
            except sqlite3.Error:
                return self._json(503, {"error": "database unavailable"})
        if path == "/pendant/feed/day-signals":
            from urllib.parse import parse_qs, urlsplit
            qs = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
            try:
                day = f"{dt.date.fromisoformat(qs.get('day', '')):%Y-%m-%d}"
            except ValueError:
                return self._json(400, {"error": "bad date"})
            from . import day_signals
            con = connect()
            try:
                out = day_signals.read(con, day) if con is not None else None
            finally:
                if con is not None:
                    con.close()
            return self._json(200, out) if out else self._json(404, {"error": "not computed"})
        if path == "/pendant/feed/days":
            return self._json(200, days_payload())
        if path == "/pendant/feed/trash":
            return self._json(200, {"trash": trash.list_trash(data_dir())})
        if path == "/pendant/feed/search":
            from urllib.parse import parse_qs, urlsplit
            qs = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
            try:
                out = search_payload(
                    qs.get("q", ""),
                    start=dt.date.fromisoformat(qs["from"]) if qs.get("from") else None,
                    end=dt.date.fromisoformat(qs["to"]) if qs.get("to") else None,
                    device=qs.get("device") or None, speaker=qs.get("speaker") or None,
                    notable=qs.get("notable") == "1", actions=qs.get("actions") == "1",
                    min_minutes=float(qs.get("min_minutes") or 0))
            except ValueError:
                return self._json(400, {"error": "bad filter"})
            return self._json(200, out)
        if path == "/pendant/feed/status":
            return self._json(200, status_payload())
        m = re.fullmatch(r"/pendant/feed/day/(\d{4}-\d{2}-\d{2})", path)
        if m:
            try:
                day = dt.date.fromisoformat(m[1])
            except ValueError:
                return self._json(400, {"error": "bad date"})
            return self._json(200, day_payload(day))
        return self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):  # keep transcript text out of the journal
        pass


def make_server(port: int, token: str) -> ThreadingHTTPServer:
    Handler.token = token
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    token = token_file().read_text().strip() if token_file().exists() else ""
    if not token:
        raise SystemExit(f"no token in {token_file()}")
    try:   # an undo cut short: its files follow the database (reassign_undo.py)
        with trash.processor_lock(data_dir()):
            con = reassign_undo._open(data_dir())
            if con is not None:
                try:
                    reassign_undo.recover(con, data_dir())
                finally:
                    con.close()
    except Exception as e:  # noqa: BLE001 - the next undo or processor run tries again
        print(f"reassign undo recovery: {type(e).__name__}: {str(e)[:120]}", flush=True)
    make_server(int(os.environ.get("PENDANT_FEED_PORT", "9796")), token).serve_forever()


if __name__ == "__main__":
    main()
