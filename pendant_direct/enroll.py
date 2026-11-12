"""Record a voice sample: someone reads a set of sentences near the pendant while the
phone's microphone records the same reading, and the app reports the wall-clock window.

POST /pendant/feed/enroll {name, device, start_ms, end_ms} (`create`) stores a
row in `enrollments` and returns its id. The processor's `process` then walks
each `waiting` row once the pendant's audio for the window has been ingested:

1. no chunks in the window (padded 1 s each side) for 2 h -> rejected
   `no audio from that pendant`; chunks not yet settled -> still waiting;
2. under 25 s of speech (Silero VAD) -> `too little speech`;
3. the speech is cut into 5-10 s pieces and embedded; the pieces must be one
   voice (average-linkage clusters at `CONSISTENCY`: the biggest cluster keeps
   at least 85% of the speech) -> else `more than one voice`;
4. a name with a strong voiceprint (>= 5 samples; the wearer's always is) must
   hear the same voice (cosine to its mean >= match_threshold; samples from mixed voices
   neither count nor vouch) -> else
   `not allowed` (a different person typed that name);
5. else the name joins `speakers.allowed` (source `enrolled`: reading the
   sentences is the consent) and every piece is added as its own voiceprint
   sample, keyed `enroll:<id>:<n>` so a retry adds nothing twice -> `added`
   (its voice-library clips are tagged origin `pendant`).

The phone's own recording of the same reading (`POST /pendant/feed/enroll/<id>/phone`,
a WAV or M4A of at most 60 s) goes through steps 2-5 at once (`add_phone`): the same
speech, consistency, consent and wearer checks, samples keyed `enroll:<id>:phone:<n>`
(no library clips: the library keeps Opus as recorded). Its verdict is stored on the row (`phone_*`) and shown as
`phone` in the status; it is independent of the pendant copy, which is still processed
when the pendant syncs. Re-sending replaces the phone samples, never adding twice.

A window holds its audio back from conversations only while it is `waiting`; only
an `added` enrollment consumes it (the chunks are marked done, and audio that
drains in late stays out too, see `WINDOW_SQL`). A `rejected` window, including one
that expires (still waiting 2 h after the reading, or its check failing 5 times),
releases its audio, which is then transcribed as an ordinary conversation.
A request whose window is in the future or more than 2 h old is refused, and so is
one overlapping any other non-rejected enrollment on the same device.
Safe to re-run.
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

from . import config as cfgmod

PAD_MS = 1000                  # the window grows this much each side (clock skew)
MIN_WINDOW_S = 25.0            # a shorter reading can't hold enough speech
MAX_WINDOW_S = 600.0
MIN_SPEECH_S = 25.0
PIECE_MIN_S = 5.0
PIECE_MAX_S = 10.0
RUN_GAP_S = 0.5                # speech spans closer than this are one run
JOIN_GAP_S = 1.5               # runs closer than this share a piece
MIN_PIECES = 3
MAX_PIECES = 12
CONSISTENCY = 0.5              # average-linkage cosine: pieces above this are one voice
MAX_ODD_SHARE = 0.15           # speech outside the main voice we tolerate (a cough, an interjection)
STRONG_SAMPLES = 5             # a voiceprint this big vets who types its name
SETTLE_MS = 300_000            # same quiet time the processor waits before a conversation
GRACE_MS = 20 * 60_000         # a failing check waits this long for a late drain first
NO_AUDIO_MS = 2 * 3_600_000    # nothing from the pendant for this long: give up
MAX_ATTEMPTS = 5               # a check that keeps failing (model missing...) expires
MAX_AGE_MS = 2 * 3_600_000     # a reading is reported within this long of starting
MAX_AHEAD_MS = 60_000          # ...and can't end later than this ahead of the server's clock
RESERVED = {"someone", "me", "them", "you", "unknown", "nobody"}

REASON_LITTLE = "too little speech"
REASON_MANY = "more than one voice"
REASON_NO_AUDIO = "no audio from that pendant"
REASON_DENIED = "not allowed"
REASON_FAILED = "could not be checked"

SQL = """CREATE TABLE IF NOT EXISTS enrollments (
    id INTEGER PRIMARY KEY, name TEXT, device TEXT, start_ms INT, end_ms INT,
    lo_ms INT, hi_ms INT, status TEXT, reason TEXT, samples INT, source TEXT, attempts INT DEFAULT 0,
    created_at REAL, finished_at REAL, phone_status TEXT, phone_reason TEXT, phone_samples INT,
    UNIQUE (device, start_ms, end_ms, name));
