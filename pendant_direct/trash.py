"""Deleting a recording, the 7-day trash, and tombstones.

Delete (`delete`, the feed's `POST .../delete`) takes one written conversation
out of everything that holds it and moves it to
`~/pendant-direct-data/trash/<DEVICE>-<start_ms>/`:

- `manifest.json`: the Raw section text (and the headings around it, so a
  restore puts it back in place), the Event note and clip file names, the
  database rows (`conversations`, `speaker_clusters`, `speaker_turns`, `stars`,
  `paragraph_samples`, `task_requests`, `task_scans`, and the `strangers`,
  `noise_exemplars` and `voice_offers` rows of its voices) and the voiceprint
  samples taken out of `voiceprints/` (its clusters' and its lines');
- `chunks.db`: its audio chunk rows (`chunks`, done marks kept);
- `event/<name>`, `clip/<name>`: the Event note and the clip.

The day's Highlights are rebuilt without it. A delete is all-or-nothing: the
copy is written first, then the rows are deleted in one transaction, the
voiceprint samples taken out and the section cut from the Raw note, and only
then is the transaction committed. Any failure before the commit puts the
note and voiceprints back and rolls the transaction back. A crash in between
is finished or undone by `recover` (the feed before each delete or restore,
the processor before each run): the `trash` row, committed with the delete, is
the deciding mark. Restore (`restore`) runs the same way in reverse, and
restoring something that is not in the trash changes nothing.

Tombstones keep a deleted recording from coming back when its pages arrive
again (a pendant drain, a USB backup): ingest drops audio chunks matching a
deleted chunk (`tombstones`: device, page and Opus hash, with a page timestamp
within 6 h, or an upload within 14 days when the page clock was invalid) or
timed inside a deleted recording's span (`range_tombstones`),
and button presses on or inside it (`star_tombstones`). They stay after the
trash entry is purged (`purge`, by the processor, 7 days after deletion).

Every vault and database change here holds the processor's lock
(`.process.lock`).
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

TRASH_DAYS = 7
LOCK_WAIT_S = 30                      # how long the feed waits for a processor run to finish
TOMB_WINDOW_MS = 6 * 3600 * 1000      # a re-sent packet carries a page timestamp this close to the deleted one
# A packet whose page clock was invalid is timed by its upload instead: it matches a
# tombstone only if uploaded within this long of the deleted recording (pendants are
# drained within days; a later identical packet after a wipe is a different recording).
TOMB_FALLBACK_WINDOW_MS = 14 * 86400 * 1000

SQL = """
CREATE TABLE IF NOT EXISTS trash (
    id TEXT PRIMARY KEY, device TEXT, start_ms INT, end_ms INT, day TEXT, title TEXT,
    raw_heading TEXT, pendant TEXT, audio_s REAL, deleted_at REAL);
CREATE TABLE IF NOT EXISTS tombstones (
    device TEXT, page INT, ord INT, opus_sha TEXT, ts_ms INT, trash_id TEXT,
    PRIMARY KEY (device, page, ord, opus_sha));
CREATE INDEX IF NOT EXISTS tombstones_packet ON tombstones (device, page, opus_sha);
CREATE TABLE IF NOT EXISTS range_tombstones (
    trash_id TEXT PRIMARY KEY, device TEXT, start_ms INT, end_ms INT);
CREATE TABLE IF NOT EXISTS star_tombstones (
    device TEXT, ts_ms INT, trash_id TEXT, PRIMARY KEY (device, ts_ms));
