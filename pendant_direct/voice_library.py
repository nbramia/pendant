"""A permanent library of good voice clips per tagged person, which retention never touches.

    <data>/voice_library/<Name>/<id>.opus      4-15 s, cut from a conversation clip at the voice's best span
    <data>/voice_library/<Name>/index.json     [{id, src, kind, rank, device, start_ms, day, start_s, end_s,
                                                 dur_s, score, voiced, speech_s, added_at}, ...]

Up to CAP clips per person, ranked: 1 hand tags (a Who's this / Voice Scoring answer, a tap-to-reassign of a
line or a conversation, the tapped voice of a bulk confirm), 2 read-aloud enrollment readings, 3 auto matches
(the highest score margin). A full library replaces its lowest-ranked clip only with a higher-ranked one.
Within a rank a batch (the backfill) prefers different days and conversations, then a higher voiced share, then
longer speech. Only `speakers.allowed` names (the consent gate); never strangers (a removed name or the older "Someone"), noise or a voice
flagged `mixed`. Each clip is keyed by its `src` (`cluster:<id>`, `para:<device>:<ms>:<i>`, `enroll:<id>:<k>`),
so naming a voice again replaces its clip and moving it away from a person drops it.

    python -m pendant_direct.voice_library --backfill [--data DIR] [--dry-run]
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import sqlite3
import struct
import sys
import time
from pathlib import Path

from . import config as cfgmod
from .oggopus import OggOpusWriter

DIR = "voice_library"
CAP = 20
MIN_S, MAX_S = 4.0, 15.0
MIN_VOICED = 0.4
FRAME_S = 0.02
HAND, ENROLL, AUTO = 1, 2, 3
RANKS = {"hand": HAND, "enroll": ENROLL, "auto": AUTO}
SOMEONE = "Someone"
_NAME = re.compile(r"^[A-Za-z][A-Za-z '-]{0,39}$")
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def root(data: Path) -> Path:
    return Path(data) / DIR


def person_dir(data: Path, name: str) -> Path:
    if not _NAME.match(name or ""):
        raise ValueError("invalid name")
    return root(data) / name


def clip_id(src: str) -> str:
    return _SAFE.sub("-", src)


@contextlib.contextmanager
def _lock(data: Path):
    root(data).mkdir(parents=True, exist_ok=True)
    with open(root(data) / ".lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def load(data: Path, name: str) -> list[dict]:
    try:
        out = json.loads((person_dir(data, name) / "index.json").read_text())
        return out if isinstance(out, list) else []
    except (OSError, ValueError):
        return []


def _save(data: Path, name: str, entries: list[dict]) -> None:
    d = person_dir(data, name)
    if not entries:
        (d / "index.json").unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            d.rmdir()
        return
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ".index.json.tmp"
    tmp.write_text(json.dumps(entries, indent=1) + "\n")
    os.replace(tmp, d / "index.json")


def people(data: Path) -> list[str]:
    r = root(data)
    return sorted(p.name for p in r.iterdir() if p.is_dir() and _NAME.match(p.name)) if r.is_dir() else []


def eligible(cfg: dict, name: str) -> str | None:
    """The canonical spelling when `name` is in `speakers.allowed` (the consent gate; a LifeOS link is not consent)."""
    name = " ".join(str(name or "").split())
    if not name or name.lower() == SOMEONE.lower():
        return None
    sp = cfg.get("speakers", cfg)
    for k in sp.get("allowed") or []:       # a LifeOS link is not consent: only `allowed` counts
        if str(k).lower() == name.lower() and _NAME.match(str(k)):
            return str(k)
    return None


# ---------------------------------------------------------------------------
# cutting a clip (our Ogg Opus files hold one 20 ms packet per page)

def read_packets(path: Path) -> list[bytes]:
    data = Path(path).read_bytes()
    pos, packets, partial = 0, [], b""
    while pos + 27 <= len(data) and data[pos:pos + 4] == b"OggS":
        nseg = data[pos + 26]
        lacing = data[pos + 27:pos + 27 + nseg]
        body = pos + 27 + nseg
        for n in lacing:
            partial += data[body:body + n]
            body += n
            if n < 255:
                packets.append(partial)
                partial = b""
        pos = body
    return [p for p in packets[2:] if p]          # after OpusHead, OpusTags; no empty end-of-stream page


def cut_clip(src: Path, start_s: float, end_s: float, out: Path) -> float:
    """Write src[start_s:end_s] to out; returns its length in seconds."""
    packets = read_packets(src)
    a = max(0, int(round(start_s / FRAME_S)))
    b = min(len(packets), int(round(end_s / FRAME_S)))
    packets = [p for p in packets[a:b] if p]
    if not packets:
        raise ValueError("empty span")
    w = OggOpusWriter(out)
    for p in packets:
        w.add_packet(p)
    w.close()
    return len(packets) * FRAME_S


# ---------------------------------------------------------------------------
# ranking

def quality(c: dict) -> tuple:
    voiced, speech = c.get("voiced") or 0.0, c.get("speech_s") or 0.0
    if c["rank"] == AUTO:
        return (-(c.get("score") or 0.0), -voiced, -speech)
    return (-voiced, -speech)


def order_key(c: dict) -> tuple:
    return (c["rank"], *quality(c))


def select(cands: list[dict], cap: int = CAP) -> list[dict]:
    """The best `cap` of a batch: by rank, and within a rank new days and conversations first, then quality."""
    chosen: list[dict] = []
    days: set = set()
    convs: set = set()
    for rank in (HAND, ENROLL, AUTO):
        pool = sorted((c for c in cands if c["rank"] == rank), key=quality)
        while pool and len(chosen) < cap:
            def tier(c):
                conv = (c.get("device"), c.get("start_ms"))
                return 0 if c.get("day") not in days and conv not in convs else 1 if conv not in convs else 2
            best = min(range(len(pool)), key=lambda i: (tier(pool[i]), i))
            c = pool.pop(best)
            chosen.append(c)
            days.add(c.get("day"))
            convs.add((c.get("device"), c.get("start_ms")))
    return chosen


# ---------------------------------------------------------------------------
# changing the library

def _remove_entry(data: Path, name: str, e: dict) -> None:
    (person_dir(data, name) / f"{e['id']}.opus").unlink(missing_ok=True)


def drop(data: Path, src: str, prefix: bool = False) -> int:
    """Remove the clip(s) a source contributed, from every person's library."""
    n = 0
    if not root(data).is_dir():
        return 0
    with _lock(data):
        for name in people(data):
            entries = load(data, name)
            keep = []
            for e in entries:
                if e["src"] == src or (prefix and e["src"].startswith(src)):
                    _remove_entry(data, name, e)
                    n += 1
                else:
                    keep.append(e)
            if len(keep) != len(entries):
                _save(data, name, keep)
    return n