CREATE INDEX IF NOT EXISTS enrollments_window ON enrollments (device, lo_ms, hi_ms);"""

# conversations() adds this to its chunk query: audio inside a waiting or added window is
# never a conversation (a rejected window releases it).
WINDOW_SQL = ("AND NOT EXISTS (SELECT 1 FROM enrollments e WHERE e.device = chunks.device "
              "AND e.status IN ('waiting', 'added') AND chunks.ts_ms BETWEEN e.lo_ms AND e.hi_ms)")


class BadRequest(ValueError):
    """The request is malformed (the feed answers 400)."""


class Conflict(ValueError):
    """The window overlaps another enrollment on that pendant (the feed answers 409)."""


def _now() -> float:
    return time.time()


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(SQL)
    have = {r[1] for r in con.execute("PRAGMA table_info(enrollments)")}
    for col, decl in (("attempts", "INT DEFAULT 0"), ("phone_status", "TEXT"), ("phone_reason", "TEXT"),
                      ("phone_samples", "INT")):
        if col not in have:
            con.execute(f"ALTER TABLE enrollments ADD COLUMN {col} {decl}")
            con.commit()


def _row(con: sqlite3.Connection, eid: int) -> dict | None:
    cur = con.execute("SELECT id, name, device, start_ms, end_ms, lo_ms, hi_ms, status, reason, samples, "
                      "source, phone_status, phone_reason, phone_samples FROM enrollments WHERE id = ?", (eid,))
    r = cur.fetchone()
    return dict(zip([c[0] for c in cur.description], r)) if r else None


def status(con: sqlite3.Connection, eid: int) -> dict | None:
    """What the app polls: {id, name, status, samples?, reason?}, None for an unknown id."""
    r = _row(con, eid)
    if r is None:
        return None
    out = {"id": r["id"], "name": r["name"], "status": r["status"]}
    if r["status"] == "added":
        out["samples"] = r["samples"]
    if r["status"] == "rejected":
        out["reason"] = r["reason"]
    if r["phone_status"]:       # the phone-mic copy of the same reading, judged when it was uploaded
        out["phone"] = {"status": r["phone_status"]}
        if r["phone_status"] == "added":
            out["phone"]["samples"] = r["phone_samples"]
        else:
            out["phone"]["reason"] = r["phone_reason"]
    return out


def create(con: sqlite3.Connection, name: str, device: str, start_ms: int, end_ms: int,
           now: float | None = None) -> int:
    """Record a reading window and return its id (the same window and name again returns
    the first id). Raises BadRequest for a malformed request, including a window that
    ends more than a minute ahead of the server's clock or started over 2 h ago, and
    Conflict when it overlaps another non-rejected enrollment on that pendant, whatever
    the name. A window too short to hold 25 s of speech, or a reserved name, is stored
    already rejected."""
    try:
        name = cfgmod.clean_name(name)
    except ValueError:
        raise BadRequest("invalid name") from None
    device = str(device or "").strip().upper()
    if not device or not all(c in "0123456789ABCDEF" for c in device):
        raise BadRequest("invalid device")
    if not (isinstance(start_ms, int) and isinstance(end_ms, int)) or isinstance(start_ms, bool):
        raise BadRequest("need integer start_ms and end_ms")
    if start_ms < 1_600_000_000_000 or end_ms <= start_ms:
        raise BadRequest("invalid window")
    if (end_ms - start_ms) / 1000 > MAX_WINDOW_S:
        raise BadRequest("window too long")
    now = _now() if now is None else now
    if end_ms > now * 1000 + MAX_AHEAD_MS:
        raise BadRequest("window is in the future")
    if start_ms < now * 1000 - MAX_AGE_MS:
        raise BadRequest("window is too old")
    ensure_schema(con)
    short = (end_ms - start_ms) / 1000 < MIN_WINDOW_S
    reserved = name.lower() in RESERVED or name.lower().startswith("speaker ")
    reason = REASON_DENIED if reserved else REASON_LITTLE if short else None
    key = (device, start_ms, end_ms, name)
    con.commit()
    con.execute("BEGIN IMMEDIATE")      # the overlap check and the insert are one step: no two requests both pass
    try:
        same = con.execute("SELECT id FROM enrollments WHERE device=? AND start_ms=? AND end_ms=? AND name=?",
                           key).fetchone()
        if same:
            con.rollback()
            return same[0]
        if not reason and con.execute(
                "SELECT 1 FROM enrollments WHERE device=? AND status != 'rejected' AND start_ms < ? AND end_ms > ?",
                (device, end_ms, start_ms)).fetchone():
            raise Conflict("overlaps another voice sample")
        eid = con.execute(
            "INSERT INTO enrollments (name, device, start_ms, end_ms, lo_ms, hi_ms, status, reason, "
            "samples, source, created_at, finished_at) VALUES (?,?,?,?,?,?,?,?,0,'enrolled',?,?)",
            (name, device, start_ms, end_ms, start_ms - PAD_MS, end_ms + PAD_MS,
             "rejected" if reason else "waiting", reason, now, now if reason else None)).lastrowid
        con.commit()
        return eid
    except BaseException:
        con.rollback()
        raise


def pieces(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """5-10 s pieces covering the speech: spans within RUN_GAP_S make a run, runs over
    10 s are split evenly, and neighbouring runs within JOIN_GAP_S share a piece while it
    stays at most 10 s. Anything under 5 s on its own is dropped."""
    runs: list[list[float]] = []
    for s, e in sorted(spans):
        if runs and s - runs[-1][1] < RUN_GAP_S:
            runs[-1][1] = max(runs[-1][1], e)
        else:
            runs.append([s, e])
    parts: list[tuple[float, float]] = []
    for s, e in runs:
        n = max(1, -(-int(round((e - s) * 1000)) // int(PIECE_MAX_S * 1000)))   # ceil(len / 10 s)
        step = (e - s) / n
        parts += [(s + i * step, s + (i + 1) * step) for i in range(n)]
    out: list[list[float]] = []
    for s, e in parts:
        if out and s - out[-1][1] <= JOIN_GAP_S and e - out[-1][0] <= PIECE_MAX_S:
            out[-1][1] = e
        else:
            out.append([s, e])
    return [(s, e) for s, e in out if e - s >= PIECE_MIN_S]


def _speech(spans, total_s: float) -> float:
    return sum(max(0.0, min(e, total_s) - max(s, 0.0)) for s, e in spans)


def _speech_in(spans, a: float, b: float) -> float:
    return sum(max(0.0, min(e, b) - max(s, a)) for s, e in spans)


def _render(rows, path: Path) -> None:
    from .oggopus import OggOpusWriter
    w = OggOpusWriter(path)
    for _ts, packet in rows:
        w.add_packet(packet)
    w.close()


def _same_voice(embs, speech: list[float], threshold: float):
    """(kept indices, kept speech share): the pieces of the biggest voice cluster."""
    import numpy as np

    from . import speakers
    labels = speakers.agglomerative(embs, threshold)
    weight = np.bincount(labels, weights=np.asarray(speech))
    main = int(np.argmax(weight))
    return [i for i, c in enumerate(labels) if c == main], float(weight[main] / weight.sum())


# ---------------------------------------------------------------------------
# the state machine

def _drop_samples(data_dir: Path, name: str, eid: int) -> None:
    """Take every pendant-copy `enroll:<id>:<n>` sample out of name's voiceprint (under its lock)."""
    from . import speakers
    speakers.replace_samples(speakers.voiceprint_dir(data_dir), name,
                             [speakers.sample_key(f"enroll:{eid}:{k}") for k in range(MAX_PIECES)], [])
    try:     # and the reading's clips leave the permanent voice library
        from . import voice_library
        for k in range(MAX_PIECES):      # the pendant copy's clips only: the phone copy stands alone
            voice_library.drop(data_dir, f"enroll:{eid}:{k}")
    except Exception:  # noqa: BLE001
        pass


