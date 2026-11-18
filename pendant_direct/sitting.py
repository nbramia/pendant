"""The sitting: a real conversation the processor stored as several pieces.

The processor cuts a stretch at MAX_CONV_S and splits at silences over GAP_S, so an hour with
one person can be four conversations. Naming a voice for "this conversation" (the reassign
sheet's default scope, a Who's this or Voice Scoring answer) covers the sitting: the tapped
piece plus the neighbouring pieces of the same real conversation that hold the same voice.

- Two consecutive pieces of one pendant are **linked** when the second continues the first's
  audio (`process.cap_cut`: back to back, or the length cap cutting at a short pause), when the
  gap between them is at most `speakers.sitting_gap_s` (180 s; overlapping pieces count), or
  when both carry the same calendar event (`process._same_event`). A piece without an end time
  is linked to nothing.
- From the tapped piece the sitting walks out both ways over linked neighbours while each
  neighbour holds a voice that **matches** the tapped one: a cosine of at least
  `speakers.sitting_floor` (0.7; null turns the sitting off) between the voices' embeddings.
  The first neighbour without one ends the sitting on that side, so a morning recorded
  without a break isn't one sitting.
- In each other piece of the sitting, every matching voice is named, best first, except the
  wearer's voice (`speakers.wearer`) and noise, which never count, and voices answered by hand
  (a name, Remove name or dismissed), mixed voices (two people) and voices whose tag another
  voice of their piece shares (their lines can't be told apart), which count as the person
  being there but keep their names. An automatic name (match, meeting, calendar) may be
  replaced. The tapped voice's own piece is left as the caller changes it, and a mixed tapped
  voice gets no sitting at all.

`plan` is pure (rows in, decision out); `load` reads the rows for one voice. The feed applies the
plan (each named voice as a hand label) and records it as one undo. Read-only dry run:
`python -m pendant_direct.sitting CLUSTER_ID [--data DIR] [--floor F] [--gap S]` prints the
pieces covered, the voices it would name with their similarities, and the voices skipped and
why, as counts and similarities only.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

GAP_S = 180          # pieces this close (or overlapping) belong to one sitting
FLOOR = 0.7          # cosine between the tapped voice and a voice in another piece to name it
WINDOW_MS = 12 * 3600 * 1000   # how far around the tapped piece pieces are read

# why a voice at or above the floor keeps its name; `NOT_THERE` ones don't show the person is there
NOT_THERE = ("wearer", "noise")
KEEPS = ("hand", "mixed", "shared")


def settings(sp: dict) -> tuple[float | None, float]:
    """(floor or None when off, gap seconds) from the `speakers` config."""
    floor = sp.get("sitting_floor", FLOOR)
    try:
        floor = None if floor is None else float(floor)
    except (TypeError, ValueError):
        floor = FLOOR
    try:
        gap = float(sp.get("sitting_gap_s", GAP_S))
    except (TypeError, ValueError):
        gap = GAP_S
    return floor, max(0.0, gap)


def linked(a: dict, b: dict, gap_s: float = GAP_S) -> bool:
    """Whether piece `b` (starting after `a`) belongs to `a`'s sitting by time or calendar."""
    from .process import _same_event, cap_cut
    if a["device"] != b["device"] or a.get("end_ms") is None or b.get("end_ms") is None:
        return False
    if b["start_ms"] - a["end_ms"] <= gap_s * 1000 or cap_cut(a, b):
        return True
    return _same_event(_event(a), _event(b))


def _event(piece: dict) -> dict | None:
    ev = piece.get("calendar_event")
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except ValueError:
            return None
    return ev if isinstance(ev, dict) else None


def chain(pieces: list[dict], at: int, gap_s: float = GAP_S) -> tuple[int, int]:
    """(first, last) index of the run of linked pieces around `pieces[at]` (sorted by start)."""
    lo = hi = at
    while lo > 0 and linked(pieces[lo - 1], pieces[lo], gap_s):
        lo -= 1
    while hi + 1 < len(pieces) and linked(pieces[hi], pieces[hi + 1], gap_s):
        hi += 1
    return lo, hi


