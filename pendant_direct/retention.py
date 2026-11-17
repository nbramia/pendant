"""Audio retention: keep audio for `retention.days` days, starred recordings forever.

Only audio bytes go: the Opus in `chunks.opus` (set to NULL) and the per-conversation clip
files in `clips/`. Every `chunks` row stays, with `opus_sha`, `done` and `ts_ms`, so ingest
still recognises a page that arrives again (INSERT OR IGNORE on the primary key) and never
re-transcribes it. Transcripts, summaries, voiceprints, speaker embeddings, the vault, the
trash and its manifests are never touched. A starred recording that is purged after the
fact cannot be brought back.

Raw spool segments (`spool/*.pmsg`, every page ever uploaded) go too once they are ingested and
all their audio is older than the cutoff; one that overlaps a starred recording, an enrollment
window or untranscribed audio stays. Dedupe needs only the `chunks` rows and tombstones, both in
the database.

The whole voices the wearer named by hand (`voice_audio/`, voice_audio.py) and the voice library are
never touched; a pass first brings the kept voices up to date, so a hand-named voice's speech is cut from
its clip before the clip goes.

Exempt: chunks that are not done, chunks inside a waiting or added enrollment window,
and (with `retention.keep_starred`, the default) every starred recording: a button press, the
app star (`notable_overrides.json`) or `notable = 1`.

Dry run (changes nothing, opens the database read-only):

    python -m pendant_direct.retention --dry-run [--data DIR] [--now YYYY-MM-DD[THH:MM]]

The processor runs the pass at most once a day, under its lock, when `retention.enabled`.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config as cfgmod
from . import highlights as hl

TZ = ZoneInfo("America/New_York")
DAY_MS = 86_400_000
PAD_MS = 1_000                 # a conversation's span, padded, covers its chunks
STAR_GRACE_MS = 300_000        # an unplaced button press protects the audio around it
LAST_KEY = "retention_last"    # meta: local day of the last pass
VACUUM_KEY = "retention_vacuum"
VACUUM_EVERY_S = 7 * 86_400
CLIP_RE = re.compile(r"^[A-Za-z0-9]+_(\d{8})_(\d{6})\.opus$")
BATCH = 1000


def settings(cfg: dict) -> dict:
    r = cfg.get("retention") if isinstance(cfg.get("retention"), dict) else {}
    try:
        days = float(r.get("days", 7))
    except (TypeError, ValueError):
        days = 7.0
    return {"enabled": r.get("enabled") is True, "days": max(days, 1.0),
            "keep_starred": r.get("keep_starred") is not False}


def local_day(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d")


def parse_now(text: str | None) -> int:
    if not text:
        return int(time.time() * 1000)
    d = dt.datetime.fromisoformat(text)
    if d.tzinfo is None:
        d = d.replace(tzinfo=TZ)
    return int(d.timestamp() * 1000)


def _has(con: sqlite3.Connection, table: str) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone())


def _merge(spans: list[tuple[int, int]]) -> tuple[list[int], list[int]]:
    spans = sorted(spans)
    los: list[int] = []
    his: list[int] = []
    for lo, hi in spans:
        if his and lo <= his[-1]:
            his[-1] = max(his[-1], hi)
        else:
            los.append(lo)
            his.append(hi)
    return los, his


def _inside(index: tuple[list[int], list[int]] | None, ts: int) -> bool:
    if not index:
        return False
    los, his = index
    i = bisect.bisect_right(los, ts) - 1
    return i >= 0 and ts <= his[i]


def _overlaps(index: tuple[list[int], list[int]] | None, lo: int, hi: int) -> bool:
    if not index:
        return False
    los, his = index
    i = bisect.bisect_right(los, hi) - 1
    return i >= 0 and his[i] >= lo


def clip_start_ms(name: str) -> int | None:
    """The start time encoded in `<device>_<YYYYMMDD>_<HHMMSS>.opus` (local time)."""
    m = CLIP_RE.match(name)
    if not m:
        return None
    try:
        d = dt.datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S").replace(tzinfo=TZ)
    except ValueError:
        return None
    return int(d.timestamp() * 1000)


def _plan_spool(con, data: Path, cutoff: int, index: dict, bucket, local_day_fn) -> tuple[list[Path], dict]:
    """Spool segments (`spool/*.pmsg`, the raw uploaded pages) that can go: ingested, every chunk and
    button press older than `cutoff`, none inside a protected (starred) span or an enrollment window,
    none with chunks still waiting for transcription. A segment that can't be decoded stays. Returns
    (files, {segments, bytes} of old segments kept because they overlap a starred recording or window).
    The spool is read here only; `chunks` rows (dedupe) and tombstones live in the database."""
    from . import process      # lazily: process imports this module
    spool = data / "spool"
    kept = {"segments": 0, "bytes": 0}
    if not spool.is_dir():
        return [], kept
    # backfill_stars reads ingested segments once per database; until it has run they are still needed
    if not con.execute("SELECT 1 FROM meta WHERE key='stars_backfilled'").fetchone():
        return [], kept
    seen = {r[0] for r in con.execute("SELECT name FROM segments")}
    windows: dict[str, list[tuple[int, int]]] = {}
    if _has(con, "enrollments"):
        for device, lo, hi in con.execute("SELECT device, lo_ms, hi_ms FROM enrollments "
                                          "WHERE status IN ('waiting', 'added')"):
            windows.setdefault(device, []).append((lo, hi))
    win_index = {d: _merge(w) for d, w in windows.items()}
    out: list[Path] = []
    for seg in sorted(spool.glob("*.pmsg")):
        if seg.name not in seen or process.segment_time_ms(seg.name) >= cutoff:
            continue
        device = seg.name.split("_")[0]
        fallback = process.segment_time_ms(seg.name)
        times: list[int] = []
        try:
            for ch in process.iter_audio_chunks_from_messages(process.records(seg), keys=process.load_keyset(device)):
                valid = ch.absolute_timestamp_ms > process.VALID_EPOCH_MS
                times.append(ch.absolute_timestamp_ms if valid else fallback)
                press = ch.chunk.button
                if press is not None and press.button_event_absolute_timestamp_ms > process.VALID_EPOCH_MS:
                    times.append(press.button_event_absolute_timestamp_ms)
        except Exception:      # unreadable: never delete what can't be understood
            continue
        times = times or [fallback]
        size = seg.stat().st_size
        if max(times) >= cutoff:
            continue
        if any(_inside(index.get(device), t) or _inside(win_index.get(device), t) for t in times):
            kept["segments"] += 1
            kept["bytes"] += size
            continue
        if con.execute("SELECT 1 FROM chunks WHERE device=? AND done=0 AND ts_ms BETWEEN ? AND ? LIMIT 1",
                       (device, min(times), max(times))).fetchone():
            continue
        out.append(seg)
        b = bucket(local_day_fn(max(times)))
        b["spool"] += 1
        b["spool_bytes"] += size
    return out, kept


def _library_summary(data: Path) -> dict:
    """{person: {clips, bytes}}: the permanent voice library, which a pass never touches."""
    from . import voice_library
    return voice_library.summary(data)


def _kept_voices(data: Path) -> dict:
    """{voices, conversations, people, bytes, speech_s}: the kept whole voices, which a pass never touches."""
    from . import voice_audio
    return voice_audio.summary(data)


def plan(con: sqlite3.Connection, data: Path, *, now_ms: int, days: float = 7, keep_starred: bool = True) -> dict:
    """What a pass would remove and keep. Reads only."""
    data = Path(data)
    cutoff = int(now_ms - days * DAY_MS)
    overrides = hl.load_overrides(data / "notable_overrides.json")
    clips_dir = data / "clips"

    convs = con.execute("SELECT device, start_ms, end_ms, day, clip, notable, title FROM conversations").fetchall() \
        if _has(con, "conversations") else []
    stars = con.execute("SELECT device, ts_ms, conv_device, conv_start_ms FROM stars").fetchall() \
        if _has(con, "stars") else []
    placed = {(r[2], r[3]) for r in stars if r[2] is not None and r[3] is not None}

    def why(device, start_ms, notable):
        reasons = []
        if (device, start_ms) in placed:
            reasons.append("button star")
        if overrides.get(hl.event_id(device, start_ms)) is True:
            reasons.append("app star")
        if notable:
            reasons.append("notable")
        return reasons

    protected: dict[str, list[tuple[int, int]]] = {}
    keep_rows = []
    for device, start, end, day, clip, notable, title in convs:
        reasons = why(device, start, notable)
        if reasons and keep_starred:
            protected.setdefault(device, []).append((start - PAD_MS, end + PAD_MS))
            if end < cutoff:
                keep_rows.append({"day": day, "device": device, "start_ms": start, "title": title,
                                  "why": reasons, "clip": Path(clip or "").name})
    if keep_starred:
        for device, ts, cd, cs in stars:
            if cd is None or cs is None:     # not placed in a conversation yet
                protected.setdefault(device, []).append((ts - STAR_GRACE_MS, ts + STAR_GRACE_MS))
    index = {d: _merge(s) for d, s in protected.items()}

    per_day: dict[str, dict] = {}

    def bucket(day):
        return per_day.setdefault(day, {"clips": 0, "clip_bytes": 0, "chunks": 0, "chunk_bytes": 0,
                                     "cache": 0, "cache_bytes": 0, "spool": 0, "spool_bytes": 0})

    # chunks: old, transcribed, outside enrollment windows and starred spans
    window = ""
    if _has(con, "enrollments"):
        window = ("AND NOT EXISTS (SELECT 1 FROM enrollments e WHERE e.device = chunks.device "
                  "AND e.status IN ('waiting', 'added') AND chunks.ts_ms BETWEEN e.lo_ms AND e.hi_ms)")
    rowids: list[int] = []
    for rid, device, ts, size in con.execute(
            "SELECT rowid, device, ts_ms, LENGTH(opus) FROM chunks "
            f"WHERE done = 1 AND opus IS NOT NULL AND ts_ms < ? {window}", (cutoff,)):
        if _inside(index.get(device), ts):
            continue
        rowids.append(rid)
        b = bucket(local_day(ts))
        b["chunks"] += 1
        b["chunk_bytes"] += size or 0
    # clips: old conversations' clips, and old orphan clips
    referenced = {Path(c[4] or "").name for c in convs}
    if _has(con, "speaker_clusters"):
        referenced |= {Path(r[0] or "").name for r in con.execute("SELECT DISTINCT clip FROM speaker_clusters")}
    clip_files: list[Path] = []
    cache_dir = data / "audio_cache"      # playback transcodes (<clip stem>.m4a), made on first play

    def add_cache(stem: str, day: str) -> None:
        c = cache_dir / f"{stem}.m4a"
        if c.is_file() and c not in clip_files:
            clip_files.append(c)
            b = bucket(day)
            b["cache"] += 1
            b["cache_bytes"] += c.stat().st_size

    wins: dict[str, list[tuple[int, int]]] = {}
    if _has(con, "enrollments"):
        for device, lo, hi in con.execute("SELECT device, lo_ms, hi_ms FROM enrollments "
                                          "WHERE status IN ('waiting', 'added')"):
            wins.setdefault(device, []).append((lo, hi))
    win_index = {d: _merge(w) for d, w in wins.items()}

    for device, start, end, day, clip, notable, title in convs:
        if not clip or end >= cutoff:
            continue
        if keep_starred and why(device, start, notable):
            continue
        # the same exemptions as chunks and spool: an unplaced press's grace window, a pending enrollment window
        if _overlaps(index.get(device), start, end) or _overlaps(win_index.get(device), start, end):
            continue
        f = clips_dir / Path(clip).name
        if f.is_file() and f not in clip_files:
            clip_files.append(f)
            b = bucket(day)
            b["clips"] += 1
            b["clip_bytes"] += f.stat().st_size
        add_cache(f.stem, day)
    if clips_dir.is_dir():
        for f in sorted(clips_dir.glob("*.opus")):
            if f.name in referenced:
                continue
            start = clip_start_ms(f.name)
            if start is None or start >= cutoff:
                continue
            device = f.name.split("_")[0]
            if _inside(index.get(device), start) or _inside(win_index.get(device), start):
                continue
            clip_files.append(f)
            b = bucket(local_day(start))
            b["clips"] += 1
            b["clip_bytes"] += f.stat().st_size
            add_cache(f.stem, local_day(start))
    # orphaned cache files: the same rule, by the start time in the stem
    if cache_dir.is_dir():
        for c in sorted(cache_dir.glob("*.m4a")):
            if c in clip_files or f"{c.stem}.opus" in referenced:
                continue
            start = clip_start_ms(f"{c.stem}.opus")
            if start is None or start >= cutoff:
                continue
            dev = c.stem.split("_")[0]
            if _inside(index.get(dev), start) or _inside(win_index.get(dev), start):
                continue
            add_cache(c.stem, local_day(start))
    spool_files, spool_kept = _plan_spool(con, data, cutoff, index, bucket, local_day) \
        if _has(con, "segments") else ([], {"segments": 0, "bytes": 0})
    clip_files.extend(spool_files)
    # starred recordings kept: their remaining audio
    for r in keep_rows:
        conv = next(c for c in convs if c[0] == r["device"] and c[1] == r["start_ms"])
        n, size = con.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(opus)), 0) FROM chunks WHERE device=? "
                              "AND ts_ms BETWEEN ? AND ? AND opus IS NOT NULL",
                              (r["device"], conv[1] - PAD_MS, conv[2] + PAD_MS)).fetchone()
        f = clips_dir / r["clip"] if r["clip"] else None
        r["chunks"], r["chunk_bytes"] = n, size
        r["clip_bytes"] = f.stat().st_size if f and f.is_file() else 0
    days_out = {d: per_day[d] for d in sorted(per_day)}
    total = {k: sum(v[k] for v in per_day.values()) for k in ("clips", "clip_bytes", "chunks", "chunk_bytes", "cache", "cache_bytes",
                                                  "spool", "spool_bytes")}
    return {"cutoff_ms": cutoff, "days": days_out, "total": total, "starred_kept": keep_rows, "spool_kept": spool_kept,
            "voice_library": _library_summary(data), "voice_audio": _kept_voices(data),
            "_rowids": rowids, "_clips": clip_files}


def run(con: sqlite3.Connection, data: Path, *, now_ms: int, days: float = 7, keep_starred: bool = True) -> dict:
    """The pass. Rows keep everything but `opus`; commits in batches. Returns the plan's report. Hand-named
    voices are kept first (voice_audio.sync_all); a failure there is logged and the pass goes on."""
    try:
        from . import voice_audio
        voice_audio.sync_all(con, data, cfgmod.notes_dir(data), cfgmod.load(data))
    except Exception as e:  # noqa: BLE001
        print(f"retention: voice audio failed ({type(e).__name__}: {str(e)[:120]})", file=sys.stderr)
    p = plan(con, data, now_ms=now_ms, days=days, keep_starred=keep_starred)
    ids = p["_rowids"]
    for i in range(0, len(ids), BATCH):
        part = ids[i:i + BATCH]
        # done = 1 again in the statement: a chunk can't have been handed out for transcription since
        con.execute(f"UPDATE chunks SET opus = NULL WHERE done = 1 AND rowid IN ({','.join('?' * len(part))})", part)
        con.commit()
    for f in p["_clips"]:
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    return report(p)


def report(p: dict) -> dict:
    return {k: v for k, v in p.items() if not k.startswith("_")}


def maybe_run(con: sqlite3.Connection, data: Path, cfg: dict, *, now_ms: int | None = None, log=print) -> dict | None:
    """The processor's daily hook (it holds the lock). No-op unless enabled and not yet run today."""
    s = settings(cfg)
    if not s["enabled"]:
        return None
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    today = local_day(now_ms)
    if _has(con, "meta"):
        last = con.execute("SELECT value FROM meta WHERE key=?", (LAST_KEY,)).fetchone()
        if last and last[0] == today:
            return None
    rep = run(con, data, now_ms=now_ms, days=s["days"], keep_starred=s["keep_starred"])
    con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (LAST_KEY, today))
    con.commit()
    t = rep["total"]
    log(f"retention: removed {t['chunks']} chunks ({t['chunk_bytes']} bytes) and {t['clips']} clips "
        f"({t['clip_bytes']} bytes), {t['cache']} playback files and {t['spool']} spool segments ({t['spool_bytes']} bytes); kept {len(rep['starred_kept'])} old starred recordings")
    last_v = con.execute("SELECT value FROM meta WHERE key=?", (VACUUM_KEY,)).fetchone()
    if t["chunks"] and (not last_v or time.time() - float(last_v[0]) >= VACUUM_EVERY_S):
        con.execute("VACUUM")
        con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (VACUUM_KEY, str(time.time())))
        con.commit()
        log("retention: vacuumed pages.db")
    return rep