def _vetting_print(con: sqlite3.Connection, vdir: Path, name: str):
    """(mean, count) of name's voiceprint for the same-voice check, without any sample from a
    mixed voice (two people in one voice must never vouch for a reading), or None."""
    from . import speakers
    key = next((n for n in speakers.load_voiceprints(vdir) if n.lower() == name.lower()), None)
    vp = speakers._read(vdir, key) if key else None
    if vp is None:
        return None
    try:
        mixed = {r[0] for r in con.execute("SELECT id FROM speaker_clusters WHERE COALESCE(mixed, 0) = 1")}
    except sqlite3.OperationalError:
        mixed = set()
    keep = [i for i, c in enumerate(vp.ids) if c not in mixed]
    vp = speakers.Voiceprint(vp.legacy_sum, vp.legacy, [vp.ids[i] for i in keep], vp.embs[keep])
    return (vp.mean(), vp.count) if vp.count > 0 else None


def _finish(con, eid: int, state: str, reason: str | None = None, samples: int = 0,
            data_dir: Path | None = None) -> str:
    if state == "rejected" and data_dir is not None:
        # a crash after the samples were written, then a retry that rejects or expires: none may stay
        _drop_samples(data_dir, con.execute("SELECT name FROM enrollments WHERE id = ?", (eid,)).fetchone()[0], eid)
    con.execute("UPDATE enrollments SET status = ?, reason = ?, samples = ?, finished_at = ? WHERE id = ?",
                (state, reason, samples, time.time(), eid))
    con.commit()
    return state