def _unit(v):
    import numpy as np
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    return v / max(float(np.linalg.norm(v)), 1e-9)


def _reason(v: dict, wearer: str, shared: bool) -> str | None:
    """Why a voice keeps its name (None: it may be named)."""
    if v.get("noise") or v.get("label_kind") == "noise":
        return "noise"
    if wearer and wearer.lower() in {str(v.get("label") or "").lower(), str(v.get("tag") or "").lower()}:
        return "wearer"
    if v.get("labeled_by") in ("user", "dismissed"):
        return "hand"
    if v.get("mixed"):
        return "mixed"
    if shared:
        return "shared"
    return None


def judge(ref, piece: dict, floor: float, wearer: str) -> dict:
    """One other piece: its voices at or above the floor named or kept (with the reason), the
    rest below it; `present` when a voice there shows the person is there."""
    from collections import Counter
    tags = Counter(v.get("tag") for v in piece["voices"] if v.get("tag"))
    named, kept, below = [], [], []
    for v in piece["voices"]:
        if v.get("embedding") is None or len(v["embedding"]) != len(ref):
            continue
        sim = round(float(_unit(v["embedding"]) @ ref), 4)
        entry = {"id": v["id"], "similarity": sim, "labeled_by": v.get("labeled_by")}
        if sim < floor:
            below.append(entry)
            continue
        why = _reason(v, wearer, bool(v.get("tag")) and tags[v.get("tag")] > 1)
        (named if why is None else kept).append(entry if why is None else {**entry, "reason": why})
    named.sort(key=lambda e: -e["similarity"])
    kept.sort(key=lambda e: -e["similarity"])
    below.sort(key=lambda e: -e["similarity"])
    return {"device": piece["device"], "start_ms": piece["start_ms"], "end_ms": piece.get("end_ms"),
            "named": named, "kept": kept, "below": below,
            "present": bool(named) or any(k["reason"] not in NOT_THERE for k in kept)}


def plan(tapped: dict, pieces: list[dict], floor: float | None, gap_s: float = GAP_S, wearer: str = "") -> dict:
    """The sitting of `tapped` (a voice: id, device, start_ms, embedding, mixed) among `pieces`
    (its pendant's conversations: device, start_ms, end_ms, calendar_event and `voices`, each with
    id, tag, label, labeled_by, label_kind, noise, mixed, embedding). Pure."""
    pieces = sorted(pieces, key=lambda p: p["start_ms"])
    at = next((i for i, p in enumerate(pieces) if p["device"] == tapped["device"]
               and p["start_ms"] == tapped["start_ms"]), None)
    own = pieces[at] if at is not None else {"device": tapped["device"], "start_ms": tapped["start_ms"],
                                             "end_ms": None}
    out = {"cluster": tapped["id"], "floor": floor, "gap_s": gap_s, "pieces": [], "named": [],
           "before": 0, "after": 0, "ended": {"before": None, "after": None}}
    covered = [{"device": own["device"], "start_ms": own["start_ms"], "end_ms": own.get("end_ms"),
                "tapped": True, "named": [], "kept": [], "below": []}]
    if at is not None and floor is not None and not tapped.get("mixed") and tapped.get("embedding") is not None:
        ref = _unit(tapped["embedding"])
        lo, hi = chain(pieces, at, gap_s)
        for side, idx in (("before", range(at - 1, lo - 1, -1)), ("after", range(at + 1, hi + 1))):
            for i in idx:
                j = judge(ref, pieces[i], floor, wearer)
                if not j["present"]:
                    out["ended"][side] = "no matching voice"
                    break
                covered.insert(0, j) if side == "before" else covered.append(j)
                if j["named"]:
                    out[side] += 1
                    out["named"] += [{**n, "device": j["device"], "start_ms": j["start_ms"]} for n in j["named"]]
            else:
                edge = lo == 0 if side == "before" else hi == len(pieces) - 1
                out["ended"][side] = "no more pieces" if edge else "not linked"
    out["pieces"] = covered
    out["parts"] = len(covered)
    ends = [p["end_ms"] for p in covered if p.get("end_ms") is not None]
    out["start_ms"] = covered[0]["start_ms"]
    out["end_ms"] = max(ends) if ends else None
    out["span_s"] = round((out["end_ms"] - out["start_ms"]) / 1000) if out["end_ms"] is not None else None
    return out