def _mb(n: int) -> str:
    return f"{n / 1e6:.2f} MB"


def format_report(rep: dict, *, dry: bool) -> str:
    cut = dt.datetime.fromtimestamp(rep["cutoff_ms"] / 1000, TZ).strftime("%Y-%m-%d %H:%M %Z")
    lines = [f"retention {'dry run: would remove' if dry else 'removed'} audio before {cut}"]
    lines.append(f"{'day':<12}{'clips':>7}{'clip MB':>10}{'chunks':>10}{'chunk MB':>10}{'cache':>7}{'cache MB':>10}{'spool':>7}{'spool MB':>10}")
    for d, v in rep["days"].items():
        lines.append(f"{d:<12}{v['clips']:>7}{v['clip_bytes'] / 1e6:>10.2f}{v['chunks']:>10}{v['chunk_bytes'] / 1e6:>10.2f}{v['cache']:>7}{v['cache_bytes'] / 1e6:>10.2f}{v['spool']:>7}{v['spool_bytes'] / 1e6:>10.2f}")
    t = rep["total"]
    lines.append(f"{'total':<12}{t['clips']:>7}{t['clip_bytes'] / 1e6:>10.2f}{t['chunks']:>10}{t['chunk_bytes'] / 1e6:>10.2f}{t['cache']:>7}{t['cache_bytes'] / 1e6:>10.2f}{t['spool']:>7}{t['spool_bytes'] / 1e6:>10.2f}")
    sk = rep["spool_kept"]
    lines.append(f"old spool segments kept (starred span or enrollment window): {sk['segments']} ({_mb(sk['bytes'])})")
    lib = rep.get("voice_library") or {}
    lines.append("voice library (never purged): " + (", ".join(f"{n} {v['clips']} clips {_mb(v['bytes'])}"
                                                               for n, v in lib.items()) or "empty"))
    va = rep.get("voice_audio") or {}
    lines.append(f"voice audio of hand-named voices (never purged): {va.get('voices', 0)} voices, "
                 f"{_mb(va.get('bytes', 0))}")
    kept = rep["starred_kept"]
    lines.append(f"starred recordings kept: {len(kept)}")
    for r in kept:
        when = dt.datetime.fromtimestamp(r["start_ms"] / 1000, TZ).strftime("%H:%M")
        lines.append(f"  {r['day']} {when} {r['device'][:6]} {r['title'] or ''} [{', '.join(r['why'])}] "
                     f"({_mb(r['clip_bytes'])} clip, {_mb(r['chunk_bytes'])} chunks)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pendant_direct.retention")
    ap.add_argument("--data", type=Path, default=Path.home() / "pendant-direct-data")
    ap.add_argument("--dry-run", action="store_true", help="report only; changes nothing")
    ap.add_argument("--now", help="pretend it is this local date/time (YYYY-MM-DD[THH:MM])")
    ap.add_argument("--days", type=float, help="override retention.days")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    cfg = cfgmod.load(a.data)
    s = settings(cfg)
    days = a.days if a.days is not None else s["days"]
    now_ms = parse_now(a.now)
    db = a.data / "pages.db"
    if a.dry_run:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        try:
            rep = report(plan(con, a.data, now_ms=now_ms, days=days, keep_starred=s["keep_starred"]))
        finally:
            con.close()
    else:
        if not s["enabled"]:
            print("retention.enabled is false; only --dry-run is allowed", file=sys.stderr)
            return 2
        import fcntl
        with open(a.data / ".process.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("the processor is running; try again", file=sys.stderr)
                return 1
            con = sqlite3.connect(db, timeout=30)
            try:
                rep = run(con, a.data, now_ms=now_ms, days=days, keep_starred=s["keep_starred"])
            finally:
                con.close()
    print(json.dumps(rep, indent=1) if a.json else format_report(rep, dry=a.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