def _check_voice(con, data_dir: Path, sp: dict, clip: Path, spans, name: str):
    """The voice checks both sources share, on a decoded clip whose speech already passed
    the 25 s bar: (embeddings, kept piece indices, cuts, speech per cut), or the rejection
    reason (too few pieces, more than one voice, a different voice than `name`'s strong
    voiceprint)."""
    import numpy as np

    from . import speakers
    cuts = pieces(spans)
    if len(cuts) > MAX_PIECES:     # keep the pieces with the most speech, in time order
        cuts = sorted(sorted(cuts, key=lambda p: -_speech_in(spans, *p))[:MAX_PIECES])
    if len(cuts) < MIN_PIECES:
        return REASON_LITTLE
    embs = np.asarray(speakers.get_embedder(data_dir)(clip, cuts), dtype=np.float32)
    if embs.ndim != 2 or len(embs) != len(cuts):
        raise ValueError("embedder returned the wrong shape")
    embs = speakers.normalize(embs)
    each = [_speech_in(spans, a, b) for a, b in cuts]
    keep, share = _same_voice(embs, each, float(sp.get("enroll_consistency", CONSISTENCY)))
    if share < 1 - MAX_ODD_SHARE:
        return REASON_MANY
    kept = [each[i] for i in keep]
    if sum(kept) < MIN_SPEECH_S * (1 - MAX_ODD_SHARE) or len(keep) < MIN_PIECES:
        return REASON_LITTLE
    embs = embs[keep]
    mine = _vetting_print(con, speakers.voiceprint_dir(data_dir), name)
    if mine is not None and mine[1] >= STRONG_SAMPLES:
        sim = float(speakers.normalize(embs.mean(0)) @ speakers.normalize(mine[0]))
        if sim < float(sp.get("match_threshold", 0.5)):
            return REASON_DENIED
    return embs, keep, cuts, each


def _render_to(rows, tmp: str) -> Path:
    clip = Path(tmp) / "window.opus"
    _render(rows, clip)
    return clip