def add(data: Path, cfg: dict, name: str, *, kind: str, src: str, clip: Path, start_s: float, end_s: float,
        device: str | None = None, start_ms: int | None = None, score: float | None = None,
        voiced: float | None = None, speech_s: float | None = None, mixed: bool = False,
        day: str | None = None, origin: str | None = None) -> str:
    """Offer one clip (`origin`: where the audio was recorded, e.g. "phone" or "pendant"). Returns "added", "replaced" or "skipped: <why>". Never raises for a bad candidate."""
    who = eligible(cfg, name)
    if who is None:
        return "skipped: not an allowed name"
    if mixed:
        return "skipped: mixed voice"
    if voiced is not None and voiced < MIN_VOICED:
        return "skipped: not speech"
    clip = Path(clip)
    if not clip.is_file():
        return "skipped: no audio"
    start_s, end_s = float(start_s), float(end_s)
    if end_s - start_s > MAX_S:               # the middle of a long span
        mid = (start_s + end_s) / 2
        start_s, end_s = mid - MAX_S / 2, mid + MAX_S / 2
    if end_s - start_s < MIN_S:
        return "skipped: under 4 s"
    rank = RANKS[kind]
    cid = clip_id(src)
    entry = {"id": cid, "src": src, "kind": kind, "rank": rank, "device": device, "start_ms": start_ms,
             "day": day, "start_s": round(start_s, 2), "end_s": round(end_s, 2), "score": score, "voiced": voiced,
             "speech_s": speech_s, "added_at": time.time()}
    if origin:
        entry["origin"] = origin
    drop(data, src)                           # naming a voice again replaces what it gave before
    with _lock(data):
        entries = load(data, who)
        replaced = False
        if len(entries) >= CAP:
            worst = max(entries, key=order_key)
            if worst["rank"] <= rank:
                return "skipped: library full"
            _remove_entry(data, who, worst)
            entries.remove(worst)
            replaced = True
        person_dir(data, who).mkdir(parents=True, exist_ok=True)
        try:
            entry["dur_s"] = round(cut_clip(clip, start_s, end_s, person_dir(data, who) / f"{cid}.opus"), 2)
        except (OSError, ValueError, struct.error) as e:
            (person_dir(data, who) / f"{cid}.opus").unlink(missing_ok=True)
            _save(data, who, entries)
            return f"skipped: could not cut ({type(e).__name__})"
        entries.append(entry)
        _save(data, who, entries)
    return "replaced" if replaced else "added"


