"""One quiet daily signal per New York day, for Dailies suggestions (`day_signals` in pages.db).

DeepSeek on Fireworks (the client constants and usage log of `summarize.py`; never Claude) reads
the day's `kind=conversation` recordings and returns:
  emotional_moment  null, or {path, valence, reason, confidence, at}: only a moment that was
                    unmistakably emotional for the wearer himself, positive or negative. Most days null.
                    `path` must be a walk down the vendored Dailies feelings wheel
                    (`dailies_wheel.json`, copied from the Home app); invalid paths are dropped.
  resonant          null or one plain sentence (<= 200 chars)
  words             0-3 single words that could be "one word for the day"

Only today and yesterday (America/New_York) are computed, and only when the day's set of
conversations (`source_key`) changed. Input is bounded (MAX_INPUT_CHARS, MAX_CONV_CHARS), keeping
higher-importance and starred conversations first. Served read-only by the feed at
`GET /pendant/feed/day-signals?day=`; shown nowhere in the app. Failures log a category only and
never block transcription, summaries or vault writes. Neither transcript nor signal text is logged.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

from . import config
from zoneinfo import ZoneInfo

from . import summarize

TZ = ZoneInfo("America/New_York")
MAX_INPUT_CHARS = 30_000      # all transcripts together
MAX_CONV_CHARS = 6_000        # one conversation
MAX_REASON, MAX_RESONANT, MAX_WORD, MAX_WORDS = 120, 200, 20, 3

# Bump whenever SYSTEM or SCHEMA changes: it is part of source_key, so the next run recomputes.
PROMPT_VERSION = "1"
MIN_REFRESH_SECONDS = 30 * 60   # at most one refresh per day per half hour

SQL = """CREATE TABLE IF NOT EXISTS day_signals (
    day TEXT PRIMARY KEY, signals TEXT NOT NULL, computed_at REAL NOT NULL, source_key TEXT NOT NULL)"""

SCHEMA = {
    "type": "object",
    "properties": {
        "has_emotional_moment": {"type": "boolean"},
        "path": {"type": "array", "items": {"type": "string"}},
        "valence": {"type": "string", "enum": ["negative", "positive", "mixed"]},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
        "at": {"type": "string"},
        "resonant": {"type": "string"},
        "words": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["has_emotional_moment", "path", "valence", "reason", "confidence", "at", "resonant", "words"],
    "additionalProperties": False,
}

SYSTEM = """You read transcripts of {W}'s day, recorded by a wearable microphone he wears. Speaker
labels come from automatic voice recognition and can be wrong. Several conversations follow, each
headed [HH:MM] title. Return a small private signal about {W}'s day. Be conservative: a wrong
guess about someone's feelings is worse than none.

- has_emotional_moment: true ONLY if at least one moment was UNMISTAKABLY emotional for {W}
  himself: he clearly shows or says that he felt something strongly (joy, pride, deep connection,
  relief, grief, fear, anger, shame, overwhelm). Positive moments count as much as negative ones.
  Other people's feelings, topics that merely sound heavy, ordinary stress, tiredness, annoyance,
  small talk, work talk, and anything you would have to infer do NOT count. On most days the answer
  is false. When unsure, false. If true, describe the single strongest moment; if false, use
  path [], valence "mixed", reason "", confidence 0, at "".
- path: {W}'s feeling for that moment as a walk down the feelings wheel, core feeling first, then
  optionally a secondary and a tertiary one (1 to 3 names), using ONLY these exact names:
{wheel}
- valence: negative, positive or mixed.
- reason: at most 120 characters, plain words saying what happened ("Tense call about the move").
  No direct quotes from the transcript.
- confidence: 0.0 to 1.0, how sure you are this was unmistakably emotional for {W} himself.
- at: HH:MM, 24-hour, of that moment (from the [HH:MM] heading or stamps).
- resonant: the single most emotionally resonant thing from the day in one plain sentence of at most
  200 characters (first person is fine), or "" if nothing stands out. No quotes from the transcript.
- words: 0 to 3 single words (letters only, at most 20 characters each) that could be {W}'s "one
  word for the day", for example Steady or Busy. [] if the day gives no basis."""


