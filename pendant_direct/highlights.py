"""Notable rules, Event notes and the daily Highlights file.

Rules and file shapes:

- Notable: importance 2, or >= 25 min of audio, or >= 2 action items, or a configured keyword in
  the title/overview. `notable_overrides.json` ({"<DEVICE_HEX>-<start_ms>": bool})
  wins over the rules.
- Event note: <vault>/Pendant/Events/YYYY-MM-DDTHHMM00 - <slug> - <DEVICE_HEX>-<start_ms>.md
- Starred: a conversation with a button-press star is notable (process.apply_stars).
- Highlights: <vault>/Pendant/Highlights/YYYY-MM-DD Highlights.md, rewritten whole
  from the `conversations` table, so regenerating is idempotent.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo

TZ_NAME = "America/New_York"
TZ = ZoneInfo(TZ_NAME)


def slugify(text: str, max_length: int = 50) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text).strip("-"))
    if len(text) > max_length:
        text = text[:max_length].rstrip("-")
    return text or "untitled"


def event_id(device: str, start_ms: int) -> str:
    return f"{device}-{start_ms}"


def load_overrides(path: Path) -> dict[str, bool]:
    try:
        data = json.loads(Path(path).read_text())
        return {str(k): bool(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_notable(*, key: str, audio_s: float, action_items: list, title: str, overview: str,
               rules: dict, overrides: dict[str, bool] | None = None, importance: int = 0) -> bool:
    if overrides and key in overrides:
        return overrides[key]
    if importance >= 2:
        return True
    minutes, items = rules.get("duration_minutes"), rules.get("action_items_min")
    if minutes is not None and audio_s >= minutes * 60:   # None turns the rule off
        return True
    if items is not None and len(action_items) >= items:
        return True
    hay = f"{title} {overview}".lower()
    return any(k and k.lower() in hay for k in rules.get("keywords", []))


def event_filename(device: str, start_ms: int, title: str) -> str:
    t = dt.datetime.fromtimestamp(start_ms / 1000, TZ)
    return f"{t:%Y-%m-%dT%H%M}00 - {slugify(title)} - {event_id(device, start_ms)}.md"


def _yaml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def frontmatter(data: dict) -> str:
    """YAML frontmatter, keys sorted."""
    lines = ["---"]
    for k in sorted(data):
        v = data[k]
        if isinstance(v, list):
            lines.append(f"{k}: []" if not v else f"{k}:")
            lines.extend(f"- {_yaml_value(i)}" for i in v)
        else:
            lines.append(f"{k}: {_yaml_value(v)}")
    lines.append("---\n")
    return "\n".join(lines)


def raw_link(day: str, heading: str) -> str:
    return f"[[{day}#{heading}]]"


def render_event(c: dict) -> str:
    """c: a conversations row as a dict (see process.CONVERSATIONS_SQL)."""
    start = dt.datetime.fromtimestamp(c["start_ms"] / 1000, TZ)
    end = dt.datetime.fromtimestamp(c["end_ms"] / 1000, TZ)
    link = raw_link(c["day"], c["raw_heading"])
    fm = frontmatter({
        "category": c.get("category") or "",
        "date": c["day"],
        "duration_minutes": round(c["audio_s"] / 60),
        "finished_at": end.isoformat(),
        "language": "en",
        "pendant": c["pendant"],
        "pendant_id": c["device"],
        "people": json.loads(c.get("people") or "[]"),
        "raw_daily": f"[[{c['day']}]]",
        "raw_link": link,
        "source": "pendant",
        "started_at": start.isoformat(),
    })
    items = json.loads(c.get("action_items") or "[]")
    lines = [fm, f"# {c['title']}", "", "## Summary", "",
             c.get("overview") or "*No summary available.*", "", "## Action Items", ""]
    lines += [f"- [ ] {i}" for i in items] or ["*No action items.*"]
    lines += ["", "## Link to Raw", "", link, ""]
    return "\n".join(lines)


def render_highlights(day: str, convs: list[dict], stars: list[dict] = ()) -> str:
    """stars: {"ts_ms", "raw_heading", "text"} for each star placed in one of
    the day's conversations (process.stars joined to conversations)."""
    convs = sorted(convs, key=lambda c: c["start_ms"])
    people = sorted({p for c in convs for p in json.loads(c.get("people") or "[]")})
    lines = [frontmatter({"date": day, "people": people, "source": "pendant",
                          "timezone": TZ_NAME, "type": "highlights"}),
             f"# Highlights — {day}", "", "## Notable Events", ""]

    def hhmm(c):
        return f"{dt.datetime.fromtimestamp(c['start_ms'] / 1000, TZ):%H:%M}"

    notable = [c for c in convs if c.get("notable") and c.get("event_file")]
    for c in notable:
        lines.append(f"- {hhmm(c)} — [[{c['event_file'].removesuffix('.md')}]]")
        if c.get("overview"):
            lines.append(f"  - {c['overview']}")
    if not notable:
        lines.append("*No notable events.*")

    lines += ["", "## Starred Moments", ""]
    for s in sorted(stars, key=lambda s: s["ts_ms"]):
        t = dt.datetime.fromtimestamp(s["ts_ms"] / 1000, TZ)
        line = f"- {t:%H:%M} — {raw_link(day, s['raw_heading'])}"
        lines.append(line + (f" — {s['text']}" if s.get("text") else ""))
    if not stars:
        lines.append("*No starred moments.*")

    lines += ["", "## Action Items", ""]
    items = [(c, i) for c in convs for i in json.loads(c.get("action_items") or "[]")]
    lines += [f"- [ ] {i} ({hhmm(c)}, {raw_link(day, c['raw_heading'])})" for c, i in items] \
        or ["*No action items.*"]

    lines += ["", "## All Conversations", ""]
    for c in convs:
        line = f"- {hhmm(c)} — {c['title']} → {raw_link(day, c['raw_heading'])}"
        if c.get("notable") and c.get("event_file"):
            line += f" | [[{c['event_file'].removesuffix('.md')}]]"
        if c.get("meeting_note"):
            line += f" | also in [[{c['meeting_note']}]]"
        lines.append(line)
    lines.append("")
    return "\n".join(lines)


def write_if_changed(path: Path, text: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == text:
        return False
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)
    return True
