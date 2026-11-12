"""Rename a speaker everywhere.

    python -m pendant_direct.rename_speaker OLD NEW [--apply] [--data DIR] [--notes DIR]

Dry run by default: prints what would change in each place and changes nothing. Runs under
the processor's lock. Places, all renamed from OLD to NEW (case-insensitive match):

- the voiceprint (`voiceprints/OLD.npz` becomes `NEW.npz`; merged when NEW already has one)
- config.json: `speakers.allowed`, `speakers.people` (the LifeOS link), `speakers.wearer`
  and any other string in the `speakers` section equal to OLD
- pages.db: `speaker_clusters` label and tag, queued and past relabels, undo records of bulk
  relabels, `paragraph_samples` holders, `enrollments` names
- the Raw notes: `**OLD:**` becomes `**NEW:**` only where a paragraph starts with its speaker
  label (`[HH:MM] **OLD:** ...`), never inside spoken text. Each changed note is copied to
  `<data>/backups/rename-speaker-<time>/` first.
- the voice library folder `voice_library/OLD`, when there is one (clips are merged into NEW's)

Refuses when OLD and NEW are linked to two different LifeOS people. All or nothing: everything is
read and checked first, then written with snapshots of the voiceprint files, config, library
folders and notes and one database transaction; a failure at any stage restores them all.
A case-only rename (Alex to alex) is a plain rename of the same speaker, never a merge.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from . import config

# Every table and column that stores a speaker name (calendar enrollment writes the name to
# enrolled_in; bulk_relabels keeps the previous values for undo).
DB_COLUMNS = (("speaker_clusters", "label"), ("speaker_clusters", "tag"), ("speaker_clusters", "enrolled_in"),
              ("transcript_relabels", "old_tag"), ("transcript_relabels", "new_tag"),
              ("bulk_relabels", "old_tag"), ("bulk_relabels", "new_tag"), ("bulk_relabels", "old_label"),
              ("bulk_relabels", "old_enrolled_in"),
              ("paragraph_samples", "holder"), ("enrollments", "name"))


class Refused(Exception):
    pass


def _same(a, b) -> bool:
    return isinstance(a, str) and str(a).lower() == str(b).lower()


# ---------------------------------------------------------------- config

def _replace_strings(node, old: str, new: str) -> tuple[object, int]:
    """node with every string equal to `old` (and dict keys equal to it) replaced, and the count."""
    n = 0
    if isinstance(node, str):
        return (new, 1) if _same(node, old) else (node, 0)
    if isinstance(node, list):
        out = []
        for x in node:
            y, c = _replace_strings(x, old, new)
            out.append(y)
            n += c
        seen, dedup = set(), []
        for y in out:     # a list that held both names keeps one
            k = y.lower() if isinstance(y, str) else None
            if k is not None and k in seen:
                continue
            seen.add(k)
            dedup.append(y)
        return dedup, n
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            v2, c = _replace_strings(v, old, new)
            n += c
            if _same(k, old):
                k, n = new, n + 1
            out[k] = v2
        return out, n
    return node, 0


def plan_config(data_dir: Path, old: str, new: str) -> tuple[dict | None, dict]:
    """(the new raw config or None when nothing changes, counts). Defaults that name OLD
    (an unset `allowed` or `wearer`) are written out explicitly."""
    path = Path(data_dir) / "config.json"
    raw = config._read_raw(path)
    effective = config.load(data_dir)["speakers"]
    sp = raw.get("speakers") if isinstance(raw.get("speakers"), dict) else {}
    counts = {"allowed": 0, "people": 0, "wearer": 0, "other": 0}
    for key in ("allowed", "wearer"):
        if key not in sp and any(_same(x, old) for x in (effective[key] if isinstance(effective[key], list) else [effective[key]])):
            sp[key] = list(effective[key]) if isinstance(effective[key], list) else effective[key]
    out = {}
    for k, v in sp.items():
        v2, c = _replace_strings(v, old, new)
        counts[k if k in ("allowed", "wearer") else "people" if k == "people" else "other"] += c
        out[k] = v2
    if not any(counts.values()):
        return None, counts
    # merging into an existing link: keep OLD's when NEW has none, else they must agree (checked earlier)
    raw = dict(raw)
    raw["speakers"] = out
    return raw, counts


def check_links(data_dir: Path, old: str, new: str) -> None:
    people = config.load(data_dir)["speakers"].get("people") or {}
    a, b = config.linked_entity(people, old), config.linked_entity(people, new)
    if a and b and a != b:
        raise Refused(f"{new} is already linked to a different LifeOS person than {old}")


# ---------------------------------------------------------------- voiceprints

class _nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _vp_files(vdir: Path, stem: str) -> list[Path]:
    return [vdir / f"{stem}{ext}" for ext in (".npz", ".npy", ".json") if (vdir / f"{stem}{ext}").exists()]


def plan_voiceprint(data_dir: Path, old: str, new: str) -> dict:
    from . import speakers
    vdir = speakers.voiceprint_dir(data_dir)
    names = [n for n in speakers._names(vdir) if not n.startswith(".")]
    stem = next((n for n in names if _same(n, old)), None)
    vp = speakers._read(vdir, stem) if stem else None
    if vp is None:
        return {"voiceprint": 0, "samples": 0, "merged": False, "stem": None}
    # another file of that name is a merge; the same file (a case-only rename) is a plain rename
    target = next((n for n in names if _same(n, new) and n != stem), None)
    return {"voiceprint": 1, "samples": vp.count, "merged": target is not None, "stem": stem,
            "target": target or new, "vp": vp}


def apply_voiceprint(data_dir: Path, plan: dict, old: str, new: str) -> None:
    from . import speakers
    import numpy as np
    if not plan["voiceprint"]:
        return
    vdir = speakers.voiceprint_dir(data_dir)
    vp, stem, target = plan["vp"], plan["stem"], plan["target"]
    with speakers.voiceprint_lock(vdir):
        if plan["merged"]:
            have = speakers._read(vdir, target)
            known = set(have.ids)
            keep = [i for i, cid in enumerate(vp.ids) if cid not in known]
            vp = speakers.Voiceprint(
                have.legacy_sum + vp.legacy_sum, have.legacy + vp.legacy, have.ids + [vp.ids[i] for i in keep],
                np.concatenate([have.embs, vp.embs[keep]]) if keep else have.embs)
        speakers._write(vdir, target, vp)
        if stem != target:
            speakers._write(vdir, stem, None)


# ---------------------------------------------------------------- database

def db_counts(data_dir: Path, old: str) -> dict:
    path = Path(data_dir) / "pages.db"
    counts: dict[str, int] = {}
    if not path.exists():
        return counts
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, col in DB_COLUMNS:
            if table in tables and col in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
                counts[f"{table}.{col}"] = con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE lower({col}) = lower(?)", (old,)).fetchone()[0]
    finally:
        con.close()
    return counts


def begin_db(data_dir: Path, old: str, new: str, counts: dict):
    """The open connection with the renames done but not committed (None without a database)."""
    path = Path(data_dir) / "pages.db"
    if not path.exists():
        return None
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        for key, n in counts.items():
            table, col = key.split(".")
            if not n:
                continue
            if table == "enrollments":   # UNIQUE (device, start_ms, end_ms, name): an existing NEW row wins
                con.execute("DELETE FROM enrollments WHERE lower(name) = lower(?) AND EXISTS (SELECT 1 FROM enrollments e "
                            "WHERE e.device = enrollments.device AND e.start_ms = enrollments.start_ms "
                            "AND e.end_ms = enrollments.end_ms AND lower(e.name) = lower(?))", (old, new))
            con.execute(f"UPDATE {table} SET {col} = ? WHERE lower({col}) = lower(?)", (new, old))
    except BaseException:
        con.close()
        raise
    return con


# ---------------------------------------------------------------- notes

def _label_re(old: str) -> re.Pattern:
    """A paragraph's speaker label: `[HH:MM] ` (optionally starred) then `**OLD:**`."""
    return re.compile(rf"(?m)^([ \t]*\[\d{{1,2}}:\d{{2}}\]\s*(?:⭐\s*)?)\*\*{re.escape(old)}:\*\*", re.I)