@lru_cache(maxsize=1)
def wheel() -> tuple:
    """The vendored feelings wheel: ((core, ((secondary, (tertiary, ...)), ...)), ...)."""
    raw = json.loads((Path(__file__).with_name("dailies_wheel.json")).read_text())
    return tuple((c["name"], tuple((s["name"], tuple(t["name"] for t in s["children"])) for s in c["children"]))
                 for c in raw)


def valid_path(path) -> bool:
    """A walk from a core feeling down 1 to 3 levels, by exact name."""
    if not isinstance(path, list) or not 1 <= len(path) <= 3 or not all(isinstance(n, str) for n in path):
        return False
    core = next((c for c in wheel() if c[0] == path[0]), None)
    if core is None:
        return False
    if len(path) == 1:
        return True
    sec = next((x for x in core[1] if x[0] == path[1]), None)
    if sec is None:
        return False
    return len(path) == 2 or path[2] in sec[1]


def system_prompt(wearer: str = "the wearer") -> str:
    lines = []
    for core, secondaries in wheel():
        lines.append(f"  {core}: " + "; ".join(f"{s} ({', '.join(t)})" for s, t in secondaries))
    return SYSTEM.replace("{wheel}", "\n".join(lines)).replace("{W}", wearer)


def logical_days(now: dt.datetime | None = None) -> list[str]:
    today = (now or dt.datetime.now(TZ)).astimezone(TZ).date()
    return [f"{today:%Y-%m-%d}", f"{today - dt.timedelta(days=1):%Y-%m-%d}"]


def gather(con: sqlite3.Connection, notes_dir: Path, day: str) -> tuple[list[dict], str]:
    """(conversations, source_key) for a day: kind=conversation rows with their transcript text from
    the vault file. The key changes when a conversation is added, removed, retimed or retitled."""
    from . import feed   # parse_daily; feed imports this module lazily
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT device, start_ms, end_ms, title, raw_heading, importance, notable, kind FROM conversations "
            "WHERE day=? ORDER BY start_ms", (day,))]
        starred = {(r[0], r[1]) for r in con.execute(
            "SELECT conv_device, conv_start_ms FROM stars WHERE conv_device IS NOT NULL")}
    finally:
        con.row_factory = None
    rows = [r for r in rows if (r["kind"] or "conversation") == "conversation"]
    key = hashlib.sha256(json.dumps([PROMPT_VERSION] + [[r["device"], r["start_ms"], r["end_ms"], r["title"]]
                                                       for r in rows]).encode()).hexdigest()
    f = notes_dir / f"{day}.md"
    texts = {c["heading"]: c for c in feed.parse_daily(f.read_text())} if f.exists() and rows else {}
    convs = []
    for r in rows:
        c = texts.get(r["raw_heading"])
        if c and c["text"].strip():
            convs.append({"start_ms": r["start_ms"], "title": r["title"] or "", "text": c["text"],
                          "rank": ((r["importance"] or 0) + (1 if (r["device"], r["start_ms"]) in starred or r["notable"] else 0),
                                   len(c["text"]))})
    return convs, key


def build_input(convs: list[dict]) -> str:
    """Highest-ranked conversations first until MAX_INPUT_CHARS is used up; each cut to
    MAX_CONV_CHARS; the kept ones then read in time order."""
    kept, used = [], 0
    for c in sorted(convs, key=lambda c: c["rank"], reverse=True):
        block = f"[{dt.datetime.fromtimestamp(c['start_ms'] / 1000, TZ):%H:%M}] {c['title']}\n{c['text'][:MAX_CONV_CHARS]}"
        if used + len(block) > MAX_INPUT_CHARS:
            block = block[:max(0, MAX_INPUT_CHARS - used)]
            if len(block) < 200:
                continue
        kept.append((c["start_ms"], block))
        used += len(block)
        if used >= MAX_INPUT_CHARS:
            break
    return "\n\n".join(b for _s, b in sorted(kept))


