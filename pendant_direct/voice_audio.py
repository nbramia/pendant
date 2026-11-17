"""The whole voice of every person the wearer names by hand, kept for good (retention never touches it).

    <data>/voice_audio/<device>_<start_ms>/<Name>.opus   that person's speech in that conversation, joined
    <data>/voice_audio/<device>_<start_ms>/<Name>.json   {version, person, device, start_ms, day, clip, tags,
                                                          cluster_ids, other_ids, segments, file_segments,
                                                          speech_s, bytes, kept_at}

A conversation's voice is kept when the wearer named it himself (`labeled_by = 'user'`, a person, not
`mixed`, not noise) and the name is in `speakers.allowed` (the consent gate). What is kept is every line
of the conversation's Raw section that shows that name (the voice's tag), cut from the conversation clip at
the line's offsets (`conversations.paragraph_offsets`) and joined in time order: the speech, not the whole
conversation. Several hand-named voices of one person in one conversation (a split voice sharing a name)
give one file. `segments` are the lines' spans in the clip (seconds); `file_segments` the same spans in the
kept file, so it can be re-embedded line by line. `cluster_ids` are the hand-named voices it covers,
`other_ids` voices named another way that show the same tag (their lines can't be told apart).

`sync_conversation` brings one conversation's files in line with the database and its note: a file goes as
soon as no hand-named voice of that person is left there (a name removed, a voice moved to someone else, a
voice marked noise), and is written or re-cut when the person's lines change. While the note can't be
read, or a relabel for the conversation is still queued, existing files stay as they are. After the clip
is gone a file can only lose lines (re-cut from itself), never gain them. The feed syncs after each
answer and reassignment, the processor every run before retention, and retention before each pass.
Deleting a conversation moves its folder into the trash copy (trash.py), so it goes for good with the
trash entry and comes back on restore.

    python -m pendant_direct.voice_audio [--backfill [--dry-run]] [--data DIR] [--notes DIR]
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import sqlite3
import struct
import sys
import time
from pathlib import Path

from . import config as cfgmod
from . import voice_library as vl
from .oggopus import OggOpusWriter

DIR = "voice_audio"
VERSION = 1
FRAME_S = vl.FRAME_S
OGG_HEADERS = 143           # bytes of an Ogg Opus file besides its packet pages (OpusHead, OpusTags, end)
MIN_SPEECH_S = 1.0
# reasons a hand-named voice wasn't kept that can clear by the next pass: retention holds that conversation's audio
RETRY = ("relabel queued", "note unreadable", "could not cut", "replacement not written", "sync failed")          # less than this of a person's lines is not kept
LINE_RE = re.compile(r"^\[\d{1,2}:\d{2}\] (?:⭐ )?(?:\*\*([^*\n]{1,40}):\*\* )?(.*)$")
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def root(data: Path) -> Path:
    return Path(data) / DIR


def conv_dir(data: Path, device: str, start_ms: int) -> Path:
    return root(data) / f"{_SAFE.sub('-', str(device))}_{int(start_ms)}"


def stem(person: str) -> str:
    return _SAFE.sub("-", person)


@contextlib.contextmanager
def _lock(data: Path):
    root(data).mkdir(parents=True, exist_ok=True)
    with open(root(data) / ".lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def load(d: Path) -> dict[str, dict]:
    """{person: metadata} of one conversation folder."""
    out = {}
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            m = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and m.get("person") and (d / f"{f.stem}.opus").is_file():
            out[m["person"]] = m
    return out


def entries(data: Path) -> list[dict]:
    """Every kept voice: its metadata plus `path` (the Opus file)."""
    out = []
    r = root(data)
    if not r.is_dir():
        return out
    for d in sorted(p for p in r.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for person, m in load(d).items():
            out.append({**m, "path": str(d / f"{stem(person)}.opus")})
    return out


def summary(data: Path) -> dict:
    """{voices, conversations, people, bytes, speech_s} of what is kept now."""
    es = entries(data)
    return {"voices": len(es), "conversations": len({(e["device"], e["start_ms"]) for e in es}),
            "people": len({e["person"] for e in es}),
            "bytes": sum(Path(e["path"]).stat().st_size for e in es if Path(e["path"]).is_file()),
            "speech_s": round(sum(float(e.get("speech_s") or 0) for e in es), 1)}


# ---------------------------------------------------------------------------
# what a conversation should keep

def hand_voices(con: sqlite3.Connection, cfg: dict, device: str, start_ms: int) -> dict[str, list[int]]:
    """{person: [cluster id]}: the conversation's voices the wearer named himself, with an allowed name."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(speaker_clusters)")}
    mixed = "COALESCE(mixed, 0)" if "mixed" in cols else "0"
    kind = "label_kind" if "label_kind" in cols else "NULL"
    out: dict[str, list[int]] = {}
    for cid, label in con.execute(
            f"SELECT id, label FROM speaker_clusters WHERE device = ? AND start_ms = ? AND labeled_by = 'user' "
            f"AND label IS NOT NULL AND COALESCE({kind}, 'person') = 'person' AND {mixed} = 0 "
            "AND COALESCE(noise, 0) = 0 ORDER BY id", (device, start_ms)):
        who = vl.eligible(cfg, label)
        if who:
            out.setdefault(who, []).append(cid)
    return out


