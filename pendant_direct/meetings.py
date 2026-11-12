"""Meetings recorded by Granola or Wispr Flow, as they land in the vault.

Most of the wearer's meetings are also recorded by Granola or Wispr; after
processing, each becomes a vault note with frontmatter (`source: granola` or
`wispr`, `date`, `created_at`, `people`) and a `## Transcript` section of
`**Speaker** *(HH:MM:SS)*: text` lines. Granola labels the wearer's microphone
`Me` and everyone else `Them`; Wispr uses `Speaker 1`, `Speaker 2`, ...

A pendant conversation that overlaps one of these meetings is the same meeting
heard twice. The meeting note stays the source of truth: the pendant section
links to it and leaves follow-ups and Event notes to it.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
SOURCES = {"granola", "wispr"}
SKIP_DIRS = {"Pendant", "zArchive", ".obsidian", ".trash", "data", "Attachments", ".git"}
UTTERANCE = re.compile(r"^\*\*(?P<who>[^*]{1,60})\*\*\s*\*\((?P<t>\d{1,2}:\d{2}(?::\d{2})?)\)\*:\s*(?P<text>.*)$")
CREATED_AT = re.compile(r"(\d{4}-\d{2}-\d{2})\s+at\s+(\d{1,2}:\d{2})")
NO_TRANSCRIPT_MIN = 30  # assumed length when a note has no timed transcript
MIN_OVERLAP = 0.5       # share of the shorter of the two spans


@dataclass
class Meeting:
    path: Path
    title: str
    source: str
    start_ms: int
    end_ms: int
    people: list[str] = field(default_factory=list)
    utterances: list[tuple[int, str, str]] = field(default_factory=list)  # (ms, speaker, text)
    ref: str | None = None      # vault path without .md, when another note has the same name

    @property
    def link(self) -> str:
        """Obsidian link target (note name without .md, or its vault path when the name is
        shared: Granola and Wispr can each file a note under the same name)."""
        return self.ref or self.path.stem


def _frontmatter(text: str) -> dict[str, str | list[str]]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out: dict[str, str | list[str]] = {}
    key = None
    for line in text[3:end].splitlines():
        if not line.strip():
            continue
        if line.startswith("- ") and key:
            v = out.get(key)
            out[key] = (v if isinstance(v, list) else []) + [line[2:].strip().strip("'\"")]
            continue
        if ":" in line and not line.startswith(" "):
            key, _, v = line.partition(":")
            key = key.strip()
            out[key] = v.strip().strip("'\"")
    return out


def _ms(day: dt.date, hhmmss: str) -> int:
    parts = [int(p) for p in hhmmss.split(":")] + [0]
    t = dt.datetime.combine(day, dt.time(parts[0] % 24, parts[1], parts[2] if len(parts) > 3 else 0), TZ)
    return int(t.timestamp() * 1000)


WORDS_PER_MIN = 150
MAX_GAP_MS = 30 * 60_000


def span(utts: list[tuple[int, str, str]]) -> tuple[int, int]:
    """Meeting span from utterance times. Notes can stamp every line with the
    same time (then length comes from the word count) or append a later session
    to the same note (the span stops at the first gap over 30 minutes)."""
    times = sorted(t for t, _, _ in utts)
    start = end = times[0]
    for t in times[1:]:
        if t - end > MAX_GAP_MS:
            break
        end = t
    words = sum(len(text.split()) for _, _, text in utts)
    return start, max(end + 30_000, start + int(words / WORDS_PER_MIN * 60_000))


def parse(path: Path, text: str) -> Meeting | None:
    """A Meeting from a vault note, or None if it isn't a Granola/Wispr meeting."""
    fm = _frontmatter(text)
    source = str(fm.get("source", "")).lower()
    if source not in SOURCES:
        return None
    try:
        day = dt.date.fromisoformat(str(fm.get("date", ""))[:10])
    except ValueError:
        return None
    utts: list[tuple[int, str, str]] = []
    in_transcript = False
    for line in text.splitlines():
        if line.startswith("#"):
            in_transcript = line.lstrip("#").strip().lower() == "transcript"
            continue
        if in_transcript:
            m = UTTERANCE.match(line.strip())
            if m:
                utts.append((_ms(day, m["t"]), m["who"].strip(), m["text"].strip()))
    if utts:
        start, end = span(utts)
    else:
        m = CREATED_AT.search(str(fm.get("created_at", "")))
        if not m:
            return None
        start = _ms(dt.date.fromisoformat(m[1]), m[2])
        end = start + NO_TRANSCRIPT_MIN * 60_000
    people = fm.get("people")
    return Meeting(path=path, title=str(fm.get("title") or path.stem), source=source,
                   start_ms=start, end_ms=end, people=people if isinstance(people, list) else [],
                   utterances=utts)