def entries_for_conversation(data: Path, device: str, start_ms: int) -> list[tuple[str, dict]]:
    """(person, entry) for clips cut from one conversation (what trash moves with it)."""
    return [(n, e) for n in people(data) for e in load(data, n)
            if e.get("device") == device and e.get("start_ms") == start_ms]


def put_entries(data: Path, items: list[tuple[str, dict]]) -> None:
    """Re-add index entries (their files were put back by trash restore). Idempotent by id."""
    with _lock(data):
        for name, e in items:
            entries = [x for x in load(data, name) if x["id"] != e["id"]]
            entries.append(e)
            _save(data, name, entries)


def remove_entries(data: Path, items: list[tuple[str, dict]]) -> None:
    """Take index entries out (their files are handled by the caller)."""
    with _lock(data):
        for name, e in items:
            _save(data, name, [x for x in load(data, name) if x["id"] != e["id"]])


# ---------------------------------------------------------------------------
# feeding it from the database

def _data_clip(data: Path, clip: str | None) -> Path:
    return Path(data) / "clips" / Path(clip or "").name


def add_cluster(con: sqlite3.Connection, data: Path, cfg: dict, cid: int, name: str, kind: str,
                score: float | None = None) -> str:
    row = con.execute("SELECT device, start_ms, clip, start_s, end_s, speech_s, voiced, COALESCE(mixed, 0), "
                      "COALESCE(noise, 0) FROM speaker_clusters WHERE id = ?", (cid,)).fetchone()
    if row is None:
        return "skipped: no such voice"
    device, start_ms, clip, a, b, speech, voiced, mixed, noise = row
    if noise:
        return "skipped: noise"
    day = con.execute("SELECT day FROM conversations WHERE device = ? AND start_ms = ?",
                      (device, start_ms)).fetchone()
    return add(data, cfg, name, kind=kind, src=f"cluster:{cid}", clip=_data_clip(data, clip), start_s=a or 0.0,
               end_s=b or 0.0, device=device, start_ms=start_ms, score=score, voiced=voiced, speech_s=speech,
               mixed=bool(mixed), day=day[0] if day else None)