def _store(data_dir: Path, name: str, prefix: str, embs, keep, cuts, each, source: str, make_clip) -> int:
    """Consent, then the kept pieces as voiceprint samples keyed `<prefix>:<n>` (a retry
    replaces every key this reading could ever have written; a phone copy's keys carry
    `phone`) and, when `make_clip` gives one, clips in the voice library tagged `source`.
    The library keeps Opus packets as recorded, so only the pendant copy has clips. Returns
    the sample count."""
    from . import speakers
    canonical = cfgmod.allow_name(data_dir, name)       # consent: reading is the opt-in
    allowed = cfgmod.load(data_dir)["speakers"].get("allowed", [])
    if speakers.allowed_name(canonical, allowed) is None:
        raise speakers.ConsentError(canonical)
    every = [speakers.sample_key(f"{prefix}:{k}") for k in range(MAX_PIECES)]
    speakers.replace_samples(speakers.voiceprint_dir(data_dir), canonical, every,
                             [(speakers.sample_key(f"{prefix}:{n}"), e) for n, e in enumerate(embs)])
    if make_clip is None:
        return len(embs)
    try:   # the reading's clips join the permanent voice library (rank 2); the audio is still here
        with tempfile.TemporaryDirectory(prefix="enroll-lib-") as tmp:
            clip = make_clip(tmp)
            from . import voice_library
            for n, i in enumerate(keep):
                voice_library.add(data_dir, cfgmod.load(data_dir), canonical, kind="enroll", src=f"{prefix}:{n}",
                                  clip=clip, start_s=cuts[i][0], end_s=cuts[i][1], device=None, start_ms=None,
                                  speech_s=each[i], origin=source)
    except Exception:  # noqa: BLE001 - the library never blocks an enrollment
        pass
    return len(embs)


def evaluate(con: sqlite3.Connection, data_dir: Path, cfg: dict, row: dict, now_ms: int | None = None) -> str:
    """Move one waiting enrollment forward: returns its status afterwards
    ("waiting" when the audio isn't all in yet)."""
    from . import speakers
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    sp = cfg.get("speakers", cfg)
    eid, device = row["id"], row["device"]
    rows = con.execute("SELECT ts_ms, opus FROM chunks WHERE device=? AND ts_ms BETWEEN ? AND ? "
                       "AND opus IS NOT NULL ORDER BY ts_ms, page, ord", (device, row["lo_ms"], row["hi_ms"])).fetchall()
    if not rows:
        if now_ms - row["end_ms"] >= NO_AUDIO_MS:
            return _finish(con, eid, "rejected", REASON_NO_AUDIO, data_dir=data_dir)
        return "waiting"
    later = con.execute("SELECT 1 FROM chunks WHERE device=? AND ts_ms > ? LIMIT 1",
                        (device, row["hi_ms"])).fetchone()
    if not later and (now_ms - rows[-1][0] < SETTLE_MS or now_ms - row["end_ms"] < SETTLE_MS):
        return "waiting"

    with tempfile.TemporaryDirectory(prefix="enroll-") as tmp:
        clip = Path(tmp) / "window.opus"
        _render(rows, clip)
        audio = speakers.load_audio(clip)
        total_s = len(audio) / speakers.SAMPLE_RATE
        spans = speakers.speech_timestamps(audio)
        speech_s = _speech(spans, total_s)
        if speech_s < MIN_SPEECH_S:
            if now_ms - row["end_ms"] < GRACE_MS and not later:
                return "waiting"      # the rest of the audio may still be draining
            return _finish(con, eid, "rejected", REASON_LITTLE, data_dir=data_dir)
        verdict = _check_voice(con, data_dir, sp, clip, spans, row["name"])
    if isinstance(verdict, str):
        return _finish(con, eid, "rejected", verdict, data_dir=data_dir)
    embs, keep, cuts, each = verdict
    n = _store(data_dir, row["name"], f"enroll:{eid}", embs, keep, cuts, each, "pendant",
               lambda tmp: _render_to(rows, tmp))
    return _finish(con, eid, "added", samples=n)


PHONE_MAX_S = 60.0
PHONE_SLACK_S = 2.0            # a recorder's tail past its 60 s stop


