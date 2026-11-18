"""Undo records for speaker reassignments from a transcript (feed.reassign_speaker:
scope line, conversation or all past conversations).

Recording (`record`, around each reassignment the feed accepts, holding the processor's
lock so nothing else writes between its before and after snapshots; 503 when a
processor run holds it past 30 s) keeps, under
`<data>/reassign_undo/<change_id>/`:

- `manifest.json`: the database rows the change could touch, as they were before
  and right after it (`speaker_clusters`, `strangers`, `noise_exemplars`,
  `paragraph_samples`, `voice_offers` for its voices and its line's sample), the
  `bulk_relabels` rows it added, the id of the last transcript relabel queued by
  then, and the files it changed;
- `files/<n>.before`, `files/<n>.after`: copies of each changed file
  (`voiceprints/*.npz`, `voice_library/<Name>/index.json` and clips, `config.json`).

The relabels it queues carry its id (`transcript_relabels.undo_id`), and when they
are applied (by the feed, or by the processor's next run) voices.apply_relabels keeps
the Raw section's text before and after them (`reassign_undo_sections`). The
`reassign_undo` row (state `open` or `undone`) is the deciding mark.

Undo (`undo`, `POST /pendant/feed/reassign/<change_id>/undo`), within 24 h, under the
processor's lock: queued relabels are applied first; then, if anything the change
touched has changed since (its rows differ from what it left, a later relabel was
queued for one of its conversations, a section differs from its text right after the
change), it refuses with 409 and changes nothing. Otherwise every row is put back as
it was, its relabels and bulk rows deleted, each section put back, and each file put
back byte for byte, or, when something else changed the file since (another sample
enrolled, another name allowed), only this change's samples, clips or config entries are
reverted in it. A voiceprint deleted since is never rebuilt (409). A
journal (`journal.json`: each file's bytes before and after the undo) makes it
all-or-nothing: the rows change in one transaction, the files are written before its
commit, and a failure (or, via `recover`, a crash) puts the files back to match the
database; `recover` also runs when the feed and the processor start. After an undo the
conversations' kept whole voices (voice_audio.py) are brought in line with the rows put back. Undoing an undone change returns `already: true` and changes nothing.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path

UNDO_WINDOW_S = 24 * 3600
KEEP_S = 2 * 24 * 3600              # records (and their file copies) are purged after this
DIR = "reassign_undo"
TABLES = ("speaker_clusters", "strangers", "noise_exemplars", "paragraph_samples", "voice_offers")
CHECKED = ("speaker_clusters", "strangers", "noise_exemplars", "paragraph_samples")   # voice_offers: bookkeeping
_LOCK = threading.Lock()            # the feed is one process: one recorded change or undo at a time


class UndoError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code, self.msg = code, msg


def root(data: Path) -> Path:
    return Path(data) / DIR


# ---------------------------------------------------------------------------
# snapshots

def _enc(v):
    return {"b64": base64.b64encode(v).decode()} if isinstance(v, (bytes, bytearray, memoryview)) else v


def _dec(v):
    return base64.b64decode(v["b64"]) if isinstance(v, dict) and "b64" in v else v


def _in(ids) -> tuple[str, list]:
    ids = list(ids)
    return f"({', '.join('?' * len(ids))})" if ids else "(NULL)", ids


def _where(table: str, keys: dict) -> tuple[str, list]:
    if table == "paragraph_samples":
        q, a = _in(keys["samples"])
        return f"id IN {q}", a
    ids = keys["clusters"] + (keys["sample_keys"] if table == "strangers" else [])
    q, a = _in(ids)
    return f"{'id' if table == 'speaker_clusters' else 'cluster_id'} IN {q}", a


def _rows(con: sqlite3.Connection, table: str, keys: dict) -> list[dict]:
    where, args = _where(table, keys)
    cur = con.execute(f"SELECT * FROM {table} WHERE {where}", args)
    cols = [d[0] for d in cur.description]
    rows = [{c: _enc(v) for c, v in zip(cols, r)} for r in cur.fetchall()]
    return sorted(rows, key=lambda r: json.dumps(r, sort_keys=True))


def snapshot_rows(con: sqlite3.Connection, keys: dict) -> dict[str, list[dict]]:
    return {t: _rows(con, t, keys) for t in TABLES}


def tracked_files(data: Path) -> dict[str, bytes]:
    """{path relative to data: bytes} of every file a reassignment can change."""
    data = Path(data)
    out: dict[str, bytes] = {}
    paths = [data / "config.json", *sorted((data / "voiceprints").glob("*.npz"))]
    lib = data / "voice_library"
    if lib.is_dir():
        for d in sorted(p for p in lib.iterdir() if p.is_dir()):
            paths += sorted(d.glob("index.json")) + sorted(d.glob("*.opus"))
    for p in paths:
        if p.name.startswith("."):
            continue
        with contextlib.suppress(OSError):
            out[str(p.relative_to(data))] = p.read_bytes()
    return out


def change_keys(con: sqlite3.Connection, scope: str, device: str, start_ms: int, req: dict) -> dict:
    """The voices and line sample a reassignment of this scope can touch."""
    from . import speakers
    clusters: list[int] = []
    samples: list[str] = []
    sample_keys: list[int] = []
    tag = req.get("tag")
    if scope in ("conversation", "all") and isinstance(tag, str):
        clusters += [r[0] for r in con.execute("SELECT id FROM speaker_clusters WHERE device = ? AND start_ms = ? "
                                               "AND tag = ?", (device, start_ms, tag))]
    if scope == "all" and isinstance(req.get("ids"), list):
        clusters += [i for i in req["ids"] if isinstance(i, int) and not isinstance(i, bool)]
    index = req.get("paragraph")
    if scope == "line" and isinstance(index, int) and not isinstance(index, bool) and index >= 0:
        sid = f"para:{device}:{start_ms}:{index}"
        row = con.execute("SELECT key FROM paragraph_samples WHERE id = ?", (sid,)).fetchone()
        samples.append(sid)
        sample_keys.append(row[0] if row else speakers.sample_key(sid))
    return {"clusters": sorted(set(clusters)), "samples": samples, "sample_keys": sample_keys}


def _max_id(con: sqlite3.Connection, table: str) -> int:
    return con.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.undo.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _open(data: Path) -> sqlite3.Connection | None:
    from . import voices
    db = Path(data) / "pages.db"
    if not db.exists():
        return None
    con = sqlite3.connect(db, timeout=30)
    voices.ensure_schema(con)
    return con


# ---------------------------------------------------------------------------
# recording

def record(data: Path, scope: str, device: str, start_ms: int, req: dict, run, extra=None) -> dict:
    """Run one reassignment (`run()`, which returns the feed's answer) and keep its undo
    record. `extra(con)`, called under the lock, lists more voices the change can touch (the
    sitting's other pieces). Returns the answer with `change_id` and `undo_until` (epoch seconds)."""
    from . import trash, voices
    data = Path(data)
    with _LOCK, contextlib.ExitStack() as stack:
        con = _open(data)
        if con is None:
            return run()
        # the processor (enrollments, relabels, other writes) waits until the change is recorded
        try:
            stack.enter_context(trash.processor_lock(data))
        except trash.TrashError as e:
            con.close()
            raise UndoError(e.code, e.msg) from None
        voices.UNDO.locked = True
        stack.callback(setattr, voices.UNDO, "locked", False)
        try:
            keys = change_keys(con, scope, device, start_ms, req)
            if extra is not None:
                keys["clusters"] = sorted(set(keys["clusters"]) | set(extra(con)))
            before = snapshot_rows(con, keys)
            bulk_floor = _max_id(con, "bulk_relabels")
            files_before = tracked_files(data)
            uid = uuid.uuid4().hex
            voices.UNDO.id = uid
            try:
                out = run()
            finally:
                voices.UNDO.id = None
            after = snapshot_rows(con, keys)
            files_after = tracked_files(data)
            relabels = con.execute("SELECT id, device, start_ms FROM transcript_relabels WHERE undo_id = ?",
                                   (uid,)).fetchall()
            bulk = [r[0] for r in con.execute("SELECT id FROM bulk_relabels WHERE id > ?", (bulk_floor,))]
            convs = {(device, start_ms)} | {(r[1], r[2]) for r in relabels}
            for r in before["speaker_clusters"]:
                convs.add((r["device"], r["start_ms"]))
            now = time.time()
            work = root(data) / f".{uid}.tmp"
            files = {}
            for n, rel in enumerate(sorted(set(files_before) | set(files_after))):
                b, a = files_before.get(rel), files_after.get(rel)
                if b == a:
                    continue
                entry = {"before": None, "after": None}
                for side, blob in (("before", b), ("after", a)):
                    if blob is not None:
                        entry[side] = f"files/{n}.{side}"
                        _write_bytes(work / entry[side], blob)
                files[rel] = entry
            manifest = {"id": uid, "scope": scope, "device": device, "start_ms": start_ms,
                        "name": out.get("name") if isinstance(out, dict) else None, "created_at": now,
                        "keys": keys, "before": before, "after": after, "bulk": bulk,
                        "relabel_floor": _max_id(con, "transcript_relabels"),
                        "conversations": sorted([list(c) for c in convs]), "files": files}
            _write_bytes(work / "manifest.json", json.dumps(manifest).encode())
            os.replace(work, root(data) / uid)
            con.execute("INSERT INTO reassign_undo (id, created_at, scope, device, start_ms, name, state) "
                        "VALUES (?,?,?,?,?,?, 'open')", (uid, now, scope, device, start_ms, manifest["name"]))
            con.commit()
            _purge(con, data, now)
        finally:
            con.close()
    return {**out, "change_id": uid, "undo_until": now + UNDO_WINDOW_S}


def _purge(con: sqlite3.Connection, data: Path, now: float) -> None:
    old = [r[0] for r in con.execute("SELECT id FROM reassign_undo WHERE created_at < ?", (now - KEEP_S,))]
    for uid in old:
        shutil.rmtree(root(data) / uid, ignore_errors=True)
        con.execute("DELETE FROM reassign_undo_sections WHERE undo_id = ?", (uid,))
        con.execute("DELETE FROM reassign_undo WHERE id = ?", (uid,))
    con.commit()


# ---------------------------------------------------------------------------
# merging a file that changed again since

def _npz(blob: bytes | None):
    from . import speakers
    if blob is None:
        return None
    import numpy as np
    with np.load(io.BytesIO(blob)) as d:
        legacy_sum = d["legacy_sum"].astype(np.float32).reshape(-1)
        ids = [int(i) for i in d["ids"]]
        return speakers.Voiceprint(legacy_sum, int(d["legacy"]), ids,
                                   d["embs"].astype(np.float32).reshape(len(ids), len(legacy_sum)))


def _npz_bytes(vp) -> bytes | None:
    import numpy as np
    if vp is None or vp.count <= 0:
        return None
    buf = io.BytesIO()
    np.savez(buf, legacy_sum=vp.legacy_sum.astype(np.float32), legacy=np.int64(vp.legacy),
             ids=np.asarray(vp.ids, dtype=np.int64), embs=vp.embs.astype(np.float32))
    return buf.getvalue()


def _same_vp(a, b) -> bool:
    import numpy as np
    if a is None or b is None:
        return (a is None or a.count <= 0) and (b is None or b.count <= 0)
    return (a.ids == b.ids and a.legacy == b.legacy and np.array_equal(a.legacy_sum, b.legacy_sum)
            and np.array_equal(a.embs, b.embs))


def merge_voiceprint(before: bytes | None, after: bytes | None, cur: bytes | None) -> bytes | None:
    """The voiceprint `cur` without the samples the change added and with the ones it took
    out (at their old places); `before`'s exact bytes when that is what it comes to."""
    import numpy as np
    b, a, c = _npz(before), _npz(after), _npz(cur)
    base = c or a or b
    if base is None:
        return None
    b_ids = b.ids if b else []
    a_ids = a.ids if a else []
    added = {i for i in a_ids if i not in b_ids}
    ids = [i for i in (c.ids if c else []) if i not in added]
    rows = [e for i, e in zip(c.ids, c.embs) if i not in added] if c else []
    for at, (i, e) in enumerate(zip(b_ids, b.embs if b else [])):
        if i not in a_ids and i not in ids:
            k = min(at, len(ids))
            ids.insert(k, i)
            rows.insert(k, e)
    dim = len(base.legacy_sum)
    from . import speakers
    vp = speakers.Voiceprint(c.legacy_sum if c else (b.legacy_sum if b else np.zeros(dim, np.float32)),
                             c.legacy if c else (b.legacy if b else 0), ids,
                             np.stack(rows).astype(np.float32) if rows else np.zeros((0, dim), np.float32))
    if _same_vp(vp, b):
        return before
    return _npz_bytes(vp)


def merge_index(before: bytes | None, after: bytes | None, cur: bytes | None) -> bytes | None:
    """A voice library index without the clips the change added and with the ones it
    removed (at their old places); `before`'s exact bytes when that is what it comes to."""
    def entries(blob):
        try:
            out = json.loads(blob) if blob else []
            return out if isinstance(out, list) else []
        except ValueError:
            return []
    b, a, c = entries(before), entries(after), entries(cur)
    b_by = {e["id"]: e for e in b}
    a_by = {e["id"]: e for e in a}
    added = {i for i, e in a_by.items() if b_by.get(i) != e}
    out = [e for e in c if e["id"] not in added]
    for at, e in enumerate(b):
        if a_by.get(e["id"]) != e and all(x["id"] != e["id"] for x in out):
            out.insert(min(at, len(out)), e)
    if out == b:
        return before
    return (json.dumps(out, indent=1) + "\n").encode() if out else None


def _merge(b, a, c):
    """c without what changed from b to a, and with b's values back where a changed them
    (dicts by key, lists by item; anything else by value)."""
    if isinstance(a, dict) and isinstance(c, dict):
        b = b if isinstance(b, dict) else {}
        out = dict(c)
        for k in set(a) | set(b):
            if k not in a:                       # removed by the change
                out.setdefault(k, b[k])
            elif k not in b:                     # added by the change
                if k in out and out[k] == a[k]:
                    del out[k]
                elif k in out and type(out[k]) is type(a[k]) and isinstance(a[k], (dict, list)):
                    out[k] = _merge(type(a[k])(), a[k], out[k])   # keep only what was added since
                    if not out[k]:
                        del out[k]
            elif a[k] != b[k] and k in out:
                out[k] = _merge(b[k], a[k], out[k])
        return out
    if isinstance(a, list) and isinstance(c, list):
        b = b if isinstance(b, list) else []
        out = [x for x in c if not (x in a and x not in b)]
        for at, x in enumerate(b):
            if x not in a and x not in out:
                out.insert(min(at, len(out)), x)
        return out
    return b if c == a else c


def merge_config(before: bytes | None, after: bytes | None, cur: bytes | None) -> bytes | None:
    """config.json without what the change added (a newly allowed name, a LifeOS link) and
    with what it removed, keeping everything else changed since; `before`'s bytes when
    that is what it comes to."""
    try:
        b, a, c = (json.loads(x) if x else None for x in (before, after, cur))
    except ValueError:
        return cur
    if c is None:
        return cur
    out = _merge(b, a, c)
    if out == b:
        return before
    return (json.dumps(out, indent=2) + "\n").encode()


# ---------------------------------------------------------------------------
# undo

def _manifest(data: Path, uid: str) -> dict | None:
    try:
        return json.loads((root(data) / uid / "manifest.json").read_text())
    except (OSError, ValueError):
        return None


def _blob(data: Path, uid: str, rel: str | None) -> bytes | None:
    return (root(data) / uid / rel).read_bytes() if rel else None


def _put_files(plan: dict[str, bytes | None]) -> None:
    """Write each absolute path's bytes (None: remove it)."""
    for p, blob in plan.items():
        path = Path(p)
        if blob is None:
            path.unlink(missing_ok=True)
            if path.parent.parent.name == "voice_library":
                with contextlib.suppress(OSError):
                    path.parent.rmdir()          # an empty person folder goes, as voice_library leaves it
        else:
            _write_bytes(path, blob)


def _journal(data: Path, uid: str) -> Path:
    return root(data) / uid / "journal.json"


def _b64(v: bytes | None):
    return None if v is None else base64.b64encode(v).decode()


def _unb64(v) -> bytes | None:
    return None if v is None else base64.b64decode(v)


def recover(con: sqlite3.Connection, data: Path) -> None:
    """Finish or roll back an undo cut short: its files follow the database's mark."""
    r = root(data)
    if not r.is_dir():
        return
    for j in r.glob("*/journal.json"):
        uid = j.parent.name
        try:
            jr = json.loads(j.read_text())
        except (OSError, ValueError):
            j.unlink(missing_ok=True)
            continue
        row = con.execute("SELECT state FROM reassign_undo WHERE id = ?", (uid,)).fetchone()
        side = "target" if row and row[0] == "undone" else "pre"
        _put_files({k: _unb64(v[side]) for k, v in jr["files"].items()})
        j.unlink(missing_ok=True)


def _conflict(msg: str):
    raise UndoError(409, msg)


def undo(data: Path, notes: Path, uid: str, now: float | None = None) -> dict:
    """Undo one recorded reassignment (see the module docstring). Raises UndoError:
    404 unknown, 410 older than 24 h, 409 changed since, 503 the processor is busy."""
    from . import trash, voices
    data, notes = Path(data), Path(notes)
    now = time.time() if now is None else now
    with _LOCK, contextlib.ExitStack() as stack:
        try:
            stack.enter_context(trash.processor_lock(data))
        except trash.TrashError as e:
            raise UndoError(e.code, e.msg) from None
        con = _open(data)
        if con is None:
            raise UndoError(404, "no such change")
        stack.callback(con.close)
        recover(con, data)
        row = con.execute("SELECT state, created_at, scope, name FROM reassign_undo WHERE id = ?",
                          (uid,)).fetchone()
        m = _manifest(data, uid)
        if row is None or m is None:
            raise UndoError(404, "no such change")
        state, created, scope, name = row
        out = {"change_id": uid, "undone": True, "already": state == "undone", "scope": scope, "name": name}
        if state == "undone":
            return out
        if now - created > UNDO_WINDOW_S:
            raise UndoError(410, "too old to undo")
        voices.apply_relabels(con, notes)       # the change's relabels land before comparing
        con.isolation_level = None
        con.execute("BEGIN IMMEDIATE")
        try:
            _undo(con, data, notes, uid, m)
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        _keep_voices(con, data, notes, m)
        return out


def _keep_voices(con, data: Path, notes: Path, m: dict) -> None:
    """The kept whole voices (voice_audio.py) of the change's conversations follow the undone rows; a failure
    is logged, never undoes the undo."""
    try:
        from . import config as cfgmod, voice_audio
        cfg = cfgmod.load(data)
        for key in sorted({(r["device"], r["start_ms"]) for side in ("before", "after")
                           for r in m[side].get("speaker_clusters") or ()}):
            voice_audio.sync_conversation(con, data, notes, cfg, *key)
    except Exception as e:  # noqa: BLE001
        print(f"reassign undo: voice audio failed ({type(e).__name__}: {str(e)[:100]})", flush=True)


def _check(con, notes: Path, uid: str, m: dict) -> dict[str, str]:
    """Refuse (409) when anything the change touched changed since; else {day: note text after the undo}."""
    from . import voices
    from .process import section_bounds
    keys = m["keys"]
    cur = snapshot_rows(con, keys)
    for t in CHECKED:
        if cur[t] != m["after"][t]:
            _conflict("changed again since; undo the later change first")
    for d, ms in (tuple(c) for c in m["conversations"]):
        if con.execute("SELECT 1 FROM transcript_relabels WHERE id > ? AND device = ? AND start_ms = ? "
                       "AND (undo_id IS NULL OR undo_id != ?) LIMIT 1", (m["relabel_floor"], d, ms, uid)).fetchone():
            _conflict("this conversation was edited after the change; undo that first")
    for t in ("strangers", "noise_exemplars"):     # a freed row id taken by another record since
        mine = set(keys["clusters"]) | (set(keys["sample_keys"]) if t == "strangers" else set())
        for r in m["before"][t]:
            hit = con.execute(f"SELECT cluster_id FROM {t} WHERE id = ?", (r["id"],)).fetchone()
            if hit and hit[0] not in mine:
                _conflict("changed again since; undo the later change first")
    texts: dict[str, str] = {}
    for day, heading, before, after in con.execute(
            "SELECT day, heading, before, after FROM reassign_undo_sections WHERE undo_id = ? "
            "ORDER BY day, start_ms", (uid,)).fetchall():
        if before is None or before == after:
            continue
        if day not in texts:
            path = notes / f"{day}.md"
            if not path.exists():
                _conflict("the conversation's note is gone")
            texts[day] = path.read_text()
        if voices.section_text(texts[day], heading) != after:
            _conflict("this conversation was edited after the change; undo that first")
        lines = texts[day].split("\n")
        b = section_bounds(lines, heading)
        lines[b[0]:b[1]] = before.split("\n")
        texts[day] = "\n".join(lines)
    return texts


def _undo(con, data: Path, notes: Path, uid: str, m: dict) -> None:
    from . import speakers, voice_library
    texts = _check(con, notes, uid, m)
    pre: dict[str, bytes | None] = {}
    plan: dict[str, bytes | None] = {}
    for day, text in texts.items():
        path = notes / f"{day}.md"
        pre[str(path)], plan[str(path)] = path.read_bytes(), text.encode()
    with speakers.voiceprint_lock(speakers.voiceprint_dir(data)), voice_library._lock(data):
        for rel, f in m["files"].items():
            path = data / rel
            before, after = _blob(data, uid, f["before"]), _blob(data, uid, f["after"])
            now_b = path.read_bytes() if path.exists() else None
            if now_b is None and after is not None and rel.startswith("voiceprints/"):
                # deleted since (a consent decision): never rebuilt by an undo
                _conflict("that voiceprint was deleted since; it isn't brought back")
            if now_b == after:
                target = before
            elif rel.startswith("voiceprints/"):
                target = merge_voiceprint(before, after, now_b)
            elif rel.endswith("/index.json"):
                target = merge_index(before, after, now_b)
            elif rel == "config.json":
                target = merge_config(before, after, now_b)
            else:
                target = now_b              # a clip changed by something else since: left alone
            pre[str(path)], plan[str(path)] = now_b, target
        plan = {k: v for k, v in plan.items() if pre[k] != v}
        _write_bytes(_journal(data, uid), json.dumps(
            {"files": {k: {"pre": _b64(pre[k]), "target": _b64(v)} for k, v in plan.items()}}).encode())
        try:
            keys = m["keys"]
            con.execute("DELETE FROM transcript_relabels WHERE undo_id = ?", (uid,))
            q, a = _in(m["bulk"])
            con.execute(f"DELETE FROM bulk_relabels WHERE id IN {q}", a)
            for t in TABLES:
                where, args = _where(t, keys)
                con.execute(f"DELETE FROM {t} WHERE {where}", args)
                for r in m["before"][t]:
                    cols = list(r)
                    con.execute(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                [_dec(r[c]) for c in cols])
            con.execute("UPDATE reassign_undo SET state = 'undone', undone_at = ? WHERE id = ?", (time.time(), uid))
            _put_files(plan)
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            _put_files({k: pre[k] for k in plan})
            raise
        finally:
            _journal(data, uid).unlink(missing_ok=True)