def summary(p: dict) -> dict:
    """What the API reports about a plan: the coverage and what else gets named, no ids of people."""
    return {"parts": p["parts"], "span_s": p["span_s"], "start_ms": p["start_ms"], "end_ms": p["end_ms"],
            "named": len(p["named"]), "named_parts": p["before"] + p["after"],
            "before": p["before"], "after": p["after"]}


VOICE_COLUMNS = ("id, device, start_ms, tag, label, labeled_by, label_kind, COALESCE(noise, 0), "
                 "COALESCE(mixed, 0), embedding")
VOICE_KEYS = ("id", "device", "start_ms", "tag", "label", "labeled_by", "label_kind", "noise", "mixed", "embedding")


def load(con: sqlite3.Connection, sp: dict, cid: int, floor: float | None = ..., gap_s: float | None = None) -> dict:
    """The plan for voice `cid` from the database (reads only). `floor` and `gap_s` default to
    the config's (`sp` is the `speakers` section). KeyError when there is no such voice."""
    import numpy as np

    from .config import wearer as wearer_name
    cfg_floor, cfg_gap = settings(sp)
    floor = cfg_floor if floor is ... else floor
    gap_s = cfg_gap if gap_s is None else gap_s
    row = con.execute(f"SELECT {VOICE_COLUMNS} FROM speaker_clusters WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise KeyError(cid)
    v = dict(zip(VOICE_KEYS, row))
    tapped = {"id": v["id"], "device": v["device"], "start_ms": v["start_ms"], "mixed": v["mixed"],
              "embedding": None if v["embedding"] is None else np.frombuffer(v["embedding"], dtype=np.float32)}
    convs = con.execute(
        "SELECT device, start_ms, end_ms, calendar_event FROM conversations WHERE device = ? "
        "AND start_ms BETWEEN ? AND ? ORDER BY start_ms",
        (v["device"], v["start_ms"] - WINDOW_MS, v["start_ms"] + WINDOW_MS)).fetchall()
    pieces = [{"device": d, "start_ms": s, "end_ms": e, "calendar_event": ev, "voices": []} for d, s, e, ev in convs]
    by_key = {(p["device"], p["start_ms"]): p for p in pieces}
    if floor is not None and len(pieces) > 1:
        for r in con.execute(f"SELECT {VOICE_COLUMNS} FROM speaker_clusters WHERE device = ? AND start_ms BETWEEN "
                             "? AND ? AND start_ms != ? ORDER BY id",
                             (v["device"], pieces[0]["start_ms"], pieces[-1]["start_ms"], v["start_ms"])):
            x = dict(zip(VOICE_KEYS, r))
            p = by_key.get((x["device"], x["start_ms"]))
            if p is not None:
                if x["embedding"] is not None:
                    x["embedding"] = np.frombuffer(x["embedding"], dtype=np.float32)
                p["voices"].append(x)
    return plan(tapped, pieces, floor, gap_s, wearer_name(sp))


def ids(p: dict) -> list[int]:
    """The voices a plan names in the other pieces."""
    return [n["id"] for n in p["named"]]


# ---------------------------------------------------------------------------
# dry run

def report(p: dict) -> str:
    """The plan as text: pieces, voices named and kept with their similarities; no names."""
    lines = [f"voice {p['cluster']}: floor {p['floor']}, gap {p['gap_s']:.0f} s",
             f"sitting: {p['parts']} part{'s' if p['parts'] != 1 else ''}"
             + (f", {p['span_s'] / 60:.0f} min" if p["span_s"] is not None else "")
             + f"; named in {p['before']} earlier and {p['after']} later part(s), {len(p['named'])} voice(s)"]
    for side in ("before", "after"):
        if p["ended"][side]:
            lines.append(f"  ends {side}: {p['ended'][side]}")
    t0 = p["start_ms"]
    for n, piece in enumerate(p["pieces"]):
        mins = (f"{(piece['end_ms'] - piece['start_ms']) / 60000:.0f} min" if piece.get("end_ms") is not None
                else "? min")
        head = f"  part {n + 1}: +{(piece['start_ms'] - t0) / 60000:.0f} min, {mins}"
        if piece.get("tapped"):
            lines.append(head + " (the tapped voice's part)")
            continue
        lines.append(head)
        for e in piece["named"]:
            lines.append(f"    name   voice {e['id']}  cos {e['similarity']:.3f}  (was {e['labeled_by'] or 'unnamed'})")
        for e in piece["kept"]:
            lines.append(f"    keep   voice {e['id']}  cos {e['similarity']:.3f}  ({e['reason']})")
        best = piece["below"][0] if piece["below"] else None
        if best:
            lines.append(f"    below  {len(piece['below'])} voice(s), best cos {best['similarity']:.3f}")
    return "\n".join(lines)


def evaluate(con: sqlite3.Connection, sp: dict, floors=(0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85),
             gap_s: float | None = None) -> dict:
    """Leave-the-labels-out check of the floor on the wearer's hand labels (reads only). Every voice
    named by hand is the tapped voice once; the other hand-named voices around it are hidden (as if
    unnamed, the wearer's own kept) and the sitting decides; each voice it names is `right` when the
    wearer named it the same person, `wrong` when he named it someone else, else `unverified`.
    `pairs` compares the tapped voice with every hand-named voice in linked pieces: the same person's
    cosines and different people's."""
    import numpy as np

    from .config import wearer as wearer_name
    _, cfg_gap = settings(sp)
    gap_s = cfg_gap if gap_s is None else gap_s
    wearer = wearer_name(sp)
    convs = {}
    for d, s, e, ev in con.execute("SELECT device, start_ms, end_ms, calendar_event FROM conversations "
                                   "ORDER BY device, start_ms"):
        convs.setdefault(d, []).append({"device": d, "start_ms": s, "end_ms": e, "calendar_event": ev, "voices": []})
    truth: dict[int, str] = {}
    labels: dict[int, str | None] = {}
    for r in con.execute(f"SELECT {VOICE_COLUMNS} FROM speaker_clusters ORDER BY id"):
        x = dict(zip(VOICE_KEYS, r))
        if x["embedding"] is None:
            continue
        x["embedding"] = np.frombuffer(x["embedding"], dtype=np.float32)
        p = next((p for p in convs.get(x["device"], []) if p["start_ms"] == x["start_ms"]), None)
        if p is None:
            continue
        labels[x["id"]] = x["label"]
        if x["labeled_by"] == "user" and x["label"] and x["label_kind"] in (None, "person"):
            truth[x["id"]] = x["label"]
            if x["label"].lower() != wearer.lower():
                x = {**x, "labeled_by": None, "label": None, "label_kind": None}   # hidden: as if unnamed
        p["voices"].append(x)
    out = {"tapped": 0, "pairs": {"same": [], "different": []}, "floors": {}}
    refs = []
    for pieces in convs.values():
        for i, p in enumerate(pieces):
            for v in p["voices"]:
                if v["id"] in truth and not v["mixed"] and truth[v["id"]].lower() != wearer.lower():
                    refs.append((pieces, i, v))
    out["tapped"] = len(refs)
    for pieces, i, v in refs:
        lo, hi = chain(pieces, i, gap_s)
        ref = _unit(v["embedding"])
        for k in range(lo, hi + 1):
            if k == i:
                continue
            for w in pieces[k]["voices"]:
                if w["id"] in truth and len(w["embedding"]) == len(ref):
                    same = truth[w["id"]].lower() == truth[v["id"]].lower()
                    out["pairs"]["same" if same else "different"].append(round(float(_unit(w["embedding"]) @ ref), 3))
    for f in floors:
        c = {"right": 0, "wrong": 0, "unverified": 0, "auto_same": 0, "auto_other": 0, "sittings": 0, "parts": 0}
        for pieces, i, v in refs:
            tapped = {"id": v["id"], "device": v["device"], "start_ms": v["start_ms"], "mixed": v["mixed"],
                      "embedding": v["embedding"]}
            pl = plan(tapped, pieces, f, gap_s, wearer)
            if pl["parts"] > 1:
                c["sittings"] += 1
                c["parts"] += pl["parts"]
            for n in pl["named"]:
                t = truth.get(n["id"])
                c["unverified" if t is None else "right" if t.lower() == truth[v["id"]].lower() else "wrong"] += 1
                if t is None and n["labeled_by"] in ("match", "meeting", "calendar"):
                    auto = labels.get(n["id"]) or ""
                    c["auto_same" if auto.lower() == truth[v["id"]].lower() else "auto_other"] += 1
        out["floors"][f] = c
    return out


def eval_report(ev: dict) -> str:
    import numpy as np
    lines = [f"hand-named voices tapped (the wearer aside): {ev['tapped']}"]
    for k in ("same", "different"):
        xs = np.asarray(ev["pairs"][k])
        if len(xs):
            lines.append(f"  {k} person in a linked piece: {len(xs)} pairs, cosine min {xs.min():.3f} "
                         f"p10 {np.percentile(xs, 10):.3f} median {np.median(xs):.3f} max {xs.max():.3f}")
        else:
            lines.append(f"  {k} person in a linked piece: 0 pairs")
    lines.append("  unverified: no hand label; of those, auto-named the same person / someone else")
    lines.append("  floor  right  wrong  unverified  auto_same  auto_other  sittings>1  parts")
    for f, c in ev["floors"].items():
        lines.append(f"  {f:5.2f}  {c['right']:5d}  {c['wrong']:5d}  {c['unverified']:10d}  {c['auto_same']:9d}  "
                     f"{c['auto_other']:10d}  {c['sittings']:10d}  {c['parts']:5d}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    from . import config as cfgmod
    ap = argparse.ArgumentParser(prog="python -m pendant_direct.sitting",
                                 description="What naming a voice for this conversation would cover (read-only).")
    ap.add_argument("cluster", type=int, nargs="?")
    ap.add_argument("--eval", action="store_true", help="check the floor against the hand labels")
    ap.add_argument("--data", type=Path, default=None)
    ap.add_argument("--floor", type=float, default=None)
    ap.add_argument("--gap", type=float, default=None)
    a = ap.parse_args(argv)
    data = a.data or cfgmod.data_dir()
    sp = cfgmod.load(data)["speakers"]
    con = sqlite3.connect(f"file:{Path(data) / 'pages.db'}?mode=ro", uri=True)
    if a.eval:
        try:
            print(eval_report(evaluate(con, sp, gap_s=a.gap)))
        finally:
            con.close()
        return
    if a.cluster is None:
        con.close()
        ap.error("give a cluster id, or --eval")
    try:
        p = load(con, sp, a.cluster, floor=a.floor if a.floor is not None else ..., gap_s=a.gap)
    except KeyError:
        raise SystemExit(f"no voice {a.cluster}") from None
    finally:
        con.close()
    print(report(p))


if __name__ == "__main__":
    main()