def add_phone(con: sqlite3.Connection, data_dir: Path, cfg: dict, eid: int, body: bytes) -> dict | None:
    """Judge and store the phone-mic recording of reading `eid` (None for an unknown id).
    Raises BadRequest for audio that can't be decoded or runs over 60 s. Returns the status.
    The row's name was already vetted by `create` (a reserved name stays refused)."""
    from . import speakers
    ensure_schema(con)
    row = _row(con, eid)
    if row is None:
        return None
    if row["status"] == "rejected" and row["reason"] == REASON_DENIED:
        return _set_phone(con, eid, "rejected", REASON_DENIED, data_dir=data_dir)
    cfg = cfg if cfg is not None else cfgmod.load(data_dir)
    sp = cfg.get("speakers", cfg)
    with tempfile.TemporaryDirectory(prefix="enroll-phone-") as tmp:
        clip = Path(tmp) / "phone.audio"
        clip.write_bytes(body)
        try:
            audio = speakers.load_audio(clip)
        except Exception:  # noqa: BLE001 - any decoder error is a bad upload
            raise BadRequest("could not decode audio") from None
        total_s = len(audio) / speakers.SAMPLE_RATE
        if total_s > PHONE_MAX_S + PHONE_SLACK_S:
            raise BadRequest("audio longer than 60 s")
        spans = speakers.speech_timestamps(audio)
        if _speech(spans, total_s) < MIN_SPEECH_S:
            return _set_phone(con, eid, "rejected", REASON_LITTLE, data_dir=data_dir)
        verdict = _check_voice(con, data_dir, sp, clip, spans, row["name"])
        if isinstance(verdict, str):
            return _set_phone(con, eid, "rejected", verdict, data_dir=data_dir)
        embs, keep, cuts, each = verdict
        n = _store(data_dir, row["name"], f"enroll:{eid}:phone", embs, keep, cuts, each, "phone", None)
    return _set_phone(con, eid, "added", samples=n)


def _set_phone(con, eid: int, state: str, reason: str | None = None, samples: int = 0,
               data_dir: Path | None = None) -> dict:
    if state == "rejected" and data_dir is not None:     # a retry that rejects leaves no earlier phone samples
        from . import speakers
        name = con.execute("SELECT name FROM enrollments WHERE id = ?", (eid,)).fetchone()[0]
        speakers.replace_samples(speakers.voiceprint_dir(data_dir), name,
                                 [speakers.sample_key(f"enroll:{eid}:phone:{k}") for k in range(MAX_PIECES)], [])
        try:
            from . import voice_library
            for k in range(MAX_PIECES):
                voice_library.drop(data_dir, f"enroll:{eid}:phone:{k}")
        except Exception:  # noqa: BLE001
            pass
    con.execute("UPDATE enrollments SET phone_status = ?, phone_reason = ?, phone_samples = ? WHERE id = ?",
                (state, reason, samples, eid))
    con.commit()
    return status(con, eid)


def process(con: sqlite3.Connection, data_dir: Path, cfg: dict | None = None, now_ms: int | None = None,
            log=print) -> list[int]:
    """Evaluate every waiting enrollment. A check that fails MAX_ATTEMPTS times, or is
    still unresolved 2 h after the reading, is rejected (`could not be checked`), which
    releases its audio. Only added enrollments consume audio: their window's chunks are
    marked done. Returns the ids that finished. A failing check is logged and retried."""
    ensure_schema(con)
    cfg = cfg if cfg is not None else cfgmod.load(data_dir)
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    finished = []
    for (eid,) in con.execute("SELECT id FROM enrollments WHERE status = 'waiting' ORDER BY id").fetchall():
        row = _row(con, eid)
        try:
            state = evaluate(con, data_dir, cfg, row, now_ms)
        except Exception as e:  # noqa: BLE001 - never block transcription
            n = con.execute("UPDATE enrollments SET attempts = attempts + 1 WHERE id = ? RETURNING attempts",
                            (eid,)).fetchone()[0]
            con.commit()
            log(f"enrollment {eid}: {type(e).__name__}: {str(e)[:120]}; attempt {n}")
            state = _finish(con, eid, "rejected", REASON_FAILED, data_dir=data_dir) if n >= MAX_ATTEMPTS else "waiting"
        if state == "waiting" and now_ms - row["end_ms"] >= NO_AUDIO_MS:
            state = _finish(con, eid, "rejected", REASON_FAILED, data_dir=data_dir)
        if state != "waiting":
            s = status(con, eid)
            log(f"enrollment {eid} ({row['name']}): {s['status']}" + (f", {s['reason']}" if s.get("reason")
                                                                       else f", {s['samples']} samples"))
            finished.append(eid)
    con.execute("UPDATE chunks SET done = 1 WHERE done = 0 AND EXISTS (SELECT 1 FROM enrollments e "
                "WHERE e.device = chunks.device AND e.status = 'added' AND chunks.ts_ms BETWEEN e.lo_ms AND e.hi_ms)")
    con.commit()
    return finished
