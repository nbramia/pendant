"""LifeOS tasks, created only when the wearer asks.

Two explicit triggers, never automatic:
- the app's "Add to tasks" button on an action item (`POST /pendant/feed/tasks`);
- The wearer asking out loud ("add buy coffee filters to my to-do list", "remind me to ...").
  A phrase pass finds candidate sentences in paragraphs he spoke, the task text
  is cut from the sentence, and Jev must answer yes (>= ASK_MIN) to "did the
  wearer explicitly ask to add something to his task list here?". Mentions
  ("I have a lot on my to-do list") fail that check.

Every request is a `task_requests` row (unique on device, start_ms, item), so
the same item never makes two tasks. A request stays `pending` while LifeOS
(localhost:8000, `POST /api/tasks`) is unreachable and is retried by the
processor. Each task links back with `[[YYYY-MM-DD#<Raw heading>]]`. Spoken
tasks are also listed under their conversation: `Added to tasks: <item>`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
LIFEOS_API = "http://localhost:8000"
ASK_MIN = 0.8
LOOKBACK_DAYS = 7  # unjudged conversations are retried this long, so a Jev outage doesn't age them out
MAX_ITEM = 200
SOURCE = "pendant"
ADDED_PREFIX = "Added to tasks: "

SQL = """
CREATE TABLE IF NOT EXISTS task_requests (
    device TEXT, start_ms INT, item TEXT, source TEXT, lifeos_task_id TEXT,
    status TEXT DEFAULT 'pending', created_at REAL,
    PRIMARY KEY (device, start_ms, item));
CREATE TABLE IF NOT EXISTS task_scans (device TEXT, start_ms INT, PRIMARY KEY (device, start_ms));
"""


class LifeOSDown(Exception):
    """LifeOS unreachable or erroring: keep the request and retry."""


class LifeOSRejected(Exception):
    """LifeOS refused the task (4xx): retrying won't help."""