def add_line(con: sqlite3.Connection, data: Path, cfg: dict, device: str, start_ms: int, index: int,
             offsets, who: str) -> str:
    """A line tapped to a person (hand rank); an ineligible `who` or no usable span only drops what it had."""
    src = f"para:{device}:{start_ms}:{index}"
    drop(data, src)
    if not offsets or index >= len(offsets) or not offsets[index]:
        return "skipped: no offsets"
    conv = con.execute("SELECT clip, day FROM conversations WHERE device = ? AND start_ms = ?",
                       (device, start_ms)).fetchone()
    if conv is None:
        return "skipped: no such conversation"
    return add(data, cfg, who, kind="hand", src=src, clip=_data_clip(data, conv[0]),
               start_s=float(offsets[index][0]), end_s=float(offsets[index][1]), device=device,
               start_ms=start_ms, day=conv[1])


def add_auto(con: sqlite3.Connection, data: Path, cfg: dict, device: str, start_ms: int, log=print) -> int:
    """Rank-3 candidates from a just-written conversation: the clusters a voiceprint matched, by score margin."""
    import numpy as np

    from . import speakers, voices
    rows = con.execute("SELECT id, label, embedding FROM speaker_clusters WHERE device = ? AND start_ms = ? "
                       "AND labeled_by = 'match' AND label IS NOT NULL AND COALESCE(mixed, 0) = 0",
                       (device, start_ms)).fetchall()
    rows = [r for r in rows if r[2] is not None and eligible(cfg, r[1])]
    if not rows:
        return 0
    sp = cfg.get("speakers", {})
    vps = speakers.load_voiceprints(speakers.voiceprint_dir(data))
    embs = speakers.normalize(np.stack([np.frombuffer(r[2], dtype=np.float32) for r in rows]))
    scores = voices.score(embs, vps, voices.load_strangers(con), exclude=[(device, start_ms)] * len(rows),
                          self_ids=[r[0] for r in rows], threshold=float(sp.get("match_threshold", 0.5)))
    n = 0
    for r, sc in zip(rows, scores):
        if sc.matched and sc.best_name and sc.best_name.lower() == str(r[1]).lower():
            n += add_cluster(con, data, cfg, r[0], r[1], "auto", score=float(sc.margin)) in ("added", "replaced")
    return n