"""


class TrashError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code, self.msg = code, msg


def _checkpoint(step: str) -> None:
    """A no-op between the steps of a delete or restore (tests make it fail)."""


def trash_id(device: str, start_ms: int) -> str:
    return f"{device}-{int(start_ms)}"


def trash_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "trash"


# ---------------------------------------------------------------------------
# tombstones (ingest side; the processor holds its lock)

def drop_tombstoned(con: sqlite3.Connection, device: str, rows: list,
                    clock_ok: list[bool] | None = None) -> tuple[list, int]:
    """rows: (device, page, ord, opus_sha, ts_ms, opus) about to be inserted into
    chunks; clock_ok[i] False when row i's ts_ms is the segment's upload time (the
    page clock was invalid). A row is deleted audio when it is timed inside a
    deleted recording's span, or is the same packet (page and Opus hash) as a
    deleted one with a page timestamp within TOMB_WINDOW_MS of it
    (TOMB_FALLBACK_WINDOW_MS for an upload-timed row). Identical packets at
    other times (silence after a wipe reuses page numbers) are kept. Returns
    (rows to keep, how many were dropped)."""
    try:
        ranges = con.execute("SELECT start_ms, end_ms FROM range_tombstones WHERE device=?", (device,)).fetchall()
        packets = con.execute("SELECT 1 FROM tombstones WHERE device=? LIMIT 1", (device,)).fetchone()
    except sqlite3.OperationalError:
        return rows, 0
    if not ranges and not packets:
        return rows, 0
    kept = []
    for i, r in enumerate(rows):
        timed = clock_ok is None or clock_ok[i]
        if timed and any(a <= r[4] <= b for a, b in ranges):
            continue
        if packets and con.execute(
                "SELECT 1 FROM tombstones WHERE device=? AND page=? AND opus_sha=? AND ABS(ts_ms - ?) <= ? LIMIT 1",
                (device, r[1], r[3], r[4], TOMB_WINDOW_MS if timed else TOMB_FALLBACK_WINDOW_MS)).fetchone():
            continue
        kept.append(r)
    return kept, len(rows) - len(kept)


def star_tombstoned(con: sqlite3.Connection, device: str, ts_ms: int, merge_ms: int) -> bool:
    """True for a button press from a deleted recording."""
    try:
        return bool(con.execute("SELECT 1 FROM star_tombstones WHERE device=? AND ts_ms > ? AND ts_ms < ?",
                                (device, ts_ms - merge_ms, ts_ms + merge_ms)).fetchone()
                    or con.execute("SELECT 1 FROM range_tombstones WHERE device=? AND start_ms <= ? AND end_ms >= ?",
                                   (device, ts_ms, ts_ms)).fetchone())
    except sqlite3.OperationalError:
        return False


# ---------------------------------------------------------------------------
# the Raw section

def _heading_offsets(text: str) -> list[tuple[int, str]]:
    """(char offset of the line, heading text) for every `## ` line."""
    out, pos = [], 0
    for line in text.split("\n"):
        if line.startswith("## "):
            out.append((pos, line[3:]))
        pos += len(line) + 1
    return out


def cut_section(text: str, heading: str) -> tuple[str, dict] | None:
    """(text without `## heading`'s section, {"text", "next", "prev"}), or None
    when the section isn't there. The section runs from its heading line to the
    next `## ` line (or the end), so cutting and inserting at the same place
    gives the text back byte for byte."""
    heads = _heading_offsets(text)
    hits = [k for k, (_o, h) in enumerate(heads) if h == heading]
    if not hits:
        return None
    if len(hits) > 1:
        raise TrashError(409, "two sections share this heading")
    k = hits[0]
    start = heads[k][0]
    end = heads[k + 1][0] if k + 1 < len(heads) else len(text)
    return text[:start] + text[end:], {"text": text[start:end],
                                       "next": heads[k + 1][1] if k + 1 < len(heads) else None,
                                       "prev": heads[k - 1][1] if k > 0 else None}


def insert_section(text: str, section: dict, later: set[str] = frozenset()) -> str:
    """Put a cut section back: before the heading that followed it, else after
    the one before it, else before the first section in `later` (headings of
    conversations that started after it), else at the end."""
    if any(h == section["text"].split("\n", 1)[0][3:] for _o, h in _heading_offsets(text)):
        return text  # already there
    heads = _heading_offsets(text)
    at = None
    for k, (off, h) in enumerate(heads):
        if section.get("next") is not None and h == section["next"]:
            at = off
            break
    if at is None and section.get("prev") is not None:
        for k, (off, h) in enumerate(heads):
            if h == section["prev"]:
                at = heads[k + 1][0] if k + 1 < len(heads) else len(text)
                break
    if at is None:
        at = next((off for off, h in heads if h in later), len(text))
    if at == len(text) and text and not text.endswith("\n"):
        text += "\n"
        at += 1
    return text[:at] + section["text"] + text[at:]


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".trash.tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# rows

def _enc(v):
    return {"b64": base64.b64encode(v).decode()} if isinstance(v, (bytes, bytearray, memoryview)) else v


def _dec(v):
    return base64.b64decode(v["b64"]) if isinstance(v, dict) and "b64" in v else v


def _rows(con: sqlite3.Connection, table: str, where: str, args) -> list[dict]:
    cur = con.execute(f"SELECT * FROM {table} WHERE {where}", args)
    cols = [d[0] for d in cur.description]
    return [{c: _enc(v) for c, v in zip(cols, r)} for r in cur.fetchall()]


def _in(ids) -> tuple[str, list]:
    ids = list(ids)
    return f"({', '.join('?' * len(ids))})" if ids else "(NULL)", ids


def _sample_keys(con: sqlite3.Connection, device: str, start_ms: int) -> list[int]:
    return [r[0] for r in con.execute("SELECT key FROM paragraph_samples WHERE id LIKE ?",
                                      (f"para:{device}:{start_ms}:%",))]


def snapshot_rows(con: sqlite3.Connection, device: str, start_ms: int) -> dict[str, list[dict]]:
    """Every row that belongs to this conversation, by table."""
    from . import process
    cids = [r[0] for r in con.execute("SELECT id FROM speaker_clusters WHERE device=? AND start_ms=?",
                                      (device, start_ms))]
    keys = _sample_keys(con, device, start_ms)
    q_c, a_c = _in(cids)
    q_v, a_v = _in(cids + keys)
    # pending presses that would land in this conversation go with it
    pending = [ts for d, ts in con.execute("SELECT device, ts_ms FROM stars WHERE conv_start_ms IS NULL AND device=?",
                                           (device,))
               if (c := process.star_conversation(con, d, ts)) and c[0] == device and c[1] == start_ms]
    q_p, a_p = _in(pending)
    return {
        "conversations": _rows(con, "conversations", "device=? AND start_ms=?", (device, start_ms)),
        "speaker_clusters": _rows(con, "speaker_clusters", "device=? AND start_ms=?", (device, start_ms)),
        "speaker_turns": _rows(con, "speaker_turns", "device=? AND start_ms=?", (device, start_ms)),
        "stars": _rows(con, "stars", f"(conv_device=? AND conv_start_ms=?) OR (device=? AND conv_start_ms IS NULL "
                                     f"AND ts_ms IN {q_p})", [device, start_ms, device, *a_p]),
        "paragraph_samples": _rows(con, "paragraph_samples", "id LIKE ?", (f"para:{device}:{start_ms}:%",)),
        "task_requests": _rows(con, "task_requests", "device=? AND start_ms=?", (device, start_ms)),
        "task_scans": _rows(con, "task_scans", "device=? AND start_ms=?", (device, start_ms)),
        "strangers": _rows(con, "strangers", f"cluster_id IN {q_v}", a_v),
        "noise_exemplars": _rows(con, "noise_exemplars", f"cluster_id IN {q_c}", a_c),
        "voice_offers": _rows(con, "voice_offers", f"cluster_id IN {q_c}", a_c),
    }


ROW_KEYS = {  # how each table's rows are found again for deletion
    "conversations": ("device", "start_ms"),
    "speaker_clusters": ("id",),
    "speaker_turns": ("id",),
    "stars": ("device", "ts_ms"),
    "paragraph_samples": ("id",),
    "task_requests": ("device", "start_ms", "item"),
    "task_scans": ("device", "start_ms"),
    "strangers": ("id",),
    "noise_exemplars": ("id",),
    "voice_offers": ("day", "cluster_id"),
}


def chunk_rows(con: sqlite3.Connection, conv: dict) -> list[tuple]:
    """(rowid, device, page, ord, opus_sha, ts_ms, opus, done) of this
    conversation's audio: the pendant's chunks timed inside its span, less any
    inside another conversation of the same pendant."""
    device, start, end = conv["device"], conv["start_ms"], conv["end_ms"]
    others = con.execute("SELECT start_ms, end_ms FROM conversations WHERE device=? AND start_ms != ? "
                         "AND end_ms >= ? AND start_ms <= ?", (device, start, start, end)).fetchall()
    rows = con.execute("SELECT rowid, device, page, ord, opus_sha, ts_ms, opus, done FROM chunks "
                       "WHERE device=? AND done IN (0, 1) AND ts_ms BETWEEN ? AND ?", (device, start, end)).fetchall()
    return [r for r in rows if not any(a <= r[5] <= b for a, b in others)]


# ---------------------------------------------------------------------------
# locking and helpers

@contextlib.contextmanager
def processor_lock(data_dir: Path, wait_s: float | None = None):
    """The processor's lock; waits up to wait_s (LOCK_WAIT_S) for a running pass, then 503."""
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    with open(Path(data_dir) / ".process.lock", "a") as lock:
        deadline = time.monotonic() + (LOCK_WAIT_S if wait_s is None else wait_s)
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TrashError(503, "the processor is busy; try again") from None
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def open_db(data_dir: Path) -> sqlite3.Connection:
    from . import process
    db = Path(data_dir) / "pages.db"
    if not db.exists():
        raise TrashError(404, "no such conversation")
    con = process.db(db)
    con.commit()
    con.isolation_level = None   # explicit BEGIN/COMMIT below
    return con


def unrecovered(data_dir: Path) -> list[str]:
    """Delete/restore/purge work left part way that recover() hasn't resolved."""
    root = trash_dir(data_dir)
    if not root.exists():
        return []
    left = [d.name for pat in (".*.deleting", ".*.restoring", ".*.purging") for d in root.glob(pat)]
    for d in root.iterdir():
        if d.is_dir() and not d.name.startswith("."):
            m = _read_manifest(d)
            if m is None or m.get("state") != "finished":
                left.append(d.name)
    return sorted(left)


def recover_or_refuse(con: sqlite3.Connection, data_dir: Path, notes_dir: Path) -> None:
    """recover(), then 503 if anything is still unresolved (or recovery failed):
    nothing else changes the vault or database until it is."""
    try:
        recover(con, data_dir, notes_dir)
    except Exception as e:  # noqa: BLE001
        print(f"trash: recovery failed ({type(e).__name__}: {str(e)[:160]})", flush=True)
        raise TrashError(503, "an earlier delete or restore needs recovery; try again later") from None
    left = unrecovered(data_dir)
    if left:
        raise TrashError(503, f"an earlier delete or restore needs recovery ({', '.join(left)})")


def _trashed(con: sqlite3.Connection, tid: str) -> bool:
    return con.execute("SELECT 1 FROM trash WHERE id=?", (tid,)).fetchone() is not None


def _vdir(data_dir: Path) -> Path:
    return Path(data_dir) / "voiceprints"


def _read_manifest(d: Path) -> dict | None:
    try:
        return json.loads((d / "manifest.json").read_text())
    except (OSError, ValueError):
        return None


def _write_manifest(d: Path, m: dict) -> None:
    _write_text(d / "manifest.json", json.dumps(m))


def _fsync_dir(d: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _lib_path(data_dir: Path, name: str, entry: dict) -> Path:
    from . import voice_library
    return voice_library.person_dir(data_dir, name) / f"{entry['id']}.opus"


def _copy_library(data_dir: Path, work: Path, device: str, start_ms: int) -> list:
    """Copy the voice library clips cut from this recording into the trash copy; [[name, entry], ...]."""
    from . import voice_library
    items = []
    for name, e in voice_library.entries_for_conversation(data_dir, device, start_ms):
        src = _lib_path(data_dir, name, e)
        if src.exists():
            (work / "library" / name).mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, work / "library" / name / src.name)
            items.append([name, e])
    return items


def _drop_library(m: dict, data_dir: Path, work: Path) -> None:
    """After the delete's commit: the live library loses those clips (only if the trash copy has them)."""
    from . import voice_library
    gone = []
    for name, e in m.get("library") or []:
        live, copy = _lib_path(data_dir, name, e), work / "library" / name / f"{e['id']}.opus"
        if copy.exists():
            live.unlink(missing_ok=True)
            gone.append((name, e))
    if gone:
        voice_library.remove_entries(data_dir, gone)


def _put_library(m: dict, data_dir: Path, work: Path) -> list[Path]:
    """Restore: put the library clips back (a clip that exists stays); returns the files placed."""
    from . import voice_library
    placed, items = [], []
    for name, e in m.get("library") or []:
        live, copy = _lib_path(data_dir, name, e), work / "library" / name / f"{e['id']}.opus"
        if not copy.exists():
            continue
        if not live.exists():
            live.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(copy, live)
            placed.append(live)
        items.append((name, e))
    voice_library.put_entries(data_dir, items)
    return placed


def _undo_library(m: dict, data_dir: Path, placed: list[Path]) -> None:
    from . import voice_library
    for live in placed:
        live.unlink(missing_ok=True)
    voice_library.remove_entries(data_dir, [(n, e) for n, e in m.get("library") or []
                                            if _lib_path(data_dir, n, e) in placed])


def _files(m: dict, data_dir: Path, notes_dir: Path) -> list[tuple[Path, Path]]:
    """(trash copy, live path) for the recording's files."""
    out = []
    if m["files"].get("event"):
        out.append((Path("event") / m["files"]["event"], Path(notes_dir).parent / "Events" / m["files"]["event"]))
    if m["files"].get("clip"):
        out.append((Path("clip") / m["files"]["clip"], Path(data_dir) / "clips" / m["files"]["clip"]))
    return out


def _same(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _highlights(con, day: str, notes_dir: Path, strict: bool = False) -> None:
    """Rebuild the day's Highlights. strict: let a failure through (a delete then
    stays unfinished and recover() retries it)."""
    from . import process
    try:
        process.write_highlights(con, day, notes_dir)
    except Exception as e:  # noqa: BLE001 - the restore itself stands; the next run rewrites it
        if strict:
            raise
        print(f"trash: highlights for {day} failed ({type(e).__name__})", flush=True)


def _later_headings(con, m: dict) -> set[str]:
    return {r[0] for r in con.execute("SELECT raw_heading FROM conversations WHERE day=? AND start_ms > ?",
                                      (m["day"], m["start_ms"]))}


def _put_section(notes_dir: Path, m: dict, later: set[str]) -> bool:
    """Put the section back into the Raw note if it's missing. True when written."""
    if not m.get("section"):
        return False
    daily = Path(notes_dir) / f"{m['day']}.md"
    text = daily.read_text() if daily.exists() else ""
    out = insert_section(text, m["section"], later)
    if out == text:
        return False
    _write_text(daily, out)
    return True


def _cut_again(notes_dir: Path, m: dict) -> None:
    """Take the section out again (undoing a restore)."""
    if not m.get("section"):
        return
    daily = Path(notes_dir) / f"{m['day']}.md"
    if daily.exists():
        cut = cut_section(daily.read_text(), m["raw_heading"])
        if cut:
            _write_text(daily, cut[0])


# ---------------------------------------------------------------------------
# delete

def delete(data_dir: Path, notes_dir: Path, device: str, start_ms: int) -> dict:
    data_dir, notes_dir = Path(data_dir), Path(notes_dir)
    tid = trash_id(device, start_ms)
    with processor_lock(data_dir):
        con = open_db(data_dir)
        try:
            recover_or_refuse(con, data_dir, notes_dir)
            if _trashed(con, tid):
                return {"id": tid, "deleted": True}
            from . import voices
            voices.apply_relabels(con, notes_dir)   # queued speaker edits land before the copy
            con.isolation_level = None
            from . import process
            conv = process.conversation_row(con, device, start_ms)
            if conv is None:
                raise TrashError(404, "no such conversation")
            return _delete(con, data_dir, notes_dir, conv)
        finally:
            con.close()


def _delete(con, data_dir: Path, notes_dir: Path, conv: dict) -> dict:
    from . import speakers
    device, start_ms, day = conv["device"], conv["start_ms"], conv["day"]
    tid = trash_id(device, start_ms)
    root = trash_dir(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    work = root / f".{tid}.deleting"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    daily = notes_dir / f"{day}.md"
    try:
        # 1. the copy: nothing live changes until the manifest is on disk
        text = daily.read_text() if daily.exists() else None
        cut = cut_section(text, conv["raw_heading"]) if text is not None and conv["raw_heading"] else None
        files = {"event": None, "clip": None}
        event = notes_dir.parent / "Events" / conv["event_file"] if conv["event_file"] else None
        if event and event.exists():
            (work / "event").mkdir()
            shutil.copy2(event, work / "event" / event.name)
            files["event"] = event.name
        clip = data_dir / "clips" / Path(conv["clip"]).name if conv["clip"] else None
        if clip and clip.exists():
            (work / "clip").mkdir()
            shutil.copy2(clip, work / "clip" / clip.name)
            files["clip"] = clip.name
        library = _copy_library(data_dir, work, device, start_ms)
        chunks = chunk_rows(con, conv)
        cdb = sqlite3.connect(work / "chunks.db")
        try:
            cdb.execute("CREATE TABLE chunks (device TEXT, page INT, ord INT, opus_sha TEXT, ts_ms INT, opus BLOB, "
                        "done INT)")
            cdb.executemany("INSERT INTO chunks VALUES (?,?,?,?,?,?,?)", [r[1:] for r in chunks])
            cdb.commit()
        finally:
            cdb.close()
        rows = snapshot_rows(con, device, start_ms)
        keys = [r["id"] for r in rows["speaker_clusters"]] + [r["key"] for r in rows["paragraph_samples"]]
        vdir = _vdir(data_dir)
        samples = speakers.take(vdir, keys, dry_run=True)
        m = {"version": 1, "id": tid, "device": device, "start_ms": start_ms, "end_ms": conv["end_ms"], "day": day,
             "title": conv["title"], "raw_heading": conv["raw_heading"], "pendant": conv["pendant"],
             "audio_s": conv["audio_s"], "deleted_at": time.time(), "section": cut[1] if cut else None,
             "files": files, "chunks": len(chunks), "rows": rows, "voiceprints": samples,
             "library": library}
        _write_manifest(work, m)
        _fsync_dir(work)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise

    began = cut_written = False
    try:
        # 2. the rows, in one transaction (committed last)
        con.execute("BEGIN IMMEDIATE")
        began = True
        for table, trows in rows.items():
            key = ROW_KEYS[table]
            for r in trows:
                con.execute(f"DELETE FROM {table} WHERE {' AND '.join(f'{k}=?' for k in key)}",
                            [_dec(r[k]) for k in key])
        con.executemany("DELETE FROM chunks WHERE rowid=?", [(r[0],) for r in chunks])
        con.executemany("INSERT OR IGNORE INTO tombstones VALUES (?,?,?,?,?,?)",
                        [(r[1], r[2], r[3], r[4], r[5], tid) for r in chunks])
        con.execute("INSERT OR REPLACE INTO range_tombstones VALUES (?,?,?,?)",
                    (tid, device, start_ms, conv["end_ms"]))
        con.executemany("INSERT OR IGNORE INTO star_tombstones VALUES (?,?,?)",
                        [(r["device"], r["ts_ms"], tid) for r in rows["stars"]])
        con.execute("INSERT INTO trash (id, device, start_ms, end_ms, day, title, raw_heading, pendant, audio_s, "
                    "deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (tid, device, start_ms, conv["end_ms"], day, conv["title"], conv["raw_heading"], conv["pendant"],
                     conv["audio_s"], m["deleted_at"]))
        _checkpoint("rows")
        # 3. voiceprint samples out
        speakers.take(vdir, keys)
        _checkpoint("voiceprints")
        # 4. the section out of the Raw note
        if cut:
            _write_text(daily, cut[0])
            cut_written = True
        _checkpoint("section")
        # 5. the commit is the delete
        con.execute("COMMIT")
    except BaseException:
        _undo_delete(con, began, cut_written, daily, text, vdir, samples, work)
        raise
    _checkpoint("committed")
    _finish_delete(con, root, work, m, data_dir, notes_dir)
    return {"id": tid, "deleted": True, "deleted_at": m["deleted_at"]}


def _undo_delete(con, began: bool, cut_written: bool, daily: Path, text: str | None, vdir: Path,
                 samples: list[dict], work: Path) -> None:
    from . import speakers
    if began:
        with contextlib.suppress(sqlite3.Error):
            con.execute("ROLLBACK")
    if cut_written:
        _write_text(daily, text)
    speakers.put(vdir, samples)
    shutil.rmtree(work, ignore_errors=True)


def _finish_delete(con, root: Path, work: Path, m: dict, data_dir: Path, notes_dir: Path) -> None:
    """After the commit, still in `.<id>.deleting`: remove the live Event note and clip
    (only if they are the trashed copies), rebuild Highlights, mark the manifest
    finished, and only then move the entry to its final name. Every step can be
    repeated, so recover() finishes a delete stopped anywhere in here."""
    for copy, live in _files(m, data_dir, notes_dir):
        if live.exists() and _same(work / copy, live):
            live.unlink()
    _drop_library(m, data_dir, work)       # the voice library's clips of this recording ride in the trash copy
    _checkpoint("files")
    _highlights(con, m["day"], notes_dir, strict=True)
    _checkpoint("highlights")
    m["state"] = "finished"
    _write_manifest(work, m)
    _checkpoint("finished")
    final = root / m["id"]
    if final.exists():  # a leftover that no trash row owned: keep it aside rather than lose it
        os.replace(final, root / f".{m['id']}.stale-{int(time.time())}")
    os.replace(work, final)
    _fsync_dir(root)


# ---------------------------------------------------------------------------
# restore

def restore(data_dir: Path, notes_dir: Path, device: str, start_ms: int) -> dict:
    data_dir, notes_dir = Path(data_dir), Path(notes_dir)
    tid = trash_id(device, start_ms)
    with processor_lock(data_dir):
        con = open_db(data_dir)
        try:
            recover_or_refuse(con, data_dir, notes_dir)
            if not _trashed(con, tid):
                if con.execute("SELECT 1 FROM conversations WHERE device=? AND start_ms=?",
                               (device, start_ms)).fetchone():
                    return {"id": tid, "restored": True}
                raise TrashError(404, "not in the trash")
            return _restore(con, data_dir, notes_dir, tid)
        finally:
            con.close()


def _free_ids(con, data_dir: Path, m: dict) -> dict[int, int]:
    """Old cluster id -> the id to restore it under (the same, unless reused)."""
    from . import speakers
    vdir = _vdir(data_dir)
    in_prints = {i for name in speakers._names(vdir) if not name.startswith(".")
                 for i in speakers.members(vdir, name)} if vdir.exists() else set()

    def taken(i: int) -> bool:
        return bool(i in in_prints
                    or con.execute("SELECT 1 FROM speaker_clusters WHERE id=?", (i,)).fetchone()
                    or con.execute("SELECT 1 FROM strangers WHERE cluster_id=?", (i,)).fetchone()
                    or con.execute("SELECT 1 FROM noise_exemplars WHERE cluster_id=?", (i,)).fetchone()
                    or con.execute("SELECT 1 FROM voice_offers WHERE cluster_id=?", (i,)).fetchone())
    top = max([con.execute("SELECT COALESCE(MAX(id), 0) FROM speaker_clusters").fetchone()[0], *in_prints, 0])
    out = {}
    for r in m["rows"]["speaker_clusters"]:
        old = r["id"]
        if taken(old):
            top += 1
            while taken(top):
                top += 1
            out[old] = top
        else:
            out[old] = old
    return out


def _restore(con, data_dir: Path, notes_dir: Path, tid: str) -> dict:
    from . import speakers
    root = trash_dir(data_dir)
    final = root / tid
    m = _read_manifest(final)
    if m is None:
        raise TrashError(500, "trash entry unreadable")
    if con.execute("SELECT 1 FROM conversations WHERE device=? AND start_ms=?",
                   (m["device"], m["start_ms"])).fetchone():
        raise TrashError(409, "a conversation with that key exists")
    for copy, live in _files(m, data_dir, notes_dir):
        if live.exists() and not _same(final / copy, live):
            raise TrashError(409, f"{live.name} exists with different content; nothing restored")
        if not (final / copy).exists():
            raise TrashError(500, f"the trash copy of {live.name} is missing; nothing restored")
    ids = _free_ids(con, data_dir, m)
    samples = [{**s, "id": ids.get(s["id"], s["id"])} for s in m["voiceprints"]]
    m = {**m, "restore_samples": samples}
    work = root / f".{tid}.restoring"
    os.replace(final, work)
    _write_manifest(work, m)   # the ids samples go back under, for recover()
    vdir = _vdir(data_dir)
    began = section_written = False
    placed: list[Path] = []
    try:
        for copy, live in _files(m, data_dir, notes_dir):
            if not live.exists():
                live.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(work / copy, live)
                placed.append(live)
        placed += _put_library(m, data_dir, work)
        con.execute("BEGIN IMMEDIATE")
        began = True
        for table, trows in m["rows"].items():
            for r in trows:
                r = {k: _dec(v) for k, v in r.items()}
                if table == "speaker_clusters":
                    r["id"] = ids[r["id"]]
                elif table in ("noise_exemplars", "voice_offers", "strangers"):
                    r["cluster_id"] = ids.get(r["cluster_id"], r["cluster_id"])
                if table in ("strangers", "noise_exemplars", "speaker_turns") and con.execute(
                        f"SELECT 1 FROM {table} WHERE id=?", (r["id"],)).fetchone():
                    r.pop("id")      # its row id was reused; any id will do
                verb = "INSERT OR IGNORE" if table in ("stars", "voice_offers", "task_scans") else "INSERT"
                con.execute(f"{verb} INTO {table} ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})", list(r.values()))
        cdb = sqlite3.connect(work / "chunks.db")
        try:
            chunks = cdb.execute("SELECT device, page, ord, opus_sha, ts_ms, opus, done FROM chunks").fetchall()
        finally:
            cdb.close()
        con.executemany("INSERT OR IGNORE INTO chunks (device, page, ord, opus_sha, ts_ms, opus, done) "
                        "VALUES (?,?,?,?,?,?,?)", chunks)
        for table in ("tombstones", "range_tombstones", "star_tombstones"):
            con.execute(f"DELETE FROM {table} WHERE trash_id=?", (tid,))
        con.execute("DELETE FROM trash WHERE id=?", (tid,))
        _checkpoint("rows")
        speakers.put(vdir, samples)
        _checkpoint("voiceprints")
        section_written = _put_section(notes_dir, m, _later_headings(con, m))
        _checkpoint("section")
        con.execute("COMMIT")
    except BaseException:
        _undo_library(m, data_dir, placed)
        _undo_restore(con, began, section_written, notes_dir, m, vdir, samples, placed, work, final)
        raise
    _checkpoint("committed")
    shutil.rmtree(work, ignore_errors=True)
    _highlights(con, m["day"], notes_dir)
    return {"id": tid, "restored": True}


def _undo_restore(con, began: bool, section_written: bool, notes_dir: Path, m: dict, vdir: Path,
                  samples: list[dict], placed: list[Path], work: Path, final: Path) -> None:
    from . import speakers
    if began:
        with contextlib.suppress(sqlite3.Error):
            con.execute("ROLLBACK")
    if section_written:
        _cut_again(notes_dir, m)
    speakers.take(vdir, [s["id"] for s in samples])
    for live in placed:
        live.unlink(missing_ok=True)
    os.replace(work, final)


# ---------------------------------------------------------------------------
# recovery, purge, listing

def recover(con: sqlite3.Connection, data_dir: Path, notes_dir: Path) -> list[str]:
    """Finish or undo a delete or restore that stopped part way (the process was
    killed). Caller holds the processor's lock. Returns what was done."""
    from . import speakers
    root = trash_dir(data_dir)
    if not root.exists():
        return []
    con.commit()
    done = []
    vdir = _vdir(data_dir)
    for d in sorted(root.glob(".*.deleting")):
        tid = d.name[1:-len(".deleting")]
        m = _read_manifest(d)
        if m is None:
            if _trashed(con, tid):   # committed, yet unreadable: leave it; unrecovered() blocks
                print(f"trash: {d.name} has an unreadable manifest; needs a hand", flush=True)
                continue
            shutil.rmtree(d, ignore_errors=True)   # stopped while copying: nothing live had changed
            continue
        if _trashed(con, tid):   # committed: finish
            _finish_delete(con, root, d, m, data_dir, notes_dir)
            done.append(f"finished deleting {tid}")
        else:                    # not committed: put back what may have moved
            _put_section(notes_dir, m, _later_headings(con, m))
            speakers.put(vdir, m["voiceprints"])
            shutil.rmtree(d, ignore_errors=True)
            done.append(f"undid deleting {tid}")
    for d in sorted(root.iterdir()):     # entries moved to their final name before being finished
        if d.name.startswith(".") or not d.is_dir():
            continue
        m = _read_manifest(d)
        if m is not None and m.get("state") == "finished":
            continue
        if not _trashed(con, d.name):   # no trash row owns it: set aside, never lost
            os.replace(d, root / f".{d.name}.stale-{int(time.time())}")
            done.append(f"set aside {d.name}")
            continue
        if m is None:   # owned but unreadable: leave it where restore looks; unrecovered() blocks
            print(f"trash: {d.name} has an unreadable manifest; needs a hand", flush=True)
            continue
        work = root / f".{d.name}.deleting"
        os.replace(d, work)
        _finish_delete(con, root, work, m, data_dir, notes_dir)
        done.append(f"finished deleting {d.name}")
    for d in sorted(root.glob(".*.restoring")):
        tid = d.name[1:-len(".restoring")]
        m = _read_manifest(d)
        if m is None:
            continue
        if _trashed(con, tid):   # not committed: back to the trash
            _cut_again(notes_dir, m)
            speakers.take(vdir, [s["id"] for s in m.get("restore_samples", m["voiceprints"])])
            for copy, live in _files(m, data_dir, notes_dir):
                if live.exists() and _same(d / copy, live):
                    live.unlink()
            _drop_library(m, data_dir, d)
            os.replace(d, root / tid)
            done.append(f"undid restoring {tid}")
        else:
            shutil.rmtree(d, ignore_errors=True)
            _highlights(con, m["day"], notes_dir)
            done.append(f"finished restoring {tid}")
    for d in sorted(root.glob(".*.purging")):
        tid = d.name[1:-len(".purging")]
        if _trashed(con, tid):
            os.replace(d, root / tid)
        else:
            shutil.rmtree(d, ignore_errors=True)
    for line in done:
        print(f"trash: {line}", flush=True)
    return done


def purge(con: sqlite3.Connection, data_dir: Path, notes_dir: Path | None = None,
          now: float | None = None, days: float = TRASH_DAYS) -> list[str]:
    """Empty trash entries deleted more than `days` ago, for good. Tombstones stay,
    so the recording's pages are never processed again. Caller holds the lock."""
    root = trash_dir(data_dir)
    cutoff = (now if now is not None else time.time()) - days * 86400
    con.commit()
    rows = con.execute("SELECT id, device, start_ms FROM trash WHERE deleted_at < ?", (cutoff,)).fetchall()
    gone = []
    for tid, device, start_ms in rows:
        d, p = root / tid, root / f".{tid}.purging"
        if d.exists():
            os.replace(d, p)
        try:
            con.execute("DELETE FROM trash WHERE id=?", (tid,))
            con.execute("DELETE FROM transcript_relabels WHERE device=? AND start_ms=?", (device, start_ms))
            con.commit()
        except BaseException:
            con.rollback()
            if p.exists():
                os.replace(p, d)
            raise
        shutil.rmtree(p, ignore_errors=True)
        gone.append(tid)
        print(f"trash: purged {tid}", flush=True)
    return gone


def list_trash(data_dir: Path) -> list[dict]:
    db = Path(data_dir) / "pages.db"
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15)
    try:
        rows = con.execute("SELECT id, device, start_ms, end_ms, day, title, raw_heading, pendant, audio_s, deleted_at "
                           "FROM trash ORDER BY deleted_at DESC").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    return [{"id": r[0], "device_id": r[1], "start_ms": r[2], "end_ms": r[3], "day": r[4], "title": r[5],
             "heading": r[6], "device": r[7], "duration_s": int(round(r[8] or 0)), "deleted_at": r[9],
             "purge_at": r[9] + TRASH_DAYS * 86400} for r in rows]