def lifeos_post(path: str, body: dict, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(f"{LIFEOS_API}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise (LifeOSRejected if 400 <= e.code < 500 else LifeOSDown)(f"HTTP {e.code}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LifeOSDown(type(e).__name__) from e


def conversation_link(day: str, heading: str) -> str:
    return f"[[{day}#{heading}]]"


def clean_item(item: str) -> str:
    item = re.sub(r"\s+", " ", item).strip(" \t.,;:!?\"'")
    return item[:MAX_ITEM].strip()


def task_id(resp: dict) -> str:
    t = resp.get("task") if isinstance(resp.get("task"), dict) else resp
    for k in ("id", "task_id", "stable_id"):
        if t.get(k):
            return str(t[k])
    raise LifeOSDown("no task id in response")


def create_in_lifeos(con, device: str, start_ms: int, item: str, post=None) -> str:
    """Create the task for a stored request and return its LifeOS id.
    `operation_key` makes the create retry-safe on LifeOS's side too."""
    row = con.execute("SELECT day, raw_heading FROM conversations WHERE device=? AND start_ms=?",
                      (device, start_ms)).fetchone()
    if not row:
        raise KeyError("no such conversation")
    key = hashlib.sha1(f"{device}|{start_ms}|{item}".encode()).hexdigest()[:20]
    body = {"description": item,
            "notes": f"Source: {SOURCE}. From pendant conversation {conversation_link(*row)}",
            "fields": {"source": SOURCE},
            "operation_key": f"pendant:{key}"}
    return task_id((post or lifeos_post)("/api/tasks", body))


def request_task(con, device: str, start_ms: int, item: str, source: str, post=None) -> dict:
    """Record the request (once) and try to create the task. Returns
    {"status": created|pending|failed, "task_id": id|None}; never raises for a
    LifeOS outage (the row stays pending). KeyError if the conversation is unknown."""
    item = clean_item(item)
    if not item:
        raise ValueError("empty item")
    con.executescript(SQL)
    if not con.execute("SELECT 1 FROM conversations WHERE device=? AND start_ms=?",
                       (device, start_ms)).fetchone():
        raise KeyError("no such conversation")
    con.execute("INSERT OR IGNORE INTO task_requests (device, start_ms, item, source, created_at) "
                "VALUES (?,?,?,?,?)", (device, start_ms, item, source, time.time()))
    con.commit()
    status, tid = con.execute("SELECT status, lifeos_task_id FROM task_requests "
                              "WHERE device=? AND start_ms=? AND item=?", (device, start_ms, item)).fetchone()
    if status == "created":
        return {"status": "created", "task_id": tid}
    try:
        tid = create_in_lifeos(con, device, start_ms, item, post)
        status = "created"
    except LifeOSRejected:
        tid, status = None, "failed"
    except LifeOSDown:
        tid, status = None, "pending"
    con.execute("UPDATE task_requests SET status=?, lifeos_task_id=? WHERE device=? AND start_ms=? AND item=?",
                (status, tid, device, start_ms, item))
    con.commit()
    return {"status": status, "task_id": tid}


def retry_pending(con, post=None, log=print) -> int:
    """Re-attempt requests kept while LifeOS was down. Returns how many were created."""
    con.executescript(SQL)
    rows = con.execute("SELECT device, start_ms, item, source FROM task_requests "
                       "WHERE status='pending'").fetchall()
    made = 0
    for device, start_ms, item, source in rows:
        out = request_task(con, device, start_ms, item, source, post)
        if out["status"] == "created":
            made += 1
        elif out["status"] == "pending":
            log("tasks: LifeOS unreachable; keeping pending requests")
            break
    return made


def tasked_items(con, device: str, start_ms: int) -> list[str]:
    """Items already requested for a conversation (created or queued)."""
    return [r[0] for r in con.execute(
        "SELECT item FROM task_requests WHERE device=? AND start_ms=? AND status IN ('created','pending') "
        "ORDER BY created_at", (device, start_ms))]


# --- spoken requests ---------------------------------------------------------

LIST = r"(?:to-?do|todo|task|tasks|reminder|reminders)(?: list)?|list"
ASK = re.compile(r"\b(?:to-?do|todo|task list|tasks list|my list|add (?:that|this|it)\b|remind me|"
                 r"make .{1,80}? (?:a |into a )(?:task|to-?do)|put .{1,80} on (?:my|the))", re.I)
PRONOUN = re.compile(r"^(?:that|this|it|that one|this one)$", re.I)
EXTRACT = (
    re.compile(r"\bremind me (?:to |about |of )?(?P<what>.+)", re.I),
    re.compile(r"\b(?:add|put|throw|stick)\s+(?P<what>.+?)\s+(?:to|on|onto|in|into)\s+(?:my|the)\s+"
               rf"(?:{LIST})\b", re.I),
    re.compile(r"\bmake\s+(?P<what>.+?)\s+(?:a|into a)\s+(?:task|to-?do)\b", re.I),
)
LABEL = re.compile(r"^\*\*([^*\n]{1,40}):\*\*\s*")
STAMP = re.compile(r"^\[(\d{1,2}):(\d{2})\]\s+")
SENTENCE = re.compile(r"(?<=[.!?])\s+")


def parse_paragraphs(lines: list[str]) -> list[tuple[str, str | None, str]]:
    """[(HH:MM, speaker label or None, text)] from a Raw section's `[HH:MM] ...` lines."""
    out = []
    for line in lines:
        m = STAMP.match(line.strip())
        if not m:
            continue
        rest = line.strip()[m.end():]
        lab = LABEL.match(rest)
        out.append((f"{int(m[1]):02d}:{m[2]}", lab[1] if lab else None, rest[lab.end():] if lab else rest))
    return out


def wearer_paragraphs(paras, wearer: str) -> list[str]:
    """Texts explicitly labeled as the wearer. Without speaker labels nobody can be
    told apart, so unlabeled conversations yield nothing (never someone else's request)."""
    me = wearer.lower()
    return [x for _t, label, x in paras if label and label.lower() == me]


def candidates(paragraphs: list[str]) -> list[tuple[str, str]]:
    """[(item, span)]: for each sentence that asks for a task, the text to add
    and the surrounding sentences Jev judges. "add that" takes the previous sentence."""
    out, seen = [], set()
    for para in paragraphs:
        sents = [s for s in SENTENCE.split(para.strip()) if s]
        for i, s in enumerate(sents):
            if not ASK.search(s):
                continue
            item = None
            for pat in EXTRACT:
                m = pat.search(s)
                if m:
                    item = clean_item(m["what"])
                    break
            if item and PRONOUN.match(item):
                item = clean_item(sents[i - 1]) if i else ""
            if not item or item.lower() in seen:
                continue
            seen.add(item.lower())
            out.append((item, " ".join(sents[max(0, i - 2):i + 2])))
    return out


def scan_text(paras, wearer: str, ask, log=print) -> list[str]:
    """Items the wearer explicitly asked to add. `ask(span, item) -> probability`;
    raises propagate (the caller retries the conversation later)."""
    items = []
    for item, span in candidates(wearer_paragraphs(paras, wearer)):
        p = ask(span, item)
        if p >= ASK_MIN:
            items.append(item)
        else:
            log(f"tasks: not a request (p={p:.2f}): {item[:60]}")
    return items


def section_lines(daily: Path, heading: str) -> list[str] | None:
    if not daily.exists():
        return None
    lines = daily.read_text().split("\n")
    for i, line in enumerate(lines):
        if line == f"## {heading}":
            j = i + 1
            while j < len(lines) and not lines[j].startswith("## "):
                j += 1
            return lines[i + 1:j]
    return None


def add_task_line(daily: Path, heading: str, item: str) -> None:
    """List `Added to tasks: <item>` under the conversation's heading (once)."""
    if not daily.exists():
        return
    line = ADDED_PREFIX + item
    lines = daily.read_text().split("\n")
    for i, l in enumerate(lines):
        if l == f"## {heading}":
            end = i + 1
            while end < len(lines) and not lines[end].startswith("## "):
                if lines[end] == line:
                    return
                end += 1
            pos = i + 1
            while pos + 1 < len(lines) and lines[pos] == "" and lines[pos + 1].startswith(ADDED_PREFIX):
                pos += 2
            lines[pos:pos] = ["", line]
            tmp = daily.with_suffix(".md.tmp")
            tmp.write_text("\n".join(lines))
            tmp.replace(daily)
            return


def spoken_pass(con, notes_dir: Path, wearer: str, days: int = LOOKBACK_DAYS, ask=None, post=None, log=print) -> set[str]:
    """Retry kept requests, then scan the last `days` days (default 7) of conversations not
    yet judged for spoken requests. A conversation is marked scanned only after
    every candidate got a verdict, so Jev outages retry next run. Returns days changed."""
    from . import jev
    con.executescript(SQL)
    retry_pending(con, post, log)
    if ask is None:
        if not jev.api_key():
            return set()
        ask = jev.asked_to_add
    since = (dt.datetime.now(TZ).date() - dt.timedelta(days=days)).isoformat()
    rows = con.execute(
        "SELECT device, start_ms, day, raw_heading FROM conversations c WHERE day >= ? AND NOT EXISTS "
        "(SELECT 1 FROM task_scans s WHERE s.device=c.device AND s.start_ms=c.start_ms)", (since,)).fetchall()
    changed: set[str] = set()
    for device, start_ms, day, heading in rows:
        lines = section_lines(notes_dir / f"{day}.md", heading)
        if lines is None:
            continue
        try:
            items = scan_text(parse_paragraphs(lines), wearer, ask, log)
        except Exception as e:  # noqa: BLE001 - Jev down: judge again next run
            log(f"tasks: spoken-request check failed ({type(e).__name__}); will retry")
            continue
        for item in items:
            out = request_task(con, device, start_ms, item, "speech", post)
            if out["status"] == "failed":
                continue
            add_task_line(notes_dir / f"{day}.md", heading, item)
            changed.add(day)
            log(f"{day} {heading[:5]}: task requested ({out['status']}): {item[:60]}")
        con.execute("INSERT OR IGNORE INTO task_scans VALUES (?,?)", (device, start_ms))
        con.commit()
    return changed