def backfill(con: sqlite3.Connection, data: Path, cfg: dict, dry_run: bool = False) -> dict:
    """Build the library from the clips that exist today: hand answers and line taps, then auto matches by margin
    (read-aloud readings keep no clip, so they only join as they are recorded). Returns {name: clips}."""
    import numpy as np

    from . import speakers, voices
    by_person: dict[str, list[dict]] = {}

    def offer(name, kind, src, **kw):
        who = eligible(cfg, name)
        if who is None:
            return
        by_person.setdefault(who, []).append({"kind": kind, "rank": RANKS[kind], "src": src, "name": who, **kw})

    cl = con.execute("SELECT id, device, start_ms, clip, start_s, end_s, speech_s, voiced, label, labeled_by, "
                     "embedding FROM speaker_clusters WHERE COALESCE(mixed, 0) = 0 AND COALESCE(noise, 0) = 0 "
                     "AND label IS NOT NULL AND (labeled_by IN ('user', 'match'))").fetchall()
    days = dict(((d, s), day) for d, s, day in con.execute("SELECT device, start_ms, day FROM conversations"))
    match = [r for r in cl if r[9] == "match" and r[10] is not None]
    margins: dict[int, float] = {}
    if match:
        vps = speakers.load_voiceprints(speakers.voiceprint_dir(data))
        sp = cfg.get("speakers", {})
        embs = speakers.normalize(np.stack([np.frombuffer(r[10], dtype=np.float32) for r in match]))
        scs = voices.score(embs, vps, voices.load_strangers(con), exclude=[(r[1], r[2]) for r in match],
                           self_ids=[r[0] for r in match], threshold=float(sp.get("match_threshold", 0.5)))
        for r, sc in zip(match, scs):
            if sc.matched and sc.best_name and sc.best_name.lower() == str(r[8]).lower():
                margins[r[0]] = float(sc.margin)
    for cid, device, start_ms, clip, a, b, speech, voiced, label, by, _e in cl:
        if by == "match" and cid not in margins:
            continue
        offer(label, "hand" if by == "user" else "auto", f"cluster:{cid}", cid=cid, device=device, start_ms=start_ms,
              day=days.get((device, start_ms)), voiced=voiced, speech_s=speech,
              score=margins.get(cid), clip=clip, start_s=a, end_s=b)
    try:
        samples = con.execute("SELECT id, holder FROM paragraph_samples").fetchall()
    except sqlite3.OperationalError:
        samples = []
    for sid, holder in samples:
        m = re.fullmatch(r"para:(.+):(\d+):(\d+)", sid or "")
        if not m or holder in (None, voices.STRANGER_HOLDER):
            continue
        device, start_ms, idx = m[1], int(m[2]), int(m[3])
        conv = con.execute("SELECT clip, day, paragraph_offsets FROM conversations WHERE device = ? AND start_ms = ?",
                           (device, start_ms)).fetchone()
        try:
            off = json.loads(conv[2]) if conv and conv[2] else None
            span = off[idx]
        except (ValueError, IndexError, TypeError):
            continue
        if conv and span:
            offer(holder, "hand", sid, device=device, start_ms=start_ms, day=conv[1], clip=conv[0],
                  start_s=float(span[0]), end_s=float(span[1]), voiced=None, score=None, speech_s=float(span[1]) - float(span[0]))
    out = {}
    for name, cands in sorted(by_person.items()):
        usable = []
        for c in cands:      # only what can really be cut and is long enough
            f = _data_clip(data, c["clip"])
            a, b = float(c["start_s"] or 0), float(c["end_s"] or 0)
            if f.is_file() and b - a >= MIN_S and (c["voiced"] is None or c["voiced"] >= MIN_VOICED):
                usable.append(c)
        counts = {"hand": 0, "enroll": 0, "auto": 0}
        for c in select(usable):
            counts[c["kind"]] += 1
            if not dry_run:
                add(data, cfg, name, kind=c["kind"], src=c["src"], clip=_data_clip(data, c["clip"]),
                    start_s=c["start_s"], end_s=c["end_s"], device=c["device"], start_ms=c["start_ms"],
                    score=c["score"], voiced=c["voiced"], speech_s=c["speech_s"], day=c["day"])
        out[name] = {"candidates": len(usable), **counts, "clips": sum(counts.values())}
    return out


def summary(data: Path) -> dict:
    """{name: {clips, bytes}} of what is in the library now."""
    out = {}
    for name in people(data):
        files = [p for p in person_dir(data, name).glob("*.opus")]
        out[name] = {"clips": len(files), "bytes": sum(p.stat().st_size for p in files)}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pendant_direct.voice_library")
    ap.add_argument("--data", type=Path, default=Path.home() / "pendant-direct-data")
    ap.add_argument("--backfill", action="store_true", help="build the library from today's clips")
    ap.add_argument("--dry-run", action="store_true", help="with --backfill: report only")
    a = ap.parse_args(argv)
    cfg = cfgmod.load(a.data)
    if a.backfill:
        db = a.data / "pages.db"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        try:
            res = backfill(con, a.data, cfg, dry_run=a.dry_run)
        finally:
            con.close()
        print(("would add" if a.dry_run else "added") + " per person:")
        for n, r in res.items():
            print(f"  {n}: {r['clips']} clips (hand {r['hand']}, enrollment {r['enroll']}, auto {r['auto']}) "
                  f"of {r['candidates']} candidates")
        if not res:
            print("  nobody")
    for n, v in summary(a.data).items():
        print(f"  {n}: {v['clips']} clips in the library, {v['bytes'] / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