def load(vault: Path, days: set[dt.date]) -> list[Meeting]:
    """Granola/Wispr meetings dated on any of `days` (cheap: reads only note headers
    until a match)."""
    wanted = {d.isoformat() for d in days}
    out = []
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if not name.endswith(".md"):
                continue
            p = Path(root) / name
            try:
                with p.open(encoding="utf-8", errors="replace") as f:
                    head = f.read(1500)
            except OSError:
                continue
            if "source:" not in head or not any(f"date: {d}" in head or f"date: '{d}'" in head for d in wanted):
                continue
            if not re.search(r"^source:\s*'?(granola|wispr)", head, re.M):
                continue
            try:
                m = parse(p, p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if m:
                out.append(m)
    stems = [m.path.stem for m in out]
    for m in out:
        if stems.count(m.path.stem) > 1:
            m.ref = m.path.relative_to(vault).with_suffix("").as_posix()
    return out


def overlap_share(a0: int, a1: int, b0: int, b1: int) -> float:
    shared = min(a1, b1) - max(a0, b0)
    shorter = max(1, min(a1 - a0, b1 - b0))
    return max(0.0, shared / shorter)


def find(meetings: list[Meeting], start_ms: int, end_ms: int) -> Meeting | None:
    """The meeting this pendant span was part of, if more than half of the
    shorter span overlaps."""
    best = max(meetings, key=lambda m: overlap_share(start_ms, end_ms, m.start_ms, m.end_ms), default=None)
    if best and overlap_share(start_ms, end_ms, best.start_ms, best.end_ms) > MIN_OVERLAP:
        return best
    return None


def find_all(meetings: list[Meeting], start_ms: int, end_ms: int) -> list[Meeting]:
    """Every meeting note this pendant span was part of (Granola and Wispr often both
    recorded it), best overlap first."""
    hits = [m for m in meetings if overlap_share(start_ms, end_ms, m.start_ms, m.end_ms) > MIN_OVERLAP]
    return sorted(hits, key=lambda m: (-overlap_share(start_ms, end_ms, m.start_ms, m.end_ms), m.link))


# --- Phase 2: names for pendant speakers from the meeting transcript -------------

WORD = re.compile(r"[a-z']{3,}")
STOP = set("the and that this with you your for are was were have has had not but they them then there "
           "what when where which who will would could should can just like yeah okay know think going "
           "about from into it's i'm don't that's".split())
ALIGN_WINDOW_MS = 120_000
MIN_SHARED_WORDS = 6
MIN_MARGIN = 1.5


def _words(text: str) -> set[str]:
    return {w for w in WORD.findall(text.lower()) if w not in STOP}


def meeting_names(m: Meeting, wearer: str = "Me") -> dict[str, str]:
    """Names for the meeting's speaker labels that we can trust. Granola labels
    the wearer's microphone `Me`; in a 1:1 (title "the wearer-Kai ..." or a single
    attendee) `Them` is the other person. Wispr's `Speaker N` carry no names."""
    names: dict[str, str] = {}
    if m.source != "granola":
        return names
    names["Me"] = wearer
    other = None
    first = re.escape(wearer.split()[0])     # titles say "Noah-Kai", the wearer may be "Noah Park"
    me = {wearer.lower(), wearer.split()[0].lower()}
    t = re.match(rf"^{first}\s*[-/&]\s*([A-Z][a-z]+)\b", m.title) or re.match(rf"^([A-Z][a-z]+)\s*[-/&]\s*{first}\b", m.title)
    if t:
        other = t[1]
    elif len([p for p in m.people if p.lower() not in me]) == 1:
        other = next(p for p in m.people if p.lower() not in me)
    if other and len({w for _, w, _ in m.utterances} - {"Me"}) <= 1:
        names["Them"] = other
    return names


def map_speakers(paragraphs: list[tuple[int, str | None, str]], m: Meeting,
                 names: dict[str, str]) -> dict[str, str]:
    """{pendant speaker tag: name} by aligning pendant paragraphs
    [(ms, tag, text)] with the meeting's utterances: words a pendant speaker
    shares with a meeting speaker talking within ALIGN_WINDOW_MS. A tag is named
    only when one meeting speaker clearly dominates (MIN_MARGIN) on enough shared
    words."""
    scores: dict[str, dict[str, int]] = {}
    for ts, tag, text in paragraphs:
        if not tag:
            continue
        mine = _words(text)
        near = [(who, _words(t)) for ut, who, t in m.utterances if abs(ut - ts) <= ALIGN_WINDOW_MS]
        for who, theirs in near:
            if who in names:
                scores.setdefault(tag, {}).setdefault(who, 0)
                scores[tag][who] += len(mine & theirs)
    out: dict[str, str] = {}
    for tag, by_who in scores.items():
        ranked = sorted(by_who.items(), key=lambda kv: -kv[1])
        best, n = ranked[0]
        runner = ranked[1][1] if len(ranked) > 1 else 0
        if n >= MIN_SHARED_WORDS and n >= MIN_MARGIN * max(runner, 1):
            out[tag] = names[best]
    # one name per pendant speaker and per person
    taken: dict[str, str] = {}
    for tag, name in sorted(out.items(), key=lambda kv: -sum(scores[kv[0]].values())):
        if name not in taken.values():
            taken[tag] = name
    return taken


SPEAKER_PARA = re.compile(r"^\[(\d{1,2}):(\d{2})\]\s*(?:⭐\s*)?(?:\*\*(?P<tag>[^*\n]{1,40}):\*\*)?\s*(?P<text>.*)$")


def section_paragraphs(body: str, day: dt.date) -> list[tuple[int, str | None, str]]:
    """[(ms, tag, text)] from a Raw section body ("[HH:MM] **Speaker A:** text")."""
    out = []
    for line in body.splitlines():
        m = SPEAKER_PARA.match(line.strip())
        if m:
            t = dt.datetime.combine(day, dt.time(int(m[1]), int(m[2])), TZ)
            out.append((int(t.timestamp() * 1000), m["tag"], m["text"]))
    return out


def rename_tags(body: str, mapping: dict[str, str]) -> str:
    for tag, name in mapping.items():
        body = body.replace(f"**{tag}:**", f"**{name}:**")
    return body