def _pending(con: sqlite3.Connection, device: str, start_ms: int) -> bool:
    try:
        return bool(con.execute("SELECT 1 FROM transcript_relabels WHERE device = ? AND start_ms = ? "
                                "AND done_at IS NULL LIMIT 1", (device, start_ms)).fetchone())
    except sqlite3.OperationalError:
        return False


def _merge(spans: list[list[float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for a, b in sorted(spans):
        if b - a <= 0:
            continue
        if out and a < out[-1][1]:        # overlapping; lines that only touch stay apart
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def wanted(con: sqlite3.Connection, notes: Path, cfg: dict, device: str, start_ms: int,
           texts: dict | None = None) -> tuple[dict[str, list[int]], dict[str, dict] | None, str | None]:
    """(hand voices {person: ids}, specs {person: {tags, cluster_ids, other_ids, segments, speech_s}} or None
    when the lines can't be known now, why not)."""
    from .process import section_bounds
    hand = hand_voices(con, cfg, device, start_ms)
    if not hand:
        return hand, {}, None
    conv = con.execute("SELECT day, raw_heading, paragraph_offsets FROM conversations WHERE device = ? AND "
                       "start_ms = ?", (device, start_ms)).fetchone()
    if conv is None:
        return hand, None, "no conversation"
    day, heading, offsets = conv
    if _pending(con, device, start_ms):
        return hand, None, "relabel queued"
    try:
        offsets = json.loads(offsets) if offsets else None
    except ValueError:
        offsets = None
    if not offsets:
        return hand, None, "no line offsets"
    texts = {} if texts is None else texts
    daily = Path(notes) / f"{day}.md"
    if day not in texts:
        try:
            texts[day] = daily.read_text()
        except OSError:
            texts[day] = None
    if texts[day] is None or not heading:
        return hand, None, "note unreadable"
    lines = texts[day].split("\n")
    b = section_bounds(lines, heading)
    if not b:
        return hand, None, "note unreadable"
    paras = [m for m in (LINE_RE.match(x) for x in lines[b[0]:b[1]]) if m]
    if len(paras) != len(offsets):
        return hand, None, "lines and offsets differ"
    voices = con.execute("SELECT id, tag, labeled_by FROM speaker_clusters WHERE device = ? AND start_ms = ?",
                         (device, start_ms)).fetchall()
    untagged = not any(m[1] for m in paras)
    specs: dict[str, dict] = {}
    for person, ids in hand.items():
        tags = {t for i, t, _b in voices if i in ids and t}
        low = {t.lower() for t in tags}
        if untagged and len(voices) == 1:
            spans = [o for o in offsets if o]
        else:
            spans = [o for m, o in zip(paras, offsets) if o and m[1] and m[1].lower() in low]
        segs = [[round(float(a), 2), round(float(b_), 2)] for a, b_ in _merge([[float(o[0]), float(o[1])]
                                                                              for o in spans])]
        speech = sum(b_ - a for a, b_ in segs)
        if speech < MIN_SPEECH_S:
            continue
        others = sorted(i for i, t, _b in voices if i not in ids and t and t.lower() in low)
        specs[person] = {"tags": sorted(tags), "cluster_ids": sorted(ids), "other_ids": others,
                         "segments": segs, "speech_s": round(speech, 2)}
    return hand, specs, None


# ---------------------------------------------------------------------------
# writing

def _cut(packets: list[bytes], spans: list[list[float]], out: Path) -> list[list[float]]:
    """Write the packets of each span (seconds into `packets`), joined; returns each span's place in `out`."""
    tmp = out.with_name(f".{out.name}.tmp")
    w = OggOpusWriter(tmp)
    pos, placed = 0, []
    try:
        for a, b in spans:
            i, j = max(0, int(round(a / FRAME_S))), min(len(packets), int(round(b / FRAME_S)))
            part = [p for p in packets[i:j] if p]
            for p in part:
                w.add_packet(p)
            placed.append([round(pos * FRAME_S, 2), round((pos + len(part)) * FRAME_S, 2)])
            pos += len(part)
    finally:
        w.close()
    if not pos:
        tmp.unlink(missing_ok=True)
        raise ValueError("empty")
    os.replace(tmp, out)
    return placed


def _source(data: Path, clip: str | None, old: dict | None, d: Path, segs: list):
    """(packets, spans into them, the segments those spans are) to cut `segs` from: the conversation clip,
    else the kept file itself (only the segments it already holds); None when there is no audio."""
    f = Path(data) / "clips" / Path(clip or "").name
    if clip and f.is_file():
        return vl.read_packets(f), segs, segs
    if old:
        have = {tuple(s): fs for s, fs in zip(old["segments"], old["file_segments"])}
        keep = [s for s in segs if tuple(s) in have]
        if keep:
            return vl.read_packets(d / f"{stem(old['person'])}.opus"), [list(have[tuple(s)]) for s in keep], keep
    return None


def _write_meta(path: Path, m: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(m, indent=1) + "\n")
    os.replace(tmp, path)


def _remove(d: Path, person: str) -> None:
    (d / f"{stem(person)}.opus").unlink(missing_ok=True)
    (d / f"{stem(person)}.json").unlink(missing_ok=True)


def sync_conversation(con: sqlite3.Connection, data: Path, notes: Path, cfg: dict, device: str, start_ms: int,
                      dry_run: bool = False, texts: dict | None = None) -> dict:
    """Bring one conversation's kept voices in line. Returns {kept, recut, updated, removed, bytes, skipped:
    {why: n}, held}; with dry_run nothing is written and `bytes` is what would be written. `held`: a hand-named
    voice wasn't kept for a reason that can clear (RETRY), so its conversation's audio must stay for now. A
    file whose voices moved to someone else goes only once that person's file is written."""
    rep = {"kept": 0, "recut": 0, "updated": 0, "removed": 0, "unchanged": 0, "bytes": 0, "skipped": {},
           "held": False}
    d = conv_dir(data, device, start_ms)
    hand, specs, why = wanted(con, notes, cfg, device, start_ms, texts)
    have = load(d)
    if not hand and not have:
        return rep
    row = con.execute("SELECT day, clip FROM conversations WHERE device = ? AND start_ms = ?",
                      (device, start_ms)).fetchone()
    day, clip = row if row else (None, None)

    def skip(reason, n=1):
        rep["skipped"][reason] = rep["skipped"].get(reason, 0) + n
        rep["held"] = rep["held"] or reason.startswith(RETRY)

    done: set[str] = set()      # people whose file is in line after this pass

    ctx = contextlib.nullcontext() if dry_run else _lock(data)
    with ctx:
        have = load(d)
        stale = [p for p in have if p not in hand or (specs is not None and p not in specs)]
        if specs is None:
            skip(why, len(hand))
            rep["unchanged"] += sum(1 for p in hand if p in have)
        for person, spec in (specs or {}).items():
            old = have.get(person)
            if old is None:      # a voice renamed or moved here: its old file, by the voices it covers
                old = next((m for p, m in have.items() if p in stale
                            and set(m.get("cluster_ids") or ()) & set(spec["cluster_ids"])), None)
            fields = {k: spec[k] for k in ("tags", "cluster_ids", "other_ids")}
            if old and old["person"] == person and old["segments"] == spec["segments"]:
                if any(old.get(k) != v for k, v in fields.items()):
                    rep["updated"] += 1
                    if not dry_run:
                        _write_meta(d / f"{stem(person)}.json", {**old, **fields})
                else:
                    rep["unchanged"] += 1
                done.add(person)
                continue
            src = _source(data, clip, old, d, spec["segments"])
            if src is None:
                skip("no audio")
                continue
            packets, spans, segs = src
            if old and old["person"] == person and segs == old["segments"]:   # no clip: nothing to drop or add
                rep["unchanged"] += 1
                if not dry_run and any(old.get(k) != v for k, v in fields.items()):
                    _write_meta(d / f"{stem(person)}.json", {**old, **fields})
                done.add(person)
                continue
            if dry_run:    # Ogg pages of one packet each (the packet and a 28-byte header), plus the headers
                rep["bytes"] += OGG_HEADERS + sum(len(p) + 28 for a, b in spans
                                    for p in packets[max(0, int(round(a / FRAME_S))):int(round(b / FRAME_S))] if p)
                rep["kept" if old is None else "recut"] += 1
                done.add(person)
                continue
            d.mkdir(parents=True, exist_ok=True)
            out = d / f"{stem(person)}.opus"
            try:
                placed = _cut(packets, spans, out)
            except (OSError, ValueError, struct.error) as e:
                skip(f"could not cut ({type(e).__name__})")
                continue
            m = {"version": VERSION, "person": person, "device": device, "start_ms": start_ms, "day": day,
                 "clip": Path(clip).name if clip else None, **fields, "segments": segs, "file_segments": placed,
                 "speech_s": round(sum(b - a for a, b in placed), 2), "bytes": out.stat().st_size,
                 "kept_at": time.time()}
            _write_meta(d / f"{stem(person)}.json", m)
            rep["bytes"] += m["bytes"]
            rep["kept" if old is None else "recut"] += 1
            done.add(person)
        targets = specs if specs is not None else {p: {"cluster_ids": ids} for p, ids in hand.items()}
        for person in stale:
            heirs = [q for q, t in targets.items() if q != person
                     and set(t["cluster_ids"]) & set(have[person].get("cluster_ids") or ())]
            if any(q not in done for q in heirs):     # its voice moved on, but the new file isn't written
                skip("replacement not written")
                continue
            rep["removed"] += 1
            if not dry_run:
                _remove(d, person)
        if not dry_run:
            with contextlib.suppress(OSError):
                d.rmdir()          # an empty folder goes
    return rep


def hand_conversations(con: sqlite3.Connection) -> set[tuple[str, int]]:
    """Conversations with a voice named by hand (what retention holds when the whole sync fails)."""
    return set(con.execute("SELECT DISTINCT device, start_ms FROM speaker_clusters WHERE labeled_by = 'user' "
                           "AND label IS NOT NULL").fetchall())


def conversations(con: sqlite3.Connection, data: Path) -> list[tuple[str, int]]:
    """Conversations to sync: those with a voice named by hand, and those with kept voices."""
    keys = set(con.execute("SELECT DISTINCT s.device, s.start_ms FROM speaker_clusters s JOIN conversations c "
                           "ON c.device = s.device AND c.start_ms = s.start_ms WHERE s.labeled_by = 'user' "
                           "AND s.label IS NOT NULL").fetchall())
    live = set(con.execute("SELECT device, start_ms FROM conversations").fetchall())
    for e in entries(data):
        if (e["device"], e["start_ms"]) in live:
            keys.add((e["device"], e["start_ms"]))
    return sorted(keys, key=lambda k: (k[1], k[0]))


def sync_all(con: sqlite3.Connection, data: Path, notes: Path, cfg: dict, dry_run: bool = False,
             log=None) -> dict:
    """sync_conversation over every conversation that has (or had) a hand-named voice: the backfill, and
    the processor's pass. Kept files of conversations no longer in the database are left alone. `held`:
    [device, start_ms] of the conversations whose audio retention must leave for the next pass."""
    total = {"conversations": 0, "kept": 0, "recut": 0, "updated": 0, "removed": 0, "unchanged": 0, "bytes": 0,
             "skipped": {}, "held": []}
    texts: dict = {}
    for device, start_ms in conversations(con, data):
        try:
            r = sync_conversation(con, data, notes, cfg, device, start_ms, dry_run=dry_run, texts=texts)
        except Exception as e:  # noqa: BLE001  one conversation never stops the others
            r = {"kept": 0, "recut": 0, "updated": 0, "removed": 0, "unchanged": 0, "bytes": 0,
                 "skipped": {f"sync failed ({type(e).__name__})": 1}, "held": True}
        if r["held"]:
            total["held"].append([device, start_ms])
        total["conversations"] += 1
        for k in ("kept", "recut", "updated", "removed", "unchanged", "bytes"):
            total[k] += r[k]
        for k, v in r["skipped"].items():
            total["skipped"][k] = total["skipped"].get(k, 0) + v
    if log and not dry_run and (total["kept"] or total["recut"] or total["removed"]):
        log(f"voice audio: kept {total['kept']}, re-cut {total['recut']}, removed {total['removed']} "
            f"({total['bytes']} bytes written)")
    return total


# ---------------------------------------------------------------------------
# trash (trash.py moves a conversation's folder with it)

def copy_to(data: Path, device: str, start_ms: int, work: Path) -> list[str]:
    """Copy this conversation's kept voices into a trash copy (`work/voice_audio/`); the file names copied."""
    d = conv_dir(data, device, start_ms)
    names = sorted(p.name for p in d.iterdir() if p.is_file() and not p.name.startswith(".")) if d.is_dir() else []
    if names:
        (work / DIR).mkdir(parents=True, exist_ok=True)
        for n in names:
            shutil.copy2(d / n, work / DIR / n)
    return names


def drop_live(data: Path, device: str, start_ms: int, work: Path, names: list[str]) -> None:
    """After a delete's commit: the live files go (only those the trash copy holds)."""
    d = conv_dir(data, device, start_ms)
    for n in names:
        if (work / DIR / n).exists():
            (d / n).unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        d.rmdir()


def put_back(data: Path, device: str, start_ms: int, work: Path, names: list[str],
             ids: dict[int, int] | None = None) -> list[Path]:
    """Restore: the files go back (a file that exists stays), metadata following restored voice ids."""
    d = conv_dir(data, device, start_ms)
    placed = []
    for n in names:
        src, live = work / DIR / n, d / n
        if not src.exists() or live.exists():
            continue
        d.mkdir(parents=True, exist_ok=True)
        if n.endswith(".json") and ids:
            m = json.loads(src.read_text())
            for k in ("cluster_ids", "other_ids"):
                m[k] = sorted(ids.get(i, i) for i in m.get(k) or [])
            _write_meta(live, m)
        else:
            shutil.copy2(src, live)
        placed.append(live)
    return placed


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pendant_direct.voice_audio")
    ap.add_argument("--data", type=Path, default=Path.home() / "pendant-direct-data")
    ap.add_argument("--notes", type=Path, help="Raw notes folder (default: the configured one; read only)")
    ap.add_argument("--backfill", action="store_true",
                    help="keep the voices named by hand whose conversation audio is still there (idempotent)")
    ap.add_argument("--dry-run", action="store_true", help="with --backfill: report counts and bytes only")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    data = a.data.expanduser()
    cfg = cfgmod.load(data)
    notes = (a.notes or cfgmod.notes_dir(data)).expanduser()
    out: dict = {}
    if a.backfill:
        con = sqlite3.connect(f"file:{data / 'pages.db'}?mode=ro", uri=True, timeout=30)
        try:
            out["backfill"] = sync_all(con, data, notes, cfg, dry_run=a.dry_run)
        finally:
            con.close()
    out["kept"] = summary(data)
    if a.json:
        print(json.dumps(out, indent=1))
        return 0
    if a.backfill:
        r = out["backfill"]
        verb = "would keep" if a.dry_run else "kept"
        print(f"{verb} {r['kept']} voices and re-cut {r['recut']} ({r['bytes']} bytes, {r['bytes'] / 1e6:.2f} MB) "
              f"in {r['conversations']} conversations; {r['unchanged']} already kept, {r['updated']} metadata "
              f"updates, {r['removed']} removed")
        for why, n in sorted(r["skipped"].items()):
            print(f"  not kept ({why}): {n}")
    k = out["kept"]
    print(f"voice audio kept: {k['voices']} voices of {k['people']} people in {k['conversations']} conversations, "
          f"{k['bytes'] / 1e6:.2f} MB, {k['speech_s'] / 60:.1f} min of speech")
    return 0


if __name__ == "__main__":
    sys.exit(main())
