"""Per-conversation title, overview, action items and category.

Transcripts are summarized by DeepSeek on Fireworks (`fireworks`, the default),
never by Claude. `heuristic` (local: the first ~8 words as title, the first two
sentences as overview, no action items) is the fallback when DeepSeek is
unavailable or fails, and can be chosen explicitly. Neither keys nor transcript
text are ever logged.

With a `pages.db` connection (`con`), DeepSeek results are cached by
sha256(model, PROMPT_VERSION, text) in `summaries`, and every Fireworks call appends its
token usage to `llm_usage`. Heuristic fallbacks are never cached. Cache and usage
problems are logged and ignored: they never block a summary.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config

TITLE_WORDS = 8
MAX_TITLE = 80
SPEAKER = re.compile(r"\*\*[^*\n]{1,40}:\*\*\s*")
STAMP = re.compile(r"\[\d{1,2}:\d{2}\]\s*")


@dataclass
class Summary:
    title: str
    overview: str = ""
    action_items: list[str] = field(default_factory=list)
    category: str = ""
    kind: str | None = None        # conversation | media | fragment; None = no model opinion
    importance: int | None = None  # 0 routine, 1 worth a look, 2 important; None = no model opinion


def clean_title(title: str) -> str:
    """One line, safe inside an Obsidian heading and [[link#heading]]."""
    t = re.sub(r"[#\[\]|^]", "", title)
    t = re.sub(r"\s+", " ", t).strip(" -—:;,")
    if len(t) > MAX_TITLE:
        t = t[:MAX_TITLE].rsplit(" ", 1)[0].rstrip(" -—:;,")
    return t or "Untitled conversation"


def plain(text: str) -> str:
    """Transcript text without [HH:MM] stamps or **Speaker:** labels."""
    return re.sub(r"\s+", " ", SPEAKER.sub("", STAMP.sub("", text))).strip()


def heuristic(text: str) -> Summary:
    words = [w for w in re.sub(r"[#\[\]|^]", "", plain(text)).split() if w]
    title = " ".join(w[:1].upper() + w[1:] for w in words[:TITLE_WORDS])
    title = clean_title(title.rstrip(".,!?;:"))
    sentences = re.split(r"(?<=[.!?])\s+", plain(text))
    overview = " ".join(sentences[:2]).strip()
    if len(overview) > 400:
        overview = overview[:400].rsplit(" ", 1)[0] + "…"
    return Summary(title=title, overview=overview)


# Bump whenever SYSTEM or SCHEMA changes: it is part of the cache key, so old results
# stop matching. tests/test_llm_cache.py pins a hash of both and fails until you bump.
PROMPT_VERSION = "1"

SQL = """
CREATE TABLE IF NOT EXISTS summaries (key TEXT PRIMARY KEY, summary TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS llm_usage (ts REAL NOT NULL, purpose TEXT, model TEXT, prompt_tokens INT,
    completion_tokens INT, reasoning_tokens INT, cached_tokens INT, ok INT);
CREATE INDEX IF NOT EXISTS llm_usage_ts ON llm_usage (ts);
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "overview": {"type": "string"},
        "action_items": {"type": "array", "items": {"type": "string"}},
        "category": {"type": "string"},
        "kind": {"type": "string", "enum": ["conversation", "media", "fragment"]},
        "importance": {"type": "integer", "enum": [0, 1, 2]},
    },
    "required": ["title", "overview", "action_items", "category", "kind", "importance"],
    "additionalProperties": False,
}

SYSTEM = """You summarize transcripts recorded by a wearable microphone worn by {W}.
Speaker labels, when present, come from automatic voice recognition and can be wrong.
Return:
- title: 3 to 8 words, Title Case, specific to what was discussed. No quotes or # [ ] | ^ characters.
- overview: 1 to 3 plain sentences on what happened and anything decided.
- action_items: ONLY follow-ups that someone states outright as a task: {W} says he will
  do something ("I'll call the plumber", "I need to email Sam"), someone asks him to ("can you
  send me that"), or he asks to be reminded ("remind me to..."). Each a short imperative phrase.
  Do NOT include anything implied, inferred, suggested by the topic, speculative ("maybe we
  should"), already done, or general intentions without a concrete task. Most conversations
  have none; an empty list is the normal answer. At most 3.
  An action item must be real work for {W} to do LATER, beyond this conversation. Never
  include in-the-moment requests that get handled right there: passing, grabbing or fetching
  something, coming over, quick favours ("pass the salt", "grab the food coloring", "can you
  hold this"). Never include instructions {W} is giving to an AI assistant, coding agent,
  computer or device while working ("use Opus for the leads", "spin up a worktree") unless he
  asks to be reminded or to add a task for himself.
- category: one lowercase word such as work, family, health, finance, errands, social,
  personal, technology, media, or other.
- kind: what the recording is.
  conversation: people really talking in the room or on a call with {W}, or {W} talking
    to himself or dictating. Includes small talk and logistics.
  media: audio from a TV, movie, video, podcast, radio, audiobook, music or a livestream playing
    nearby, not people talking to each other. Scripted or narrated speech, ads, theme songs.
  fragment: too little to be a recording worth keeping: a few unclear words, one remark with no
    context, mumbling, background noise, or text so garbled the topic can't be told.
  Examples: "Unintelligible Background Audio Clip", "Brief Unclear Fragment With No Content" and
  "Unclear Remark About Barn End" are fragments; a news anchor reading headlines is media; two
  people planning dinner is a conversation.
- importance: 0 routine (chores, small talk, logistics, nothing he'd look up again); 1 worth a
  look (a plan, a decision in progress, real family or work discussion, information he may want
  later); 2 important (a firm commitment or deadline, a major decision, health, money, legal or
  safety matters, a significant emotional conversation). Most recordings are 0 or 1; use 2
  rarely. A media or fragment recording is always 0."""


OPENCODE_AUTH = Path.home() / ".local" / "share" / "opencode" / "auth.json"
FIREWORKS_URL = "https://api.fireworks.ai/inference/v1/chat/completions"
FIREWORKS_MODEL = "accounts/fireworks/models/deepseek-v4p1-flash-1017"


def fireworks_key() -> str | None:
    """FIREWORKS_API_KEY, else the key OpenCode uses (auth.json, fireworks-ai)."""
    if os.environ.get("FIREWORKS_API_KEY"):
        return os.environ["FIREWORKS_API_KEY"]
    try:
        return json.loads(OPENCODE_AUTH.read_text())["fireworks-ai"]["key"] or None
    except (OSError, KeyError, ValueError, TypeError):
        return None


def parse_summary(data: dict) -> Summary:
    items = [re.sub(r"\s+", " ", str(i)).strip() for i in data.get("action_items") or []][:3]
    kind = str(data.get("kind", "")).strip().lower()
    imp = data.get("importance")
    if isinstance(imp, str) and imp.strip().isdigit():
        imp = int(imp)
    return Summary(title=clean_title(str(data.get("title", ""))),
                   overview=re.sub(r"\s+", " ", str(data.get("overview", ""))).strip(),
                   action_items=[i for i in items if i],
                   category=re.sub(r"[^a-z-]", "", str(data.get("category", "")).lower())[:30],
                   kind=kind if kind in ("conversation", "media", "fragment") else None,
                   importance=imp if isinstance(imp, int) and not isinstance(imp, bool) and 0 <= imp <= 2 else None)


def cache_key(model: str, text: str) -> str:
    return hashlib.sha256("\0".join((model, PROMPT_VERSION, text)).encode()).hexdigest()


def _tokens(resp: dict) -> dict:
    u = resp.get("usage") or {}
    def n(*path):
        v = u
        for p in path:
            v = v.get(p) if isinstance(v, dict) else None
        return v if isinstance(v, int) and not isinstance(v, bool) else 0
    return {"prompt_tokens": n("prompt_tokens"), "completion_tokens": n("completion_tokens"),
            "reasoning_tokens": n("completion_tokens_details", "reasoning_tokens") or n("reasoning_tokens"),
            "cached_tokens": n("prompt_tokens_details", "cached_tokens") or n("cached_tokens")}


def fireworks(text: str, model: str, post=None, usage=None, wearer: str = "the wearer") -> Summary:
    """Summarize with a Fireworks-hosted model (OpenAI-compatible chat API, JSON
    schema output). `post(url, headers, body) -> dict` is injectable for tests.
    `usage(dict)` is called once per request actually sent, with token counts and `ok`.
    Raises on any problem; summarize() catches."""
    key = fireworks_key()
    if not key:
        raise RuntimeError("no Fireworks key")
    body = {
        "model": model,
        "max_tokens": 2000,
        "temperature": 0.2,
        "response_format": {"type": "json_schema", "json_schema": {"name": "summary", "schema": SCHEMA}},
        "messages": [{"role": "system", "content": SYSTEM.replace("{W}", wearer)},
                     {"role": "user", "content": f"<transcript>\n{text}\n</transcript>"}],
    }
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
        resp = post(FIREWORKS_URL, headers, body)
        tokens = _tokens(resp)
        choice = resp["choices"][0]
        if choice.get("finish_reason") == "length":
            raise RuntimeError("summary cut off")
        content = choice["message"]["content"]
        # Reasoning models may wrap the JSON in a think block or code fence.
        content = re.sub(r"(?s)<think>.*?</think>", "", content).strip()
        m = re.search(r"(?s)\{.*\}", content)
        if not m:
            raise ValueError("no JSON object in reply")
        out = parse_summary(json.loads(m.group(0)))
        ok = True
        return out
    finally:
        if usage:
            usage({**tokens, "ok": ok})


def _cache_get(con, key: str, log) -> Summary | None:
    try:
        row = con.execute("SELECT summary FROM summaries WHERE key=?", (key,)).fetchone()
        return Summary(**json.loads(row[0])) if row else None
    except Exception as e:  # a bad cache never blocks a summary
        log(f"summary cache read failed ({type(e).__name__})")
        return None


def _cache_put(con, key: str, s: Summary, log) -> None:
    try:
        con.execute("INSERT OR REPLACE INTO summaries VALUES (?,?,?)", (key, json.dumps(asdict(s)), time.time()))
        con.commit()
    except Exception as e:
        log(f"summary cache write failed ({type(e).__name__})")


def _log_usage(con, purpose: str, model: str, u: dict, log) -> None:
    try:
        con.execute("INSERT INTO llm_usage VALUES (?,?,?,?,?,?,?,?)",
                    (time.time(), purpose, model, u["prompt_tokens"], u["completion_tokens"],
                     u["reasoning_tokens"], u["cached_tokens"], int(u["ok"])))
        con.commit()
    except Exception as e:
        log(f"llm usage log failed ({type(e).__name__})")


def summarize(text: str, cfg: dict, log=print, post=None, jev_post=None, con=None,
              purpose: str = "summary") -> Summary:
    # DeepSeek unless the heuristic was chosen explicitly. Any other value (including
    # a legacy "claude") means DeepSeek: transcripts never go to Claude.
    if cfg.get("summarizer", "fireworks") != "heuristic":
        try:
            model = cfg.get("fireworks_model") or FIREWORKS_MODEL
            key = cache_key(model, text)
            s = _cache_get(con, key, log) if con is not None else None
            if s is None:
                s = fireworks(text, model, post=post, wearer=config.wearer_first(cfg.get("speakers")),
                              usage=(lambda u: _log_usage(con, purpose, model, u, log)) if con is not None else None)
                if s.title != "Untitled conversation" and con is not None:
                    _cache_put(con, key, s, log)
            if s.title != "Untitled conversation":
                if cfg.get("jev_action_items", True):
                    from . import jev
                    s.action_items = jev.keep_explicit(text, s.action_items, log=log, post=jev_post,
                                                      wearer=config.wearer_first(cfg.get("speakers")))
                return s
            log("fireworks summary had no title; using heuristic")
        except Exception as e:  # never block transcription
            log(f"fireworks summary failed ({type(e).__name__}); using heuristic")
        return heuristic(text)
    return heuristic(text)