def plan_notes(notes_dir: Path, old: str, new: str) -> dict:
    """Reads every note now (an unreadable or read-only one fails before anything is written)."""
    rx = _label_re(old)
    changes, scanned, labels = [], 0, 0
    for f in sorted(Path(notes_dir).glob("*.md")) if Path(notes_dir).is_dir() else []:
        scanned += 1
        out, n = rx.subn(lambda m: f"{m[1]}**{new}:**", f.read_text())
        if n:
            if not os.access(f, os.W_OK) or not os.access(f.parent, os.W_OK):
                raise Refused(f"{f.name} is not writable")
            changes.append((f, out))
            labels += n
    return {"notes_scanned": scanned, "notes_changed": len(changes), "labels": labels, "changes": changes}


def apply_notes(data_dir: Path, plan: dict, backup: Path) -> None:
    for f, out in plan["changes"]:
        backup.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, backup / f.name)
        tmp = f.with_name(f".{f.name}.tmp")
        tmp.write_text(out)
        os.replace(tmp, f)


# ---------------------------------------------------------------- voice library

def plan_library(data_dir: Path, old: str, new: str) -> dict:
    root = Path(data_dir) / "voice_library"
    dirs = [d for d in root.iterdir() if d.is_dir()] if root.is_dir() else []
    src = next((d for d in dirs if _same(d.name, old)), None)
    if src is None:
        return {"voice_library_clips": 0, "src": None}
    clips = [f for f in src.iterdir() if f.is_file() and f.name != "index.json" and not f.name.startswith(".")]
    dst = next((d for d in dirs if _same(d.name, new) and d != src), None)
    return {"voice_library_clips": len(clips), "src": src, "dst": dst, "root": root}