def parse(day: str, data: dict) -> dict:
    """The model's reply as the served JSON; anything invalid is dropped, never repaired."""
    moment = None
    path = data.get("path")
    conf = data.get("confidence")
    if (data.get("has_emotional_moment") is True and valid_path(path)
            and isinstance(conf, (int, float)) and not isinstance(conf, bool)
            and data.get("valence") in ("negative", "positive", "mixed")):
        at = str(data.get("at") or "").strip()
        moment = {"path": list(path), "valence": data["valence"],
                  "reason": re.sub(r"\s+", " ", str(data.get("reason") or "")).strip()[:MAX_REASON].strip(),
                  "confidence": round(min(1.0, max(0.0, float(conf))), 2),
                  "at": at if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at) else None}
    resonant = re.sub(r"\s+", " ", str(data.get("resonant") or "")).strip()[:MAX_RESONANT].strip() or None
    words: list[str] = []
    for w in data.get("words") if isinstance(data.get("words"), list) else []:
        w = str(w).strip()
        if re.fullmatch(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", w) and len(w) <= MAX_WORD and w.lower() not in {x.lower() for x in words}:
            words.append(w)
    return {"day": day, "emotional_moment": moment, "resonant": resonant, "words": words[:MAX_WORDS]}


def ask(day: str, text: str, model: str, post=None, usage=None, wearer: str = "the wearer") -> dict:
    """One DeepSeek call. Raises on any problem; compute() catches."""
    key = summarize.fireworks_key()
    if not key:
        raise RuntimeError("no Fireworks key")
    body = {"model": model, "max_tokens": 2000, "temperature": 0.1,
            "response_format": {"type": "json_schema", "json_schema": {"name": "day_signals", "schema": SCHEMA}},
            "messages": [{"role": "system", "content": system_prompt(wearer)},
                         {"role": "user", "content": f"<transcripts>\n{text}\n</transcripts>"}]}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if post is None:
        def post(url, headers, body):
            import urllib.request
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
    tokens = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0}
    ok = False
    try:
        resp = post(summarize.FIREWORKS_URL, headers, body)
        tokens = summarize._tokens(resp)
        choice = resp["choices"][0]
        if choice.get("finish_reason") == "length":
            raise RuntimeError("cut off")
        content = re.sub(r"(?s)<think>.*?</think>", "", choice["message"]["content"]).strip()
        m = re.search(r"(?s)\{.*\}", content)
        if not m:
            raise ValueError("no JSON object in reply")
        out = parse(day, json.loads(m.group(0)))
        ok = True
        return out
    finally:
        if usage:
            usage({**tokens, "ok": ok})


def compute(con: sqlite3.Connection, notes_dir: Path, cfg: dict, log=print, post=None,
            now: dt.datetime | None = None) -> int:
    """Refresh today's and yesterday's signals where the day's conversation set changed. Returns
    the number of rows written. Never raises; logs categories only."""
    if cfg.get("summarizer", "fireworks") == "heuristic":
        return 0
    written = 0
    try:
        con.execute(SQL)
        model = cfg.get("fireworks_model") or summarize.FIREWORKS_MODEL
        for day in logical_days(now):
            try:
                convs, key = gather(con, notes_dir, day)
                row = con.execute("SELECT source_key, computed_at FROM day_signals WHERE day=?", (day,)).fetchone()
                if row and row[0] == key:
                    continue
                # Conversations land every few minutes; the reader (LifeOS) polls every 30, so a
                # changed day is refreshed at most that often and the processing run stays short.
                if row and time.time() - row[1] < MIN_REFRESH_SECONDS:
                    continue
                if not convs and not row:
                    continue   # nothing recorded and nothing stored: no row, no call
                if convs:
                    signals = ask(day, build_input(convs), model, post=post, wearer=config.wearer_first(cfg.get("speakers")),
                                  usage=lambda u: summarize._log_usage(con, "day_signals", model, u, log))
                else:
                    signals = {"day": day, "emotional_moment": None, "resonant": None, "words": []}
                con.execute("INSERT OR REPLACE INTO day_signals VALUES (?,?,?,?)",
                            (day, json.dumps(signals), time.time(), key))
                con.commit()
                written += 1
            except Exception as e:  # one day failing never blocks the other or anything else
                log(f"day signals failed ({type(e).__name__})")
    except Exception as e:
        log(f"day signals failed ({type(e).__name__})")
    return written


def read(con: sqlite3.Connection, day: str) -> dict | None:
    """The stored signals for a day, or None when not computed."""
    try:
        row = con.execute("SELECT signals, computed_at FROM day_signals WHERE day=?", (day,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return {**json.loads(row[0]), "computed_at": row[1]} if row else None