def _index(d: Path) -> list:
    try:
        x = json.loads((d / "index.json").read_text())
        return x if isinstance(x, list) else []
    except (OSError, ValueError):
        return []


def apply_library(data_dir: Path, plan: dict, old: str, new: str) -> None:
    """Move or merge the folder holding voice_library's own lock (voice_library.py)."""
    if plan["src"] is None:
        return
    import fcntl
    with open(plan["root"] / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            _apply_library(plan, new)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _apply_library(plan: dict, new: str) -> None:
    src = plan["src"]
    root = plan["root"]
    if plan["dst"] is None:                       # nobody there yet: move the folder as it is
        os.replace(src, root / new)
        return
    dst = plan["dst"]
    merged = _index(dst)
    dst_ids = {e.get("id") for e in merged if isinstance(e, dict)}
    src_entries = [e for e in _index(src) if isinstance(e, dict)]

    def fresh(stem: str) -> str:
        while True:
            cand = f"{stem}-{uuid.uuid4().hex[:8]}"
            if cand not in dst_ids and not list(dst.glob(f"{cand}.*")):
                return cand
    moved: dict[str, str] = {}                    # old id -> id it has now
    dropped: set[str] = set()                     # identical to a clip NEW already has
    for f in sorted(p for p in src.iterdir() if p.is_file() and p.name != "index.json" and not p.name.startswith(".")):
        target = dst / f.name
        if target.exists() and target.read_bytes() == f.read_bytes():
            dropped.add(f.stem)                   # the same clip: nothing is lost by keeping NEW's
            f.unlink()
        elif target.exists():
            nid = fresh(f.stem)
            os.replace(f, dst / f"{nid}{f.suffix}")
            moved[f.stem] = nid
        else:
            os.replace(f, target)
            moved[f.stem] = f.stem
    for e in src_entries:
        eid = e.get("id")
        if eid in dropped:
            continue
        nid = moved.get(eid, eid)
        if nid in dst_ids:
            nid = fresh(str(nid))
        e = {**e, "id": nid}
        dst_ids.add(nid)
        merged.append(e)
    if merged:
        tmp = dst / ".index.json.tmp"
        tmp.write_text(json.dumps(merged, indent=1))
        os.replace(tmp, dst / "index.json")
    shutil.rmtree(src, ignore_errors=True)


# ---------------------------------------------------------------- snapshots

class _Snapshot:
    """Copies of everything a rename touches outside the database, so a failure can put it back."""

    def __init__(self, data_dir: Path, old: str, new: str, vp: dict, lib: dict, notes: dict, root: Path):
        from . import speakers
        self.data, self.root = Path(data_dir), root
        self.vdir = speakers.voiceprint_dir(data_dir)
        self.vfiles = [self.vdir / f.name for stem in {vp.get("stem"), vp.get("target"), old, new} if stem
                       for f in _vp_files(self.vdir, stem)]
        self.vstems = {s for s in (vp.get("stem"), vp.get("target"), old, new) if s}
        self.cfg = self.data / "config.json"
        self.libdirs = [d for d in (lib.get("src"), lib.get("dst")) if d]
        self.libroot = lib.get("root")
        self.libnew = (self.libroot / new) if self.libroot else None
        self.notes = [f for f, _ in notes["changes"]]
        root.mkdir(parents=True, exist_ok=True)
        for f in self.vfiles:
            (root / "voiceprints").mkdir(exist_ok=True)
            shutil.copy2(f, root / "voiceprints" / f.name)
        if self.cfg.exists():
            shutil.copy2(self.cfg, root / "config.json")
        for d in self.libdirs:
            shutil.copytree(d, root / "library" / d.name)
        for f in self.notes:
            (root / "notes").mkdir(exist_ok=True)
            shutil.copy2(f, root / "notes" / f.name)

    def restore(self) -> None:
        for stem in self.vstems:
            for f in _vp_files(self.vdir, stem):
                f.unlink()
        for f in (self.root / "voiceprints").glob("*") if (self.root / "voiceprints").exists() else []:
            shutil.copy2(f, self.vdir / f.name)
        if (self.root / "config.json").exists():
            shutil.copy2(self.root / "config.json", self.cfg)
        else:
            self.cfg.unlink(missing_ok=True)
        if self.libroot:
            for d in {*self.libdirs, self.libnew}:
                if d and d.exists():
                    shutil.rmtree(d)
            for d in (self.root / "library").glob("*") if (self.root / "library").exists() else []:
                shutil.copytree(d, self.libroot / d.name)
        for f in self.notes:
            shutil.copy2(self.root / "notes" / f.name, f)


# ---------------------------------------------------------------- run

def run(data_dir: Path, notes_dir: Path, old: str, new: str, apply: bool = False, out=sys.stdout,
        lock_wait_s: float = 30.0) -> dict:
    """Counts per place. Raises Refused for a bad rename; with `apply` a failure at any stage
    restores everything and re-raises."""
    old, new = " ".join(old.split()), " ".join(new.split())
    if not config._NAME_RE.match(new) or not config._NAME_RE.match(old):
        raise Refused("names are letters, spaces, ' and - (1-40 characters)")
    if old == new:
        raise Refused("the names are the same")
    data_dir = Path(data_dir)
    from . import trash
    try:
        lock = trash.processor_lock(data_dir, wait_s=lock_wait_s)
        lock.__enter__()
    except trash.TrashError as e:
        raise Refused(f"the processor is busy ({e})") from None
    try:
        counts = _run_locked(data_dir, Path(notes_dir), old, new, apply)
    finally:
        lock.__exit__(None, None, None)
    print(f"{'renamed' if apply else 'dry run, nothing changed:'} {old} -> {new}", file=out)
    for k, v in counts.items():
        print(f"  {k}: {v}", file=out)
    if not apply:
        print("run again with --apply to rename", file=out)
    return counts


def _run_locked(data_dir: Path, notes_dir: Path, old: str, new: str, apply: bool) -> dict:
    # 1. read and check everything; nothing is written
    check_links(data_dir, old, new)
    new_raw, cfg_counts = plan_config(data_dir, old, new)
    vp = plan_voiceprint(data_dir, old, new)
    dbc = db_counts(data_dir, old)
    notes = plan_notes(notes_dir, old, new)
    lib = plan_library(data_dir, old, new)
    counts: dict = {k: vp[k] for k in ("voiceprint", "samples", "merged")}
    counts.update({f"db {k}": v for k, v in dbc.items()})
    counts.update({k: notes[k] for k in ("notes_scanned", "notes_changed", "labels")})
    counts["voice_library_clips"] = lib["voice_library_clips"]
    counts.update({f"config {k}": v for k, v in cfg_counts.items()})
    if not apply:
        return counts
    # 2. snapshot, then write: database in one open transaction, committed last
    root = data_dir / "backups" / f"rename-speaker-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    snap = _Snapshot(data_dir, old, new, vp, lib, notes, root / "snapshot")
    con = None
    try:
        con = begin_db(data_dir, old, new, dbc)
        apply_voiceprint(data_dir, vp, old, new)
        apply_notes(data_dir, notes, root)
        apply_library(data_dir, lib, old, new)
        apply_config(data_dir, new_raw)
        if con is not None:
            con.execute("COMMIT")
        shutil.rmtree(root / "snapshot", ignore_errors=True)
    except BaseException:
        if con is not None:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        snap.restore()
        raise
    finally:
        if con is not None:
            con.close()
    return counts


def apply_config(data_dir: Path, new_raw: dict | None) -> None:
    if new_raw is not None:
        config._write_raw(Path(data_dir) / "config.json", new_raw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--apply", action="store_true", help="make the change (default: dry run)")
    ap.add_argument("--data", type=Path, default=Path(os.environ.get("PENDANT_DATA", Path.home() / "pendant-direct-data")))
    ap.add_argument("--notes", type=Path, default=None)
    args = ap.parse_args(argv)
    if args.notes is None:
        args.notes = config.notes_dir(args.data.expanduser())
    try:
        run(args.data.expanduser(), args.notes.expanduser(), args.old, args.new, args.apply)
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # unreadable config, locked database
        print(f"rename_speaker failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
