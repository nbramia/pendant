#!/usr/bin/env python3
"""Turn uploaded pendant segments into daily vault transcripts.

1. Ingest: decode every new spool segment (.pmsg) into audio chunks and store
   them in SQLite keyed by (device, storage session, page, chunk), so pages the
   phone sent twice (live + drain, or re-drains) count once.
2. Group: walk chunks in flash order and split into conversations wherever the
   pendant's clock jumps by more than GAP_S (it records on voice activity).
3. Transcribe: once a conversation has been quiet for SETTLE_S, write it to
   Ogg Opus, transcribe with faster-whisper large-v3-turbo (local, CPU), and
   append it to <notes_dir>/YYYY-MM-DD.md. Paragraphs carry a
   wall-clock [HH:MM] at the start, after pauses >= PAUSE_S, and every
   STAMP_EVERY_S, mapped back from clip offsets through the page clock.
   When two pendants recorded the same moment, only the conversation with
   more audio is written (transcribed spans are kept in the `spans` table).
4. Per written conversation, in order: transcribe -> speakers (local ECAPA
   voiceprints, `speakers.py`) -> summarize (title/overview/action items,
   `summarize.py`) -> Raw section `## HH:MM — Title (XmYYs) · <pendant name>` ->
   Event note if notable -> the day's Highlights file (`highlights.py`).
   Speaker and summary failures fall back to unlabeled text and a heuristic
   title; they never block transcription.
5. Stars: a short press of the pendant's button is stored in `stars` at ingest
   (segments ingested before stars existed are rescanned once, `meta`).
   Each run places pending stars in the written conversation covering their
   time: a `⭐ ` after the paragraph's `[HH:MM] ` stamp, notable, and a line in
   the day's Highlights. Stars that arrive after their conversation was
   written are applied the same way on a later run.

Safe to re-run; a lock prevents overlap. Run: `.venv/bin/python -m pendant_direct.process`.
"""
from __future__ import annotations

import bisect
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config as cfgmod
from . import day_signals
from . import highlights as hl
from . import attendance
from . import calendar_hint
from . import classify as classifier
from . import enroll
from . import meetings
from . import names as pnames
from . import retention
from . import summarize
from . import tasks
from . import trash
from . import voice_audio, voice_library
from . import voices
from .voices import sample_ok
from .decode import iter_audio_chunks_from_messages
from .keys import load_keyset
from .oggopus import OggOpusWriter

DATA = Path(os.environ.get("PENDANT_DATA") or Path.home() / "pendant-direct-data")
SPOOL = DATA / "spool"
DB = DATA / "pages.db"
CLIPS = DATA / "clips"
NOTES_DIR = cfgmod.notes_dir(DATA)
TZ = ZoneInfo("America/New_York")
GAP_S = 60           # silence that splits two conversations
SETTLE_S = 300       # wait this long after a conversation's last audio
MAX_CONV_S = 1800    # cut longer stretches (back-to-back talk) so they land while still going
CUT_WINDOW_S = 300   # ...at the longest pause within this long before the cap
MIN_AUDIO_S = 2.0    # skip blips
PAUSE_S = 20         # a pause this long inside a conversation gets a fresh [HH:MM]
STAMP_EVERY_S = 300  # and so does any paragraph this long after the last stamp
FRAME_S = 0.02       # pendant Opus frames are 20 ms
VALID_EPOCH_MS = 1_600_000_000_000


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


CHUNKS_SQL = """CREATE TABLE IF NOT EXISTS chunks (
            device TEXT, page INT, ord INT, opus_sha TEXT,
            ts_ms INT, opus BLOB, done INT DEFAULT 0,
            PRIMARY KEY (device, page, ord, opus_sha))"""


CONVERSATIONS_SQL = """CREATE TABLE IF NOT EXISTS conversations (
            device TEXT, start_ms INT, end_ms INT, audio_s REAL, day TEXT, pendant TEXT,
            title TEXT, overview TEXT, action_items TEXT, category TEXT, people TEXT,
            notable INT, raw_heading TEXT, event_file TEXT, clip TEXT, meeting_note TEXT,
            meeting_source TEXT,
            PRIMARY KEY (device, start_ms))"""

# One row per short press of a pendant's button ("star a moment"), keyed on the
# press's own clock so live and drained copies of a page give one star.
# conv_device/conv_start_ms name the conversation it was placed in (NULL until
# that conversation is written); text is the starred paragraph.
STARS_SQL = """CREATE TABLE IF NOT EXISTS stars (
            device TEXT, ts_ms INT, conv_device TEXT, conv_start_ms INT, text TEXT,
            PRIMARY KEY (device, ts_ms))"""
SHORT_PRESS = 1          # shared.proto Button.BUTTON_SHORT_PRESS
STAR = "⭐"
STAR_GRACE_S = GAP_S     # a press this close before/after a conversation belongs to it

# One row per speaker cluster of a written conversation. label is the enrolled
# name (labeled_by 'match' at processing time, 'meeting'/'calendar' from those
# passes, 'user' from the app) or NULL; tag is what the transcript shows
# ("Speaker A", the name; "Someone" for a stranger in older rows).
SPEAKER_CLUSTERS_SQL = """CREATE TABLE IF NOT EXISTS speaker_clusters (
            id INTEGER PRIMARY KEY, device TEXT, start_ms INT, tag TEXT, label TEXT,
            embedding BLOB, clip TEXT, start_s REAL, end_s REAL, speech_s REAL,
            excerpt TEXT, labeled_by TEXT, created_at REAL,
            voiced REAL, noise INT DEFAULT 0)"""
# voiced: fraction of the stored snippet (start_s..end_s) that is speech, NULL until
# the feed checks it; noise: 1 once the wearer said "not a voice"; enrolled_in: the
# voiceprint this cluster's embedding was folded into ('' none, NULL = rows from
# before the column, see voices.prior_enrollment); label_kind: 'person',
# 'stranger', 'noise' or 'mixed' once the wearer answered; mixed: 1 when turn rescoring took most of
# its speech away or the wearer answered "Two people" (two people in one voice: never a voiceprint
# sample, never bulk relabeled); match_score: the score the voiceprint pass gave the cluster for the person it
# named it after (the value `speakers.match_threshold` applies to), set with labeled_by 'match',
# NULL otherwise. Voice Scoring tables: voices.SQL.
SPEAKER_CLUSTER_MIGRATIONS = voices.CLUSTER_COLUMNS

# One row per rescored speaker turn (2 s of speech or more) of a written conversation: its
# near-field features (wearer.py), its score for the wearer (margin over his threshold, NULL
# without a voiceprint) and, with a wearer model, its wearer probability. tag is the voice's
# tag in the transcript.
SPEAKER_TURNS_SQL = """CREATE TABLE IF NOT EXISTS speaker_turns (
            id INTEGER PRIMARY KEY, device TEXT, start_ms INT, start_s REAL, end_s REAL, speech_s REAL,
            tag TEXT, level_db REAL, decay_db REAL, flatness REAL, score REAL, p REAL)"""


# kind: conversation | media | fragment; importance 0-2; kind_reasons: JSON list.
# NULL on rows from before classification (the feed reads them as conversation, 0).
KIND_COLUMNS = (("kind", "TEXT"), ("importance", "INT"), ("kind_reasons", "TEXT"))


def wearer_similarities(clusters, wearer: str, data: Path) -> list:
    """[(label, speech_s, embedding), ...] -> [(label, speech_s, similarity), ...]: how
    each unlabelled voice scores against the wearer's voiceprint (`voices.score`, the
    Voice Scoring scorer). None where it can't be known (labelled voices, no embedding,
    no voiceprint): unknown is never evidence of absence."""
    out = [(label, speech, None) for label, speech, _e in clusters]
    todo = [i for i, (label, _s, e) in enumerate(clusters) if not label and e is not None]
    if not todo:
        return out
    try:
        import numpy as np
        from . import speakers
        vps = speakers.load_voiceprints(speakers.voiceprint_dir(data))
        key = next((k for k in vps if k.lower() == wearer.lower()), None)
        if key is None:
            return out
        scores = voices.score(np.stack([np.asarray(clusters[i][2], dtype=np.float32) for i in todo]),
                              {key: vps[key]})
        for i, sc in zip(todo, scores):
            out[i] = (out[i][0], out[i][1], sc.similarity)
    except Exception as e:  # unknown, not absent
        log(f"wearer similarity failed ({type(e).__name__})")
    return out


def classification(summary, text: str, audio_s: float, low_conf_share: float, clusters, cfg: dict,
                   data: Path | None = None):
    """(kind, importance, reasons) for a written conversation; a conversation with
    importance 0 if anything goes wrong. `clusters`: [(label, speech_s, embedding), ...]."""
    try:
        wearer = cfgmod.wearer(cfg.get("speakers"))
        share, absent, voices_n = classifier.evidence(
            wearer_similarities(clusters, wearer, data or DATA), wearer, cfg.get("classify"))
        sig = classifier.Signals(words=len(summarize.plain(text).split()), audio_s=audio_s,
                                 low_conf_share=low_conf_share, wearer_share=share, wearer_absent=absent,
                                 voices=voices_n)
    except Exception as e:
        log(f"classification failed ({type(e).__name__}); conversation, importance 0")
        return "conversation", 0, ["classification failed"]
    return classifier.safe_classify(summary, sig, cfg.get("classify"), log=log)


def db(path: Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or DB, timeout=30)
    # Older databases keyed chunks on (device, page, opus_sha), which merged
    # byte-identical packets (e.g. silence frames) within a page. Rebuild with
    # the chunk's position in the key, keeping done marks.
    old = con.execute("SELECT sql FROM sqlite_master WHERE name='chunks'").fetchone()
    if old and "PRIMARY KEY (device, page, opus_sha)" in old[0]:
        con.executescript(f"""
            ALTER TABLE chunks RENAME TO chunks_old;
            {CHUNKS_SQL};
            INSERT INTO chunks SELECT device, page, ord, opus_sha, ts_ms, opus, done FROM chunks_old;
            DROP TABLE chunks_old;
            DROP INDEX IF EXISTS chunks_pending;
        """)
    con.executescript(f"""
        {CHUNKS_SQL};
        CREATE INDEX IF NOT EXISTS chunks_pending ON chunks (device, done, ts_ms);
        CREATE TABLE IF NOT EXISTS segments (name TEXT PRIMARY KEY, ingested_at REAL);
        CREATE TABLE IF NOT EXISTS spans (device TEXT, start_ms INT, end_ms INT, audio_s REAL);
        {CONVERSATIONS_SQL};
        CREATE INDEX IF NOT EXISTS conversations_day ON conversations (day);
        {SPEAKER_CLUSTERS_SQL};
        CREATE INDEX IF NOT EXISTS speaker_clusters_conv ON speaker_clusters (device, start_ms);
        {SPEAKER_TURNS_SQL};
        CREATE INDEX IF NOT EXISTS speaker_turns_conv ON speaker_turns (device, start_ms);
        {STARS_SQL};
        {tasks.SQL};
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        {CALENDAR_SQL};
        {voices.SQL}
        {summarize.SQL}
        {day_signals.SQL};
        {trash.SQL}
        {enroll.SQL}
    """)
    sc_cols = {r[1] for r in con.execute("PRAGMA table_info(speaker_clusters)")}
    for col, typ in SPEAKER_CLUSTER_MIGRATIONS:
        if col not in sc_cols:
            con.execute(f"ALTER TABLE speaker_clusters ADD COLUMN {col} {typ}")
    cols = {r[1] for r in con.execute("PRAGMA table_info(conversations)")}
    for col in ("meeting_note", "meeting_source", "calendar_enroll", "paragraph_ms", "paragraph_offsets",
                "meeting_notes", "calendar_event"):
        if col not in cols:
            con.execute(f"ALTER TABLE conversations ADD COLUMN {col} TEXT")
    for col, typ in KIND_COLUMNS:
        if col not in cols:
            con.execute(f"ALTER TABLE conversations ADD COLUMN {col} {typ}")
    voices.ensure_schema(con)
    from . import calendar_hint
    con.execute(calendar_hint.PRESENT_SQL)     # LifeOS's kept likely-present answers (warm_present)
    con.execute(calendar_hint.MUTES_SQL)       # events the wearer marked "Not with them"
    return con


def records(path: Path):
    data = path.read_bytes()
    pos = 0
    while pos + 4 <= len(data):
        (n,) = struct.unpack_from("<I", data, pos)
        pos += 4
        if pos + n > len(data):
            return
        yield data[pos:pos + n]
        pos += n


def segment_time_ms(name: str) -> int:
    """Upload-side fallback clock, from the segment name (DEVICE_YYYYMMDDTHHMMSSZ[_suffix])."""
    try:
        stamp = name.split(".")[0].split("_")[1]
        return int(dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
                   .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    except (IndexError, ValueError):
        return int(time.time() * 1000)


def star_of(ch, device: str, seg_name: str) -> tuple[str, int] | None:
    """(device, press ms) when this chunk is a short press of the button."""
    press = ch.chunk.button
    if press is None or press.button_event != SHORT_PRESS:
        return None
    if press.button_event_absolute_timestamp_ms <= VALID_EPOCH_MS:
        log(f"{seg_name}: star on page {ch.page_index} without a valid clock; skipped")
        return None
    return device, press.button_event_absolute_timestamp_ms


STAR_MERGE_MS = 10_000   # presses this close together on one pendant are one star


def add_stars(con: sqlite3.Connection, stars) -> int:
    """Insert (device, ms) presses, folding any within STAR_MERGE_MS of an
    existing star on the same pendant into that star. Returns stars added."""
    added = 0
    for device, ts in sorted(stars):
        if trash.star_tombstoned(con, device, ts, STAR_MERGE_MS):
            continue  # a press from a deleted recording
        near = con.execute("SELECT 1 FROM stars WHERE device=? AND ts_ms > ? AND ts_ms < ?",
                           (device, ts - STAR_MERGE_MS, ts + STAR_MERGE_MS)).fetchone()
        if not near:
            con.execute("INSERT OR IGNORE INTO stars (device, ts_ms) VALUES (?,?)", (device, ts))
            added += 1
    return added


def collapse_star_duplicates(con: sqlite3.Connection) -> set[str]:
    """Once per database: drop stars within STAR_MERGE_MS after an earlier star
    on the same pendant. Returns the days whose Highlights need rewriting."""
    if con.execute("SELECT 1 FROM meta WHERE key='stars_collapsed'").fetchone():
        return set()
    days: set[str] = set()
    rows = con.execute("SELECT device, ts_ms FROM stars ORDER BY device, ts_ms").fetchall()
    keep_at: dict[str, int] = {}
    for device, ts in rows:
        last = keep_at.get(device)
        if last is not None and ts - last < STAR_MERGE_MS:
            con.execute("DELETE FROM stars WHERE device=? AND ts_ms=?", (device, ts))
            days.add(dt.datetime.fromtimestamp(ts / 1000, TZ).date().isoformat())
        else:
            keep_at[device] = ts
    # Rewrite the affected Highlights before recording that the cleanup ran, so
    # a crash in between repeats the cleanup instead of stranding old entries.
    for day in sorted(days):
        write_highlights(con, day)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('stars_collapsed', ?)", (str(time.time()),))
    con.commit()
    return days


def backfill_stars(con: sqlite3.Connection) -> int:
    """Once per database: collect short presses from segments ingested before
    stars existed (their audio is already in `chunks` and isn't touched)."""
    if con.execute("SELECT 1 FROM meta WHERE key='stars_backfilled'").fetchone():
        return 0
    seen = {r[0] for r in con.execute("SELECT name FROM segments")}
    found: set[tuple[str, int]] = set()
    for seg in sorted(SPOOL.glob("*.pmsg")):
        if seg.name not in seen:
            continue  # ingest() reads it in full
        device = seg.name.split("_")[0]
        for ch in iter_audio_chunks_from_messages(records(seg)):  # button chunks aren't encrypted
            star = star_of(ch, device, seg.name)
            if star:
                found.add(star)
    add_stars(con, found)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('stars_backfilled', ?)", (str(time.time()),))
    con.commit()
    log(f"star backfill: {len(found)} stars in {len(seen)} earlier segments")
    return len(found)


def ingest(con: sqlite3.Connection) -> int:
    collapse_star_duplicates(con)  # before new presses meet old duplicates
    backfill_stars(con)
    seen = {r[0] for r in con.execute("SELECT name FROM segments")}
    new = 0
    for seg in sorted(SPOOL.glob("*.pmsg")):
        if seg.name in seen:
            continue
        device = seg.name.split("_")[0]
        fallback = segment_time_ms(seg.name)
        # A page can arrive several times: partial while live, then sealed in a
        # drain (with a different page timestamp), then again in re-drains. The
        # Opus packets themselves are identical across copies, so a packet's
        # page index plus its hash identifies it (also across wipes, where page
        # indices restart but the audio differs).
        ords: dict[tuple[int, int], int] = {}
        rows, stars, clock_ok = [], set(), []
        for ch in iter_audio_chunks_from_messages(records(seg), keys=load_keyset(device)):
            key = (ch.page_index, ch.absolute_timestamp_ms)
            ords[key] = ords.get(key, -1) + 1
            star = star_of(ch, device, seg.name)
            if star:
                stars.add(star)
            if not ch.opus:
                continue
            ts = ch.absolute_timestamp_ms if ch.absolute_timestamp_ms > VALID_EPOCH_MS else fallback
            sha = hashlib.sha256(ch.opus).hexdigest()[:24]
            rows.append((device, ch.page_index, ords[key], sha, ts, ch.opus))
            clock_ok.append(ch.absolute_timestamp_ms > VALID_EPOCH_MS)
        rows, dropped = trash.drop_tombstoned(con, device, rows, clock_ok)
        con.executemany("INSERT OR IGNORE INTO chunks (device, page, ord, opus_sha, ts_ms, opus) "
                        "VALUES (?,?,?,?,?,?)", rows)
        add_stars(con, stars)
        con.execute("INSERT INTO segments VALUES (?, ?)", (seg.name, time.time()))
        con.commit()
        new += 1
        log(f"ingested {seg.name}: {len(rows)} audio chunks"
            + (f", {len(stars)} star{'s' * (len(stars) != 1)}" if stars else "")
            + (f", {dropped} from deleted recordings dropped" if dropped else ""))
    return new


PENDING_SQL = "done=0 AND opus IS NOT NULL"
AUDIO_SQL = "opus IS NOT NULL"     # a chunk whose audio still exists (retention may have dropped it)


def candidate_sql(pending: bool) -> str:
    """The chunk filter of conversation grouping, shared by the live pass and --plan-day.
    Live takes untranscribed audio; the dry run takes every chunk that still has audio."""
    return PENDING_SQL if pending else AUDIO_SQL


def chunk_rows(con: sqlite3.Connection, device: str, pending: bool = True,
               lo_ms: int | None = None, hi_ms: int | None = None) -> list:
    """(rowid, ts_ms, opus) of the chunks to group, in order; never audio in an enrollment window."""
    return con.execute(
        f"SELECT rowid, ts_ms, {'opus' if pending else 'NULL'} FROM chunks WHERE device=? AND {candidate_sql(pending)} "
        f"{'' if pending else 'AND ts_ms >= ? AND ts_ms < ? '}{enroll.WINDOW_SQL} ORDER BY ts_ms, page, ord",
        (device,) if pending else (device, 0 if lo_ms is None else lo_ms, (1 << 62) if hi_ms is None else hi_ms),
    ).fetchall()


def conversations(con: sqlite3.Connection, device: str, pending: bool = True,
                  lo_ms: int | None = None, hi_ms: int | None = None):
    """Untranscribed audio in time order, split wherever the pendant was quiet > GAP_S,
    and stretches longer than MAX_CONV_S cut at their longest pause near the cap.
    `pending=False` (the --plan-day dry run) takes every chunk with audio in [lo_ms, hi_ms)."""
    rows = chunk_rows(con, device, pending, lo_ms, hi_ms)
    conv: list = []
    for r in rows:
        if conv and r[1] - conv[-1][1] > GAP_S * 1000:
            yield from cap_length(conv)
            conv = []
        conv.append(r)
    if conv:
        yield from cap_length(conv)


# --- tiny recordings -------------------------------------------------------
# A few seconds of sound between conversations would otherwise get a card, a Raw
# section and a summary call. Before transcription a conversation of under
# `grouping.tiny_audio_s` joins its neighbour on the same pendant when the gap is at
# most `grouping.tiny_join_gap_s` (the preceding one if both are close); otherwise it is
# transcribed and marked kind `fragment`, reason "tiny". Measured on Nov 8 2026: no
# conversation lasted 19.3-25.4 s, and gaps between conversations clustered at 60-138 s.
TINY_AUDIO_S = 20
TINY_JOIN_GAP_S = 120
TINY_REASON = "tiny"


def tiny_settings(cfg: dict) -> tuple[float, float]:
    """(tiny_audio_s, tiny_join_gap_s) from config; bad values fall back to the defaults. Audio is
    capped at 60 s so a real conversation is never tiny; 0 turns the rule off."""
    g = cfg.get("grouping") if isinstance(cfg.get("grouping"), dict) else {}
    out = []
    for key, default in (("tiny_audio_s", TINY_AUDIO_S), ("tiny_join_gap_s", TINY_JOIN_GAP_S)):
        v = g.get(key, default)
        out.append(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else default)
    return min(out[0], 60.0), out[1]


def join_blocked(con: sqlite3.Connection, device: str, a_ms: int, b_ms: int, pending: bool) -> bool:
    """True when something other than quiet lies between two conversations (the gap from a_ms
    to b_ms): audio already transcribed, held back, tombstoned or inside an enrollment window or
    a deleted recording's span. Joining across it would swallow or resurrect that audio."""
    cand = candidate_sql(pending)
    # inclusive at both ends: a non-pending chunk sharing a timestamp with an endpoint chunk is a barrier
    if con.execute(f"SELECT 1 FROM chunks WHERE device=? AND ts_ms>=? AND ts_ms<=? "
                   f"AND NOT ({cand} {enroll.WINDOW_SQL}) LIMIT 1", (device, a_ms, b_ms)).fetchone():
        return True
    if con.execute("SELECT 1 FROM enrollments WHERE device=? AND status IN ('waiting','added') "
                   "AND lo_ms<=? AND hi_ms>=? LIMIT 1", (device, b_ms, a_ms)).fetchone():
        return True
    if con.execute("SELECT 1 FROM range_tombstones WHERE device=? AND start_ms<=? AND end_ms>=? LIMIT 1",
                   (device, b_ms, a_ms)).fetchone():
        return True
    return bool(con.execute("SELECT 1 FROM tombstones WHERE device=? AND ts_ms>=? AND ts_ms<=? LIMIT 1",
                            (device, a_ms, b_ms)).fetchone())


def merge_tiny(con: sqlite3.Connection, device: str, convs: list, cfg: dict, pending: bool = True):
    """(groups, tiny_keys, pieces): `convs` with tiny conversations joined to a close neighbour.
    tiny_keys holds the start ms of the groups that stay tiny (to be marked fragments); pieces lists
    the audio seconds of the conversations that went into each merged group. Conversations under
    MIN_AUDIO_S (blips) are left alone and never a neighbour. Raises on any error: callers fall back."""
    tiny_s, gap_s = tiny_settings(cfg)
    audible = [c for c in convs if len(c) * FRAME_S >= MIN_AUDIO_S]
    blips = [c for c in convs if len(c) * FRAME_S < MIN_AUDIO_S]
    if tiny_s <= 0:
        return list(convs), set(), []

    def is_tiny(g) -> bool:
        return len(g[0]) * FRAME_S < tiny_s

    def joinable(a, b) -> bool:
        return ((b[0][0][1] - a[0][-1][1]) <= gap_s * 1000 and b[0][-1][1] - a[0][0][1] <= MAX_CONV_S * 1000
                and not join_blocked(con, device, a[0][-1][1], b[0][0][1], pending))

    groups: list = []                      # [rows, [piece audio_s, ...]]
    for c in audible:
        g = [c, [len(c) * FRAME_S]]
        prev = groups[-1] if groups else None
        if prev and is_tiny(g) and joinable(prev, g):
            prev[0] = prev[0] + c           # a tiny conversation joins the one before it
            prev[1] += g[1]
        elif prev and is_tiny(prev) and joinable(prev, g):
            groups[-1] = [prev[0] + c, prev[1] + g[1]]   # a tiny one with no close predecessor joins the next
        else:
            groups.append(g)
    tiny_keys = {g[0][0][1] for g in groups if is_tiny(g)}
    pieces = [g[1] for g in groups if len(g[1]) > 1]
    out = sorted([g[0] for g in groups] + blips, key=lambda c: c[0][1])
    return out, tiny_keys, pieces


def plan_conversations(con: sqlite3.Connection, device: str, cfg: dict, pending: bool = True,
                       lo_ms: int | None = None, hi_ms: int | None = None):
    """(conversations, tiny_keys, pieces) for a device. Any failure in the merge pass gives the
    plain grouping, with nothing flagged, and never blocks transcription."""
    base = list(conversations(con, device, pending, lo_ms, hi_ms))
    try:
        return merge_tiny(con, device, base, cfg, pending)
    except Exception as e:
        log(f"{device}: tiny-recording pass failed ({type(e).__name__}: {str(e)[:120]}); plain grouping")
        return base, set(), []


def plan_day(con: sqlite3.Connection, day: str, cfg: dict) -> dict:
    """Dry run for one local day: how its chunks group without and with the tiny rule (counts only).
    Conversations are those of MIN_AUDIO_S or more, which are the ones that get transcribed."""
    d0 = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
    lo = int(d0.timestamp() * 1000)
    hi = int((d0 + dt.timedelta(days=1)).timestamp() * 1000)
    tiny_s, gap_s = tiny_settings(cfg)
    res = {"day": day, "tiny_audio_s": tiny_s, "tiny_join_gap_s": gap_s, "conversations_before": 0,
           "conversations_after": 0, "tiny_before": 0, "tiny_merged": 0, "tiny_flagged": 0,
           "summary_calls_saved": 0, "merged_groups": 0, "merged_60s_or_more": 0, "blips_skipped": 0}
    for (device,) in con.execute("SELECT DISTINCT device FROM chunks WHERE ts_ms >= ? AND ts_ms < ?",
                                 (lo, hi)).fetchall():
        base = list(conversations(con, device, False, lo, hi))
        out, tiny_keys, pieces = merge_tiny(con, device, base, cfg, False)
        audible = [c for c in base if len(c) * FRAME_S >= MIN_AUDIO_S]
        res["blips_skipped"] += len(base) - len(audible)
        res["conversations_before"] += len(audible)
        res["conversations_after"] += sum(1 for c in out if len(c) * FRAME_S >= MIN_AUDIO_S)
        res["tiny_before"] += sum(1 for c in audible if len(c) * FRAME_S < tiny_s)
        res["tiny_flagged"] += len(tiny_keys)
        res["merged_groups"] += len(pieces)
        # every piece but the longest one of a merged group was absorbed into it
        res["tiny_merged"] += sum(len(p) - 1 for p in pieces)
        res["merged_60s_or_more"] += sum(1 for p in pieces for x in sorted(p)[:-1] if x >= 60)
    res["summary_calls_saved"] = res["conversations_before"] - res["conversations_after"]
    return res


def cap_length(conv: list):
    """Split a conversation longer than MAX_CONV_S (wall clock). Each cut goes at the
    longest gap between chunks in the CUT_WINDOW_S before the cap, so it falls on a
    pause rather than mid-sentence where possible."""
    while conv and conv[-1][1] - conv[0][1] > MAX_CONV_S * 1000:
        cap = conv[0][1] + MAX_CONV_S * 1000
        lo = cap - CUT_WINDOW_S * 1000
        best, best_gap = None, -1
        for i in range(1, len(conv)):
            t = conv[i][1]
            if t > cap:
                break
            if t >= lo and t - conv[i - 1][1] > best_gap:
                best, best_gap = i, t - conv[i - 1][1]
        if best is None:  # no chunk in the window: cut at the cap
            best = next(i for i in range(1, len(conv)) if conv[i][1] > cap)
        yield conv[:best]
        conv = conv[best:]
    if conv:
        yield conv


def write_clip(conv, clip: Path):
    """Write a conversation's packets to Ogg Opus. Returns (breakpoints, pauses):
    breakpoints are (audio offset s, wall ms) wherever the page clock moves, so
    any offset into the clip maps back to wall-clock time; pauses are the audio
    offsets where >= PAUSE_S of silence was left out."""
    w = OggOpusWriter(clip)
    breakpoints, pauses = [], []
    for i, r in enumerate(conv):
        if not breakpoints or r[1] != breakpoints[-1][1]:
            if breakpoints and r[1] - conv[i - 1][1] >= PAUSE_S * 1000:
                pauses.append(i * FRAME_S)
            breakpoints.append((i * FRAME_S, r[1]))
        w.add_packet(r[2])
    w.close()
    return breakpoints, pauses


def wall_ms(breakpoints, offset_s: float) -> int:
    i = max(bisect.bisect_right([b[0] for b in breakpoints], offset_s) - 1, 0)
    off, ts = breakpoints[i]
    return int(ts + (offset_s - off) * 1000)


def paragraphs(segs, breakpoints, pauses, labels=None, turns=None):
    """Group whisper segments into [(wall datetime, text)], starting a new
    paragraph after a pause, STAMP_EVERY_S since the last stamp, or (with
    speaker labels) a change of speaker, which opens the paragraph with
    `**Name:**`. labels: one per segment. turns (speakers.Turn pieces, finer than
    segments: a segment where the voice changes is split at the word) replace
    segments and labels."""
    return paragraphs_with_spans(segs, breakpoints, pauses, labels, turns)[0]


def paragraphs_with_spans(segs, breakpoints, pauses, labels=None, turns=None):
    """`paragraphs` plus each paragraph's (start s, end s) in the clip:
    ([(wall datetime, text)], [[start_s, end_s]])."""
    if turns:
        units = [(t.start, t.end, t.text, t.label) for t in turns]
    else:
        units = [(start, end, text, labels[i] if labels else None)
                 for i, (start, end, text) in enumerate(segs)]
    out: list[list] = []     # [wall ms, who, texts, start s, end s]
    prev_start = 0.0
    for start, end, text, who in units:
        ts = wall_ms(breakpoints, start)
        if (not out or any(prev_start < p <= start for p in pauses)
                or ts - out[-1][0] >= STAMP_EVERY_S * 1000 or who != out[-1][1]):
            out.append([ts, who, [], start, end])
        out[-1][2].append(text)
        out[-1][4] = max(end, out[-1][4])
        prev_start = start
    paras = [(dt.datetime.fromtimestamp(ts / 1000, TZ), (f"**{who}:** " if who else "") + " ".join(t))
             for ts, who, t, _s, _e in out]
    return paras, [[round(s, 2), round(e, 2)] for _ts, _who, _t, s, e in out]


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """True when more than half of the shorter span lies inside the other."""
    shared = min(a_end, b_end) - max(a_start, b_start)
    shorter = min(a_end - a_start, b_end - b_start)
    return shared >= 0 if shorter <= 0 else shared > shorter / 2


def pick_conversations(con: sqlite3.Connection, settled) -> set[int]:
    """Indices into settled [(device, conv)] worth transcribing. Two pendants
    hearing the same moment both record it: of conversations from different
    devices that overlap by more than half, keep the one with more audio.
    One already transcribed can't be retracted, so a new one is dropped only
    if a transcribed overlapping one has at least as much audio."""
    rivals = con.execute("SELECT device, start_ms, end_ms, audio_s FROM spans").fetchall()
    keep = set()
    for k in sorted(range(len(settled)), key=lambda k: -len(settled[k][1])):
        device, conv = settled[k]
        audio_s = len(conv) * FRAME_S
        if audio_s < MIN_AUDIO_S:
            continue
        span = (conv[0][1], conv[-1][1])
        dup = next((r for r in rivals if r[0] != device and r[3] >= audio_s
                    and overlaps(*span, r[1], r[2])), None)
        if dup:
            log(f"{device} {dt.datetime.fromtimestamp(span[0] / 1000, TZ):%Y-%m-%d %H:%M}: "
                f"{audio_s:.0f}s overlaps {dup[0]}'s {dup[3]:.0f}s; skipped")
            continue
        keep.add(k)
        rivals.append((device, *span, audio_s))
    return keep


_PRIOR_RUN = None


def naming_prior(sp: dict, prints: dict, key, segs) -> dict | None:
    """{name: prior} from LifeOS closeness for the enrolled people (closeness.py), including
    whether each is on the calendar event at the conversation's time. LifeOS is asked once per
    processing run (people in one pass, the calendar once per day) under a ~3 s budget in all;
    on a timeout or failure, or when nobody is linked, None (naming uses no prior)."""
    global _PRIOR_RUN
    try:
        from . import closeness
        if not any((sp.get("people") or {}).values()):
            return None
        if _PRIOR_RUN is None:
            _PRIOR_RUN = closeness.PriorRun()
        span = None
        if key and segs:
            start_ms = int(key[1])
            span = (start_ms, start_ms + int(max(s[1] for s in segs) * 1000))
        return _PRIOR_RUN.priors(sp, list(prints), span) or None
    except Exception as e:
        log(f"naming prior unavailable ({type(e).__name__}); none applied")
        return None


def context_bars(sp: dict, prints: dict, key, segs) -> dict:
    """context.bars_for this conversation (`speakers.context`, off by default), through the run's
    shared LifeOS lookups; {} when off or on any failure."""
    global _PRIOR_RUN
    try:
        from . import closeness, context
        if not context.settings(sp).get("enabled"):
            return {}
        if _PRIOR_RUN is None:
            _PRIOR_RUN = closeness.PriorRun()
        return context.bars_for(sp, list(prints), key, segs, run=_PRIOR_RUN)
    except Exception as e:
        log(f"context bars unavailable ({type(e).__name__}); none applied")
        return {}


def match_scores(embeddings, names: list, prints: dict, cal, threshold: float, key) -> list:
    """Each embedding's score for the person `names[i]` (the scale `match_threshold` applies to),
    None where that person has no voiceprint or the score isn't finite. Only reads."""
    from . import speakers
    m = speakers.score_matrix(np_stack(embeddings), prints, threshold, cal, exclude=key)
    index = {n.lower(): j for j, n in enumerate(m.names)}
    out = []
    for i, name in enumerate(names):
        j = index.get(name.lower())
        v = float(m.match[i, j]) if j is not None else None
        out.append(v if v is not None and math.isfinite(v) else None)
    return out


def attach_match_scores(diar, prints: dict, cal, threshold: float, key) -> None:
    """Record on each named cluster its score for the person it was named after (the
    scale `match_threshold` applies to). Only reads: naming is already decided."""
    named = [c for c in diar.clusters if c.name]
    if not named or not (prints or cal is not None):
        return
    for c, v in zip(named, match_scores([c.embedding for c in named], [c.name for c in named],
                                        prints, cal, threshold, key)):
        c.match_score = v


def scored(diar, prints: dict, cal, kw: dict) -> None:
    """attach_match_scores, best effort: a failure leaves the scores unset."""
    try:
        attach_match_scores(diar, prints, cal, kw["match_threshold"], kw["exclude"])
    except Exception as e:  # never block transcription
        log(f"match scores unavailable ({type(e).__name__}: {str(e)[:100]})")


def np_stack(rows):
    import numpy as np
    return np.stack([np.asarray(r, dtype=np.float32) for r in rows])


def find_speakers(clip: Path, segs, cfg: dict, strangers=None, con=None, key=None):
    """speakers.Diarization for a clip, or None when disabled or anything fails.
    `strangers` (embeddings the wearer called "a random person") veto voiceprint
    matches they are closer to. With `con`, voices are scored against each person's
    samples and a cohort from the database (calibration.py; `key` is this
    conversation's (device, start_ms)); any failure there falls back to the mean."""
    sp = cfg.get("speakers", {})
    if not sp.get("enabled", True):
        return None
    try:
        from . import speakers
        embedder = speakers.get_embedder(DATA)
        if sp.get("denoise") is True:   # the voice model hears a cleaned copy; Whisper already ran on the clip
            embedder = speakers.DenoisedEmbedder(embedder)
        prints = speakers.load_voiceprints(speakers.voiceprint_dir(DATA))
        cal = None
        if con is not None:
            try:
                from . import calibration
                cal = calibration.load(con, speakers.voiceprint_dir(DATA), sp)
            except Exception as e:
                log(f"calibration unavailable ({type(e).__name__}: {str(e)[:120]}); using raw scores")
        kw = dict(cluster_threshold=float(sp.get("cluster_threshold", 0.25)),
                  match_threshold=float(sp.get("match_threshold", 0.5)), strangers=strangers,
                  match_margin=float(sp.get("match_margin", 0.0)), calibration=cal, exclude=key, prior=naming_prior(sp, prints, key, segs),
                  extra=context_bars(sp, prints, key, segs), **speakers.naming_settings(sp))
        if segs and all(getattr(s, "words", None) for s in segs if s[2].strip()):
            try:
                rescorer = turn_scorer(sp, prints, cal, strangers, key, kw["prior"], con)
                if rescorer is not None:
                    rescorer.extra = kw["extra"]
            except Exception as e:  # e.g. a bad margin in config.json: cluster-level labels
                log(f"turn rescoring unavailable ({type(e).__name__}: {str(e)[:120]})")
                rescorer = None
            try:
                diar = speakers.diarize_words(clip, segs, embedder, prints, **kw, rescorer=rescorer,
                                              boundaries=boundary_settings(sp))
                log_rescore(clip, diar)
                log_boundaries(clip, diar)
                scored(diar, prints, cal, kw)
                return diar
            except Exception as e:  # word turns are best effort; segment turns still work
                log(f"{clip.name}: word-level turns failed ({type(e).__name__}: {str(e)[:120]}); "
                    "using segment-level turns")
        diar = speakers.diarize(clip, segs, embedder, prints, **kw)
        scored(diar, prints, cal, kw)
        return diar
    except Exception as e:  # never block transcription
        log(f"{clip.name}: speaker recognition failed ({type(e).__name__}: {str(e)[:120]}); unlabeled")
        return None


def boundary_settings(sp: dict):
    """speakers.Boundaries from the `speakers` settings. A malformed value falls back to its
    default (and a switch is on only when it is exactly true), so config.json can never stop
    the speaker step."""
    from . import speakers
    d = cfgmod.DEFAULTS["speakers"]

    def num(key, ok, cast=float):
        try:
            v = cast(sp.get(key, d[key]))
        except (TypeError, ValueError):
            return cast(d[key])
        return v if math.isfinite(v) and ok(v) else cast(d[key])
    return speakers.Boundaries(
        min_run_s=num("min_run_s", lambda v: 0 <= v <= 10),
        blip_absorb=sp.get("blip_absorb", d["blip_absorb"]) is True,
        blip_words=num("blip_words", lambda v: 1 <= v <= 20, int),
        blip_margin=num("blip_margin", lambda v: -1 <= v <= 2),
        short_run_s=num("short_run_s", lambda v: 0 <= v <= 10),
        change_points=sp.get("change_points", d["change_points"]) is True,
        cp_side_s=num("cp_side_s", lambda v: 1 <= v <= 10),
        cp_threshold=num("cp_threshold", lambda v: -1 <= v <= 1),
        cp_same=num("cp_same", lambda v: -1 <= v <= 1),
        snap_words=num("snap_words", lambda v: 0 <= v <= 5, int))


def log_boundaries(clip: Path, diar) -> None:
    """One journal line for the turn-boundary passes that ran: counts only."""
    b = getattr(diar, "boundaries", None)
    if not b:
        return
    parts = []
    for name, r in b.items():
        parts.append(f"{name} failed ({r['error']})" if "error" in r else
                     f"{name} " + ", ".join(f"{k} {v}" for k, v in r.items()))
    log(f"{clip.name}: turn boundaries: " + "; ".join(parts))


def turn_scorer(sp: dict, prints: dict, cal, strangers, key, prior=None, con=None):
    """speakers.TurnScorer for this conversation (`speakers.turn_rescore`, on by default),
    or None when turn rescoring is off."""
    if not sp.get("turn_rescore", True):
        return None
    from . import speakers
    d = cfgmod.DEFAULTS["speakers"]
    return speakers.TurnScorer(prints, threshold=float(sp.get("match_threshold", 0.5)), calibration=cal,
                               strangers=strangers, exclude=key,
                               margin=float(sp.get("turn_margin", d["turn_margin"])),
                               margin_calibrated=float(sp.get("turn_margin_calibrated", d["turn_margin_calibrated"])),
                               person_floor=float(sp.get("turn_person_floor", d["turn_person_floor"])),
                               prior=prior, split_cosine=speakers.naming_settings(sp)["split_cosine"],
                               **wearer_settings(sp, key[0] if key else None, con))


def wearer_settings(sp: dict, device: str | None, con=None) -> dict:
    """TurnScorer fields for the near-field wearer signal (wearer.py): the model in
    `speakers.wearer_model` (None when absent or malformed: naming as before), its thresholds,
    and this pendant's stored-turn stats for a pendant the model hasn't seen."""
    from . import wearer
    d = cfgmod.DEFAULTS["speakers"]
    raw = sp.get("wearer_model")
    model = wearer.Model.from_config(raw) if raw else None
    if raw and model is None:
        log("speakers.wearer_model is malformed; wearer signal not applied")
    def num(key, ok=lambda v: True):
        """A wearer setting as a finite float, else None (never raises: no model, no effect)."""
        try:
            v = float(sp.get(key, d[key]))
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) and ok(v) else None
    wp = num("wearer_p", lambda v: 0.5 < v < 1.0)
    strict, strict_cal = num("wearer_strict"), num("wearer_strict_calibrated")
    if wp is None or strict is None or strict_cal is None:
        if model is not None:
            log("speakers.wearer_p (0.5-1), wearer_strict or wearer_strict_calibrated is invalid; "
                "wearer signal not applied")
        model = None
    wp = d["wearer_p"] if wp is None else wp
    strict = d["wearer_strict"] if strict is None else strict
    strict_cal = d["wearer_strict_calibrated"] if strict_cal is None else strict_cal
    dev_stats = None
    if model is not None and con is not None and device and device not in model.devices:
        try:
            rows = con.execute("SELECT level_db, decay_db, flatness FROM speaker_turns WHERE device = ? "
                               "AND level_db IS NOT NULL", (device,)).fetchall()
            dev_stats = wearer.stats(rows) if rows else None
        except sqlite3.Error:
            dev_stats = None
    return dict(wearer=cfgmod.wearer(sp),
                features=sp.get("wearer_features", True) is not False,
                device=device, device_stats=dev_stats, wearer_model=model,
                wearer_p=wp,
                wearer_strict=strict, wearer_strict_calibrated=strict_cal)


def log_rescore(clip: Path, diar) -> None:
    """One journal line per conversation: counts and tags only, never words."""
    r = getattr(diar, "rescore", None)
    if not r:
        return
    if "error" in r:
        log(f"{clip.name}: turn rescoring failed ({r['error']}); cluster-level labels")
        return
    moves = ", ".join(f"{k} x{n}" for k, n in r["moves"].items())
    log(f"{clip.name}: rescored {r['scored']} turns, moved {r['moved']} ({r['moved_s']:.0f}s)"
        f"{f' [{moves}]' if moves else ''}, inherited {r['inherited']}"
        f"{', mixed: ' + ', '.join(r['mixed']) if r['mixed'] else ''} in {r['cpu_s']:.2f}s cpu")
    w = r.get("wearer")
    if w:
        log(f"{clip.name}: wearer " + (w["skipped"] if "skipped" in w else
            ", ".join(f"{t} p={p:.2f}" for t, p in w["p"])
            + (f"; named {', '.join(w['named'])}" if w["named"] else "")
            + (f"; unnamed {', '.join(w['unnamed'])}" if w["unnamed"] else "")))
    if r.get("features_error"):
        log(f"{clip.name}: near-field features unavailable ({r['features_error']})")


def save_clusters(con: sqlite3.Connection, device: str, start_ms: int, clip: Path, diar) -> None:
    import numpy as np
    now = time.time()
    con.executemany(
        "INSERT INTO speaker_clusters (device, start_ms, tag, label, embedding, clip, start_s, end_s, "
        "speech_s, excerpt, labeled_by, created_at, enrolled_in, mixed, wearer_named, match_score) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'',?,?,?)",
        [(device, start_ms, c.tag, c.name, np.asarray(c.embedding, dtype=np.float32).tobytes(),
          str(clip), c.start_s, c.end_s, c.speech_s, c.excerpt, "match" if c.name else None, now,
          int(bool(getattr(c, "mixed", False))), int(bool(getattr(c, "wearer_named", False))),
          getattr(c, "match_score", None) if c.name else None)
         for c in diar.clusters])


def save_turns(con: sqlite3.Connection, device: str, start_ms: int, diar) -> None:
    """The rescored turns' near-field features (`speaker_turns`), when they were measured."""
    turns = (getattr(diar, "rescore", None) or {}).get("turns") or []
    con.execute("SAVEPOINT save_turns")      # all or nothing: a failure keeps the old rows
    try:
        con.execute("DELETE FROM speaker_turns WHERE device=? AND start_ms=?", (device, start_ms))
        con.executemany(
            "INSERT INTO speaker_turns (device, start_ms, start_s, end_s, speech_s, tag, level_db, decay_db, "
            "flatness, score, p) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(device, start_ms, t["start"], t["end"], t["speech_s"], t["voice"], t["level_db"], t["decay_db"],
              t["flatness"], t["score"], t.get("p")) for t in turns])
    except BaseException:
        con.execute("ROLLBACK TO save_turns")
        con.execute("RELEASE save_turns")
        raise
    con.execute("RELEASE save_turns")


def write_event(conv: dict, notes_dir: Path | None = None) -> str | None:
    """Write the Event note for a notable conversation; returns its filename."""
    name = hl.event_filename(conv["device"], conv["start_ms"], conv["title"])
    hl.write_if_changed((notes_dir or NOTES_DIR).parent / "Events" / name, hl.render_event({**conv, "event_file": name}))
    return name


def write_highlights(con: sqlite3.Connection, day: str, notes_dir: Path | None = None) -> Path:
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM conversations WHERE day=?", (day,))]
        stars = [dict(r) for r in con.execute(
            "SELECT s.ts_ms, s.text, c.raw_heading FROM stars s JOIN conversations c "
            "ON c.device = s.conv_device AND c.start_ms = s.conv_start_ms WHERE c.day=?", (day,))]
    finally:
        con.row_factory = None
    path = (notes_dir or NOTES_DIR).parent / "Highlights" / f"{day} Highlights.md"
    hl.write_if_changed(path, hl.render_highlights(day, rows, stars))
    return path


RELINK_DAYS = 2


def vault() -> Path:
    return NOTES_DIR.parent.parent  # the vault (NOTES_DIR is <vault>/Pendant/Raw)


def meeting_for(start_ms: int, end_ms: int, cache: dict):
    """The Granola/Wispr meeting a pendant span belongs to (vault notes for that
    day are loaded once per run)."""
    found = meetings_for(start_ms, end_ms, cache)
    return found[0] if found else None


def meetings_for(start_ms: int, end_ms: int, cache: dict) -> list:
    """Every Granola/Wispr note of the meeting a pendant span belongs to, best first."""
    day = dt.datetime.fromtimestamp(start_ms / 1000, TZ).date()
    if day not in cache:
        try:
            cache[day] = meetings.load(vault(), {day})
        except OSError:
            cache[day] = []
    return meetings.find_all(cache[day], start_ms, end_ms)


CONTINUE_GAP_MS = 5_000        # parts of one meeting: back to back on one pendant ...


def cap_cut(prev: dict, r: dict) -> bool:
    """True when `r` continues `prev`'s audio: back to back (CONTINUE_GAP_MS), or cut by the
    MAX_CONV_S cap, which cuts at the longest pause (under GAP_S) in its last CUT_WINDOW_S."""
    gap = r["start_ms"] - prev["end_ms"]
    if gap < 0 or prev["device"] != r["device"]:
        return False
    return gap <= CONTINUE_GAP_MS or (gap < GAP_S * 1000 and
                                      prev["end_ms"] - prev["start_ms"] >= (MAX_CONV_S - CUT_WINDOW_S) * 1000)
CAL_TODAY_S = 15 * 60          # today's calendar is fetched again at most this often ...
CAL_PAST_S = 60 * 60           # ... a past day's at most hourly
CAL_BUDGET_S = 10.0            # all calendar and people lookups of one run together
CAL_CALL_S = 5.0               # one lookup


def stored_notes(raw, note=None, source=None) -> list[dict]:
    """[{note, source}] from a conversation's meeting_notes column (or its older single link)."""
    try:
        v = json.loads(raw) if raw else None
    except ValueError:
        v = None
    if isinstance(v, list):
        return [n for n in v if isinstance(n, dict) and isinstance(n.get("note"), str)]
    return [{"note": note, "source": source or ""}] if note else []


def note_key(n: dict) -> str:
    """A linked note's identity: its link (the vault path when its name is shared)."""
    return n["note"]


def upgrade_links(notes: list[dict], found: list[dict]) -> list[dict]:
    """Stored name-only links rewritten to the vault path once another note with the same
    name appears (`found` carry the path then): the same source's note first."""
    out = []
    for n in notes:
        if "/" not in n["note"]:
            same = [f for f in found if "/" in f["note"] and f["note"].rsplit("/", 1)[-1] == n["note"]]
            same.sort(key=lambda f: (f.get("source") or "") != (n.get("source") or ""))
            if same:
                n = {**n, "note": same[0]["note"]}
        out.append(n)
    return out


def stored_event(raw) -> dict | None:
    """The conversation's calendar match (calendar_event column), or None ('' = none found)."""
    try:
        v = json.loads(raw) if raw else None
    except ValueError:
        return None
    return v if isinstance(v, dict) and v.get("title") is not None else None


def calendar_line(ev: dict) -> str:
    """The Raw section's `*Calendar: title (HH:MM–HH:MM) · names*` line."""
    def clean(t):
        return " ".join(str(t or "").replace("*", "").split())
    t0 = dt.datetime.fromtimestamp(ev["start_ms"] / 1000, TZ)
    t1 = dt.datetime.fromtimestamp(ev["end_ms"] / 1000, TZ)
    names = ", ".join(clean(a.get("name")) for a in ev.get("attendees") or [] if clean(a.get("name")))
    return f"*Calendar: {clean(ev['title'])} ({t0:%H:%M}–{t1:%H:%M}){f' · {names}' if names else ''}*"


def header_lines(notes: list[dict], ev: dict | None) -> list[str]:
    """The lines under a Raw heading: each meeting note ("Also recorded in", or "Same meeting as"
    for a note that only another part of the meeting overlaps), then the calendar line."""
    from . import transcribe as tx
    out = []
    for n in notes:
        line = tx.meeting_note_line(n["note"], n.get("source") or "")
        out.append(line.replace("*Also recorded in ", "*Same meeting as ", 1) if n.get("via") else line)
    return out + ([calendar_line(ev)] if ev else [])


def _is_header(line: str) -> bool:
    return line.startswith(("*Also recorded in [[", "*Same meeting as [[", "*Calendar: "))


def set_section_header(daily: Path, heading: str, header: list[str]) -> bool:
    """Make the lines right under `## heading` (meeting links, the calendar line) exactly
    `header`. True when the file changed."""
    if not daily.exists():
        return False
    lines = daily.read_text().split("\n")
    for i, line in enumerate(lines):
        if line != f"## {heading}":
            continue
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or _is_header(lines[j])):
            j += 1
        new = [""] + [x for h in header for x in (h, "")]
        if lines[i + 1:j] == new:
            return False
        lines[i + 1:j] = new
        tmp = daily.with_suffix(".md.tmp")
        tmp.write_text("\n".join(lines))
        os.replace(tmp, daily)
        return True
    return False


CALENDAR_SQL = "CREATE TABLE IF NOT EXISTS calendar_days (day TEXT PRIMARY KEY, fetched_at REAL, events TEXT)"


def wearer_label(cfg: dict | None = None) -> str:
    """The wearer's speaker name (`speakers.wearer`, e.g. "Noah Park")."""
    try:
        sp = (cfg or cfgmod.load(DATA)).get("speakers", {})
    except Exception:
        sp = {}
    return str(sp.get("wearer") or attendance.WEARER_NAME)


class CalendarLookup:
    """Calendar events per day and attendee people for one processing run.

    Each day's events are cached in pages.db (`calendar_days`): today's are fetched again
    at most every CAL_TODAY_S, past days' at most every CAL_PAST_S. All LifeOS calls of a
    run share CAL_BUDGET_S; on a failure, a timeout or a spent budget the cached events are
    used, and with none cached the day is unknown (None): stored matches then stand."""

    def __init__(self, events_on=None, people_search=None, me=None, con=None, budget: float = CAL_BUDGET_S,
                 clock=time.monotonic, now=time.time, wearer: str | None = None, my_person_id=None):
        self.events_on = events_on
        self.people_search = people_search
        self.me = me
        self.wearer = wearer
        self.my_person_id = my_person_id     # fn(timeout) -> LifeOS's own person id (the wearer)
        self._me_asked = me is not None
        self.con = con
        self.budget, self.clock, self.now = budget, clock, now
        self.started: float | None = None
        self.days: dict = {}
        self.people: dict[str, dict] = {}
        self.down = False
        self.calls = 0

    def _remaining(self) -> float:
        if self.started is None:
            self.started = self.clock()
        return self.budget - (self.clock() - self.started)

    def _timeout(self) -> float | None:
        """Seconds the next LifeOS call may take, or None when the run's budget is spent."""
        left = self._remaining()
        return None if self.down or left <= 0 else min(CAL_CALL_S, left)

    def _cached(self, day: dt.date):
        if self.con is None:
            return None, None
        try:
            self.con.execute(CALENDAR_SQL)
            row = self.con.execute("SELECT fetched_at, events FROM calendar_days WHERE day=?",
                                   (day.isoformat(),)).fetchone()
        except sqlite3.Error:
            return None, None
        if not row:
            return None, None
        try:
            raw = json.loads(row[1])
            events = [attendance.Event(e["title"], e["start_ms"], e["end_ms"], list(e["attendees"]), e.get("id"),
                                       e.get("with_owner"), e.get("series"))
                      for e in raw]
        except (ValueError, KeyError, TypeError):
            return None, None
        # a day cached before events said whether the wearer was there: fetch it again (kept as a fallback)
        return (row[0] if all("with_owner" in e for e in raw) else 0.0), events

    def _store(self, day: dt.date, events) -> None:
        if self.con is None:
            return
        rows = [{"title": e.title, "start_ms": e.start_ms, "end_ms": e.end_ms, "attendees": e.attendees, "id": e.id,
                 "with_owner": e.with_owner, "series": e.series}
                for e in events]
        try:
            self.con.execute(CALENDAR_SQL)
            self.con.execute("INSERT OR REPLACE INTO calendar_days (day, fetched_at, events) VALUES (?,?,?)",
                             (day.isoformat(), self.now(), json.dumps(rows)))
            self.con.commit()
        except sqlite3.Error:
            pass

    def _fetch(self, day: dt.date, timeout: float):
        self.calls += 1
        if self.events_on is not None:
            return self.events_on(day)
        return attendance.events_on(day, get=lambda path, params: attendance._get(path, params, timeout=timeout))

    def day_events(self, day: dt.date):
        """The day's events (cached or fresh), or None when unknown."""
        if day in self.days:
            return self.days[day]
        fetched, cached = self._cached(day)
        max_age = CAL_TODAY_S if day >= dt.datetime.now(TZ).date() else CAL_PAST_S
        if cached is not None and self.now() - fetched < max_age:
            self.days[day] = cached
            return cached
        timeout = self._timeout()
        events = None
        if timeout is not None:
            try:
                events = self._fetch(day, timeout)
                self._store(day, events)
            except Exception as e:  # LifeOS down or slow: the cache stands, try again next run
                log(f"calendar lookup failed ({type(e).__name__}); {'cached events' if cached is not None else 'none cached'}")
                self.down = True
        self.days[day] = events if events is not None else cached
        return self.days[day]

    def event(self, start_ms: int, end_ms: int):
        """(ok, attendance.Event or None)."""
        events = self.day_events(dt.datetime.fromtimestamp(start_ms / 1000, TZ).date())
        if events is None:
            return False, None
        return True, attendance.event_for(events, start_ms, end_ms)

    def _search(self, q: str, timeout: float) -> list[dict]:
        if self.people_search is not None:
            return self.people_search(q)
        from . import lifeos_people
        return lifeos_people.people(q, timeout=timeout)

    def _self_id(self, timeout: float) -> str | None:
        """LifeOS's own person (asked once per run; None when unknown)."""
        if not self._me_asked:
            self._me_asked = True
            try:
                if self.my_person_id is not None:
                    self.me = self.my_person_id(timeout)
                elif self.people_search is None:
                    from . import lifeos_people
                    self.me = lifeos_people.my_person_id(timeout=timeout)
            except Exception as e:  # the email and name checks still find the wearer
                log(f"LifeOS self lookup failed ({type(e).__name__})")
        return self.me

    def attendees(self, ev) -> list[dict] | None:
        out = []
        for a in ev.attendees:
            if a not in self.people:
                timeout = self._timeout()
                if timeout is None:
                    return None
                me = self._self_id(timeout)
                try:
                    self.people[a] = attendance.resolve_attendees([a], lambda q: self._search(q, timeout), me,
                                                                  self.wearer)
                except Exception as e:
                    log(f"attendee lookup failed ({type(e).__name__})")
                    self.down = True
                    return None
            out += self.people[a]
        return out


def event_json(ev, attendees: list[dict]) -> dict:
    return {"id": ev.id, "title": ev.title, "start_ms": ev.start_ms, "end_ms": ev.end_ms,
            "attendees": attendees, "with_owner": getattr(ev, "with_owner", None),
            "series": getattr(ev, "series", None) or ev.id}


def _same_event(a: dict | None, b: dict | None) -> bool:
    if not a or not b:
        return False
    return (a.get("id") or (a.get("title"), a.get("start_ms"))) == (b.get("id") or (b.get("title"), b.get("start_ms")))


def backfill_presence(con: sqlite3.Connection, cal: "CalendarLookup", max_days: int = 60) -> int:
    """Stored calendar matches from before events said whether the wearer was there get
    `with_owner` and `series` from that day's events (newest days first, within the run's
    calendar budget). An event gone from the calendar gets `with_owner` None (unknown, and not
    looked up again); older than `max_days`: left as it is.
    Returns how many conversations it updated."""
    since = (dt.datetime.now(TZ).date() - dt.timedelta(days=max_days)).isoformat()
    rows = con.execute("SELECT device, start_ms, day, calendar_event FROM conversations WHERE calendar_event "
                       "IS NOT NULL AND calendar_event != '' AND calendar_event NOT LIKE '%\"with_owner\"%' "
                       "AND day >= ? ORDER BY day DESC", (since,)).fetchall()
    done = 0
    for device, start_ms, day, raw in rows:
        ev = stored_event(raw)
        if not ev:
            continue
        events = cal.day_events(dt.date.fromisoformat(day))
        if events is None:
            if cal.down or cal._timeout() is None:
                break
            continue
        hit = next((e for e in events if (ev.get("id") and e.id == ev.get("id"))
                    or (e.title, e.start_ms) == (ev.get("title"), ev.get("start_ms"))), None)
        if hit is None or hit.with_owner is None:
            # gone from the calendar, or LifeOS didn't say: marked unknown (None) so it isn't walked again
            ev.update({"with_owner": None, "series": ev.get("series") or ev.get("id")})
        else:
            ev.update({"with_owner": hit.with_owner, "series": hit.series or hit.id})
        con.execute("UPDATE conversations SET calendar_event=? WHERE device=? AND start_ms=?",
                    (json.dumps(ev), device, start_ms))
        done += 1
    con.commit()
    if done:
        log(f"calendar presence backfilled for {done} conversation{'s' * (done != 1)}")
    return done


def link_meeting_side_effects(con: sqlite3.Connection, device: str, start_ms: int, event_file) -> None:
    """A conversation newly linked to a meeting note: the note owns its follow-ups, so they
    and the Event note go (a star outranks the note: the conversation stays notable)."""
    starred = con.execute("SELECT 1 FROM stars WHERE conv_device=? AND conv_start_ms=?",
                          (device, start_ms)).fetchone()
    if event_file and not starred:
        (NOTES_DIR.parent / "Events" / event_file).unlink(missing_ok=True)
    con.execute("UPDATE conversations SET action_items='[]' WHERE device=? AND start_ms=?", (device, start_ms))
    if not starred:
        con.execute("UPDATE conversations SET notable=0, event_file=NULL WHERE device=? AND start_ms=?",
                    (device, start_ms))
    elif event_file:  # refresh the Event note without the dropped follow-ups
        rewrite_event(con, device, start_ms)


def relink_meetings(con: sqlite3.Connection, cal: CalendarLookup | None = None) -> set[str]:
    """Link recent conversations to their meeting notes and calendar event.

    Meeting notes usually land after the pendant section was written, and a meeting longer
    than MAX_CONV_S is split into parts that each overlap only some of its notes. Each run:
    every recent conversation gets every Granola/Wispr note it overlaps and the calendar
    event that best matches it (attendees resolved to LifeOS people). Consecutive
    conversations on one pendant split from continuous audio (`cap_cut`: back to back, or the
    MAX_CONV_S cap cutting at a short pause), where the earlier one matched a note or an event on its own, are one meeting: every part gets the same event
    and all of the parts' notes. A conversation's first note drops its follow-ups and
    Event note; its Raw section lists every note and a `*Calendar:*` line."""
    cal = cal or CalendarLookup(con=con, wearer=wearer_label())
    since = (dt.datetime.now(TZ).date() - dt.timedelta(days=RELINK_DAYS)).isoformat()
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT device, start_ms, end_ms, day, raw_heading, event_file, meeting_note, meeting_source, "
            "meeting_notes, calendar_event FROM conversations WHERE day >= ? ORDER BY device, start_ms", (since,))]
    finally:
        con.row_factory = None
    cache: dict = {}
    for r in rows:
        r["own_notes"] = [{"note": m.link, "source": m.source, "_m": m}
                          for m in meetings_for(r["start_ms"], r["end_ms"], cache)]
        stored = stored_notes(r["meeting_notes"], r["meeting_note"], r["meeting_source"])
        day_notes = [{"note": m.link, "source": m.source}
                     for m in cache.get(dt.datetime.fromtimestamp(r["start_ms"] / 1000, TZ).date(), [])]
        r["notes"] = upgrade_links(stored, day_notes)
        r["renamed"] = {a["note"]: b["note"] for a, b in zip(stored, r["notes"]) if a["note"] != b["note"]}
        old = stored_event(r["calendar_event"])
        ok, ev = cal.event(r["start_ms"], r["end_ms"])
        if not ok:          # LifeOS down: the stored match stands (unknown when there is none)
            r["known"] = r["calendar_event"] is not None
            r["own_event"] = old if old and not old.get("via") else None
        elif ev is None:
            r["known"], r["own_event"] = True, None
        else:
            fresh = event_json(ev, [])
            if old and not old.get("via") and _same_event(old, fresh) and old.get("title") == ev.title \
                    and len(old.get("attendees") or []) == len(ev.attendees):
                r["known"], r["own_event"] = True, {**old, "with_owner": fresh["with_owner"], "series": fresh["series"]}
            else:
                people = cal.attendees(ev)
                if people is None:
                    r["known"] = r["calendar_event"] is not None
                    r["own_event"] = old if old and not old.get("via") else None
                else:
                    r["known"], r["own_event"] = True, event_json(ev, people)
    # parts of one meeting
    groups: list[list[dict]] = []
    for r in rows:
        prev = groups[-1][-1] if groups else None
        g_event = next((m["own_event"] for m in groups[-1] if m["own_event"]), None) if groups else None
        if prev and cap_cut(prev, r) and any(m["own_notes"] or m["own_event"] for m in groups[-1]) \
                and (r["own_event"] is None or g_event is None or _same_event(r["own_event"], g_event)):
            groups[-1].append(r)
        else:
            groups.append([r])
    changed: set[str] = set()
    for g in groups:
        g_event = next((m["own_event"] for m in g if m["own_event"]), None)
        g_notes: dict[tuple, dict] = {}
        for m in g:
            for n in m["own_notes"]:
                g_notes.setdefault(note_key(n), n)
        for r in g:
            device, start_ms, day, heading = r["device"], r["start_ms"], r["day"], r["raw_heading"]
            own = {note_key(n): n for n in r["own_notes"]}
            have = {note_key(x) for x in r["notes"]}
            # A note that overlaps this part recorded it; one that only another part
            # overlaps is linked "via continuation" and doesn't take this part's follow-ups.
            notes, added = [], []
            for n in r["notes"] + [n for k, n in g_notes.items() if k not in have]:
                k = note_key(n)
                entry = {"note": n["note"], "source": n.get("source") or ""}
                if k not in own and (n.get("via") or k not in have):
                    entry["via"] = "continuation"
                notes.append(entry)
                if k not in have:
                    added.append(n)
            notes.sort(key=lambda x: bool(x.get("via")))     # notes that recorded this part first
            first_own = bool(own) and not any(not x.get("via") for x in r["notes"])
            if r["own_event"]:
                event = r["own_event"]
            elif g_event:
                event = {**g_event, "via": "continuation"}
            else:
                event = None
            want_event = (json.dumps(event) if event else "") if r["known"] or event else r["calendar_event"]
            want_notes = json.dumps(notes) if notes else None
            if want_notes == r["meeting_notes"] and want_event == r["calendar_event"]:
                continue
            if r["meeting_note"] in r["renamed"]:
                con.execute("UPDATE conversations SET meeting_note=? WHERE device=? AND start_ms=?",
                            (r["renamed"][r["meeting_note"]], device, start_ms))
            if first_own:
                link_meeting_side_effects(con, device, start_ms, r["event_file"])
                if not r["meeting_note"]:
                    first = next(n for n in notes if not n.get("via"))
                    con.execute("UPDATE conversations SET meeting_note=?, meeting_source=? WHERE device=? AND start_ms=?",
                                (first["note"], first["source"], device, start_ms))
            con.execute("UPDATE conversations SET meeting_notes=?, calendar_event=? WHERE device=? AND start_ms=?",
                        (want_notes, want_event, device, start_ms))
            if heading and set_section_header(NOTES_DIR / f"{day}.md", heading,
                                              header_lines(notes, stored_event(want_event))):
                changed.add(day)
            for n in added:
                m = own.get(note_key(n), {}).get("_m")
                if m is not None:
                    apply_meeting_names(con, device, start_ms, day, heading, m, cfgmod.load(DATA))
                log(f"{day} {(heading or '')[:5]}: linked to {n['source']} note '{n['note']}'"
                    f"{'' if note_key(n) in own else ' (same meeting)'}")
            if added:
                changed.add(day)
    con.commit()
    return changed


def tx_note(m) -> str:
    from . import transcribe as tx
    return tx.meeting_note_line(m.link, m.source)


def section_bounds(lines: list[str], heading: str) -> tuple[int, int] | None:
    """(first body line, end line) of `## heading` in a daily file's lines."""
    for i, line in enumerate(lines):
        if line == f"## {heading}":
            j = i + 1
            while j < len(lines) and not lines[j].startswith("## "):
                j += 1
            return i + 1, j
    return None


def apply_meeting_names(con: sqlite3.Connection, device: str, start_ms: int, day: str,
                        heading: str, m, cfg: dict) -> dict[str, str]:
    """Name this section's pendant speakers from the meeting's transcript, in
    the vault text and the speaker table. Only names on the consent list (the
    wearer by default) also update a voiceprint; others are text labels."""
    daily = NOTES_DIR / f"{day}.md"
    names = meetings.meeting_names(m, cfgmod.wearer(cfg.get("speakers")))
    if not names or not daily.exists():
        return {}
    lines = daily.read_text().split("\n")
    bounds = section_bounds(lines, heading)
    if not bounds:
        return {}
    body = "\n".join(lines[bounds[0]:bounds[1]])
    mapping = meetings.map_speakers(meetings.section_paragraphs(body, dt.date.fromisoformat(day)), m, names)
    if not mapping:
        return {}
    lines[bounds[0]:bounds[1]] = meetings.rename_tags(body, mapping).split("\n")
    tmp = daily.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines))
    os.replace(tmp, daily)
    allowed = cfg.get("speakers", {}).get("allowed", [])
    for tag, name in mapping.items():
        row = con.execute("SELECT id, embedding, COALESCE(mixed, 0), speech_s FROM speaker_clusters WHERE device=? "
                          "AND start_ms=? AND tag=? AND labeled_by IS NULL", (device, start_ms, tag)).fetchone()
        if not row:
            continue
        label = None
        no_sample = row[2] or not sample_ok(row[3], cfg.get("speakers", {}))
        if name in allowed and no_sample:
            label = name        # a mixed or short voice is named but never folded into a voiceprint
        elif name in allowed:
            try:
                import numpy as np
                from . import speakers
                speakers.enroll(speakers.voiceprint_dir(DATA), name, np.frombuffer(row[1], dtype=np.float32), allowed,
                                cluster_id=row[0])
                label = name
            except Exception as e:  # never block
                log(f"voiceprint update for {name} failed ({type(e).__name__})")
        con.execute("UPDATE speaker_clusters SET tag=?, label=?, labeled_by='meeting', enrolled_in=? WHERE id=?",
                    (name, label, "" if no_sample else label or "", row[0]))
    con.commit()
    log(f"{day} {heading[:5]}: named {', '.join(f'{t} = {n}' for t, n in mapping.items())} from {m.source}")
    return mapping


MAX_ENROLL_MEETING = 3        # people, the wearer included (the wearer's rule)
MIN_OTHER_SHARE = 0.7         # the other attendee's voice must dominate non-wearer speech
MIN_VOICE_S = 20.0            # speech that counts as a distinct voice in the room


def enroll_from_calendar(con: sqlite3.Connection, device: str, start_ms: int, end_ms: int, day: str,
                         heading: str, cfg: dict, cache: dict) -> str | None:
    """Learn a coworker's voice from a small meeting with known attendance.

    Only when the calendar event lists every attendee as a real person and has
    at most MAX_ENROLL_MEETING people. With 2 people, the non-wearer voice is the
    other attendee; with 3, only if one of the two others is already recognized.
    Returns a final outcome to store, or None to retry later (e.g. The wearer's own
    voice isn't identified in this conversation yet)."""
    sp = cfg.get("speakers", {})
    if not sp.get("meeting_enroll", True):
        return "off"
    the_day = dt.date.fromisoformat(day)
    try:
        if the_day not in cache:
            cache[the_day] = attendance.events_on(the_day)
        # the strict rule: inside the booked time (no overrun, no continuation), since this
        # learns a voiceprint without a tap
        ev = attendance.event_within(cache[the_day], start_ms, end_ms)
        if not ev:
            return "no event"
        # an event the wearer wasn't at (someone else's on his calendar, unanswered, declined) or one he
        # muted says nothing about whose voice is whose
        if calendar_hint.event_counts({"with_owner": ev.with_owner, "series": ev.series, "id": ev.id},
                                      calendar_hint.muted_ids(con)) is not True:
            return f"not with them ({ev.title})"
        people = attendance.resolve_people(ev.attendees)
    except Exception as e:  # LifeOS down: try again next run
        log(f"calendar lookup failed ({type(e).__name__})")
        return None
    if people is None:
        return f"attendance unknown ({ev.title})"
    if len(people) > MAX_ENROLL_MEETING or len(people) < 2:
        return f"{len(people)} people ({ev.title})"
    wearer = attendance.WEARER_NAME
    others = [p for p in people if p != wearer]
    clusters = con.execute("SELECT id, tag, label, embedding, speech_s, COALESCE(mixed, 0), labeled_by, label_kind "
                           "FROM speaker_clusters WHERE device=? AND start_ms=?", (device, start_ms)).fetchall()
    voices = [c for c in clusters if (c[4] or 0) >= MIN_VOICE_S]
    if len(voices) > len(people):
        # Someone not on the invite was talking (a doctor, a therapist, a guest):
        # attendance is incomplete, so the voices can't be trusted to the names.
        return f"{len(voices)} voices but {len(people)} invited ({ev.title})"
    first = {p.split()[0] for p in [wearer]}
    wearer_voices = [c for c in clusters if (c[2] or c[1] or "").split()[0] in first]
    if not wearer_voices:
        return None  # retry once the wearer's own voice is identified here
    rest = [c for c in clusters if c not in wearer_voices]
    known = {c[2] for c in rest if c[2] in others}
    remaining = [p for p in others if p not in known]
    # unnamed and never answered: a voice whose name the wearer removed (a stranger) or that he
    # answered otherwise is never the attendee
    unknown = [c for c in rest if not c[2] and c[6] not in ("user", "dismissed")
               and c[7] not in ("stranger", "noise")]
    if len(remaining) != 1 or not unknown:
        return f"ambiguous ({ev.title})"
    top = max(unknown, key=lambda c: c[4] or 0)
    total = sum((c[4] or 0) for c in unknown)
    if total <= 0 or (top[4] or 0) / total < MIN_OTHER_SHARE:
        return f"no dominant voice ({ev.title})"
    if top[5]:
        return f"mixed voice ({ev.title})"     # two people in one voice: never a voiceprint sample
    name = remaining[0]
    import numpy as np
    from . import speakers
    sample = sample_ok(top[4], sp)    # a short voice is named but doesn't teach the voiceprint
    if sample:
        speakers.enroll(speakers.voiceprint_dir(DATA), name, np.frombuffer(top[3], dtype=np.float32), [name],
                        cluster_id=top[0])
    con.execute("UPDATE speaker_clusters SET tag=?, label=?, labeled_by='calendar', enrolled_in=? WHERE id=?",
                (name, name, name if sample else "", top[0]))
    old_tag = top[1]
    daily = NOTES_DIR / f"{day}.md"
    if old_tag and old_tag.startswith("Speaker ") and daily.exists():
        lines = daily.read_text().split("\n")
        b = section_bounds(lines, heading)
        if b:
            body = "\n".join(lines[b[0]:b[1]])
            lines[b[0]:b[1]] = meetings.rename_tags(body, {old_tag: name}).split("\n")
            tmp = daily.with_suffix(".md.tmp")
            tmp.write_text("\n".join(lines))
            os.replace(tmp, daily)
    log(f"{day} {heading[:5]}: learned {name}'s voice from '{ev.title}' ({len(people)} people)")
    return f"enrolled {name} ({ev.title})"


def calendar_pass(con: sqlite3.Connection, cfg: dict, cal: CalendarLookup | None = None) -> set[str]:
    """Try calendar enrollment for recent conversations that haven't settled (with `cal`, the
    run's calendar lookups: no second fetch of a day relink_meetings already has)."""
    since = (dt.datetime.now(TZ).date() - dt.timedelta(days=RELINK_DAYS)).isoformat()
    rows = con.execute("SELECT device, start_ms, end_ms, day, raw_heading FROM conversations "
                       "WHERE calendar_enroll IS NULL AND day >= ?", (since,)).fetchall()
    cache: dict = {d: e for d, e in (cal.days if cal else {}).items() if e is not None}
    changed: set[str] = set()
    for device, start_ms, end_ms, day, heading in rows:
        try:
            outcome = enroll_from_calendar(con, device, start_ms, end_ms, day, heading, cfg, cache)
        except Exception as e:  # never block
            log(f"{day} {heading[:5]}: calendar enrollment failed ({type(e).__name__})")
            outcome = None
        if outcome:
            con.execute("UPDATE conversations SET calendar_enroll=? WHERE device=? AND start_ms=?",
                        (outcome, device, start_ms))
            if outcome.startswith("enrolled"):
                changed.add(day)
    con.commit()
    return changed


STAMPED = re.compile(r"^\[(\d{1,2}):(\d{2})\] ")


def conversation_row(con: sqlite3.Connection, device: str, start_ms: int) -> dict | None:
    con.row_factory = sqlite3.Row
    try:
        r = con.execute("SELECT * FROM conversations WHERE device=? AND start_ms=?", (device, start_ms)).fetchone()
    finally:
        con.row_factory = None
    return dict(r) if r else None


def rewrite_event(con: sqlite3.Connection, device: str, start_ms: int, notes_dir: Path | None = None) -> bool:
    row = conversation_row(con, device, start_ms)
    try:
        name = write_event(row, notes_dir)
    except OSError as e:
        log(f"{row['day']} {row['raw_heading'][:5]}: event note failed ({e})")
        return False
    con.execute("UPDATE conversations SET notable=1, event_file=? WHERE device=? AND start_ms=?",
                (name, device, start_ms))
    return True


def star_conversation(con: sqlite3.Connection, device: str, ts_ms: int):
    """The pressing pendant's written conversation a press belongs to: one whose
    span (plus STAR_GRACE_S either side) holds it, the closest first. Another
    pendant's conversation never takes it (that pendant's audio may still be
    pending), so a press with no such conversation yet stays pending."""
    grace = STAR_GRACE_S * 1000
    rows = con.execute("SELECT device, start_ms, end_ms, day, raw_heading, paragraph_ms FROM conversations "
                       "WHERE device = ? AND start_ms - ? <= ? AND end_ms + ? >= ?",
                       (device, grace, ts_ms, grace, ts_ms)).fetchall()
    return min(rows, key=lambda r: max(r[1] - ts_ms, ts_ms - r[2], 0), default=None)


def reconcile_notable(con: sqlite3.Connection, device: str, start_ms: int, overrides: dict[str, bool],
                      cfg: dict, notes_dir: Path | None = None) -> bool:
    """Make one conversation's notable flag and Event note match what it should
    be now: its notable_overrides.json entry if any, else notable when it has a
    button star, else the notable rules (never for a meeting-linked one).
    Returns True when something changed. Call under the processor's lock."""
    row = conversation_row(con, device, start_ms)
    if row is None:
        return False
    key = hl.event_id(device, start_ms)
    if key in overrides:
        want = overrides[key]
    elif con.execute("SELECT 1 FROM stars WHERE conv_device=? AND conv_start_ms=?", (device, start_ms)).fetchone():
        want = True
    else:
        want = not row["meeting_note"] and hl.is_notable(
            key=key, audio_s=row["audio_s"] or 0, action_items=json.loads(row["action_items"] or "[]"),
            title=row["title"] or "", overview=row["overview"] or "", rules=cfg.get("notable", {}))
    if want and not (row["notable"] and row["event_file"]):
        rewrite_event(con, device, start_ms, notes_dir)
        return True
    if not want and (row["notable"] or row["event_file"]):
        if row["event_file"]:
            ((notes_dir or NOTES_DIR).parent / "Events" / row["event_file"]).unlink(missing_ok=True)
        con.execute("UPDATE conversations SET notable=0, event_file=NULL WHERE device=? AND start_ms=?",
                    (device, start_ms))
        return True
    return False


def star_paragraph(body_lines: list[str], day: str, paragraph_ms: list[int] | None, ts_ms: int) -> int | None:
    """Index into body_lines of the stamped paragraph covering ts_ms: the last one
    starting at or before it (the first, for a press just before the talk began).
    Uses the exact paragraph start times when stored, else the [HH:MM] stamps."""
    idx = [i for i, line in enumerate(body_lines) if STAMPED.match(line)]
    if not idx:
        return None
    if paragraph_ms and len(paragraph_ms) == len(idx):
        times = paragraph_ms
    else:
        the_day = dt.date.fromisoformat(day)
        times = []
        for i in idx:
            m = STAMPED.match(body_lines[i])
            t = dt.datetime.combine(the_day, dt.time(int(m[1]), int(m[2])), TZ)
            if times and t.timestamp() * 1000 < times[-1]:  # past midnight
                t += dt.timedelta(days=1)
            times.append(int(t.timestamp() * 1000))
    return idx[max(bisect.bisect_right(times, ts_ms) - 1, 0)]


def star_text(line: str, limit: int = 200) -> str:
    text = STAMPED.sub("", line, count=1).removeprefix(STAR).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def apply_stars(con: sqlite3.Connection, overrides: dict[str, bool]) -> set[str]:
    """Place every pending star in its written conversation: `⭐ ` after the
    paragraph's [HH:MM] stamp (once, however many presses), the paragraph text
    stored for Highlights, and the conversation made notable (with an Event
    note) unless notable_overrides.json says otherwise. Stars whose conversation
    isn't written yet, or whose Raw section has no stamped paragraph to mark,
    stay pending for a later run. Returns the changed days."""
    changed: set[str] = set()
    pending = con.execute("SELECT device, ts_ms FROM stars WHERE conv_start_ms IS NULL ORDER BY ts_ms").fetchall()
    for device, ts_ms in pending:
        conv = star_conversation(con, device, ts_ms)
        if not conv:
            continue
        c_device, c_start, _end, day, heading, para_json = conv
        text = None
        daily = NOTES_DIR / f"{day}.md"
        lines = daily.read_text().split("\n") if daily.exists() else []
        bounds = section_bounds(lines, heading)
        if bounds:
            body = lines[bounds[0]:bounds[1]]
            k = star_paragraph(body, day, json.loads(para_json) if para_json else None, ts_ms)
            if k is not None:
                stamp = STAMPED.match(body[k])[0]
                if not body[k][len(stamp):].startswith(STAR):
                    body[k] = f"{stamp}{STAR} {body[k][len(stamp):]}"
                    lines[bounds[0]:bounds[1]] = body
                    tmp = daily.with_suffix(".md.tmp")
                    tmp.write_text("\n".join(lines))
                    os.replace(tmp, daily)
                text = star_text(body[k])
        if text is None:  # section or paragraph not there (yet): retry next run
            log(f"{day} {heading[:5]}: no paragraph for the star at {ts_ms}; kept pending")
            continue
        con.execute("UPDATE stars SET conv_device=?, conv_start_ms=?, text=? WHERE device=? AND ts_ms=?",
                    (c_device, c_start, text, device, ts_ms))
        notable, event_file = con.execute("SELECT notable, event_file FROM conversations WHERE device=? AND "
                                          "start_ms=?", (c_device, c_start)).fetchone()
        if overrides.get(hl.event_id(c_device, c_start), True) and not (notable and event_file):
            rewrite_event(con, c_device, c_start)
        log(f"{day} {heading[:5]}: starred at {dt.datetime.fromtimestamp(ts_ms / 1000, TZ):%H:%M:%S}")
        changed.add(day)
    con.commit()
    return changed


def add_note_to_section(daily: Path, heading: str, note: str) -> None:
    """Insert `note` right under `## heading` in a daily file (once)."""
    if not daily.exists():
        return
    lines = daily.read_text().split("\n")
    for i, line in enumerate(lines):
        if line == f"## {heading}":
            if i + 2 < len(lines) and lines[i + 2] == note:
                return
            lines[i + 1:i + 1] = ["", note]
            tmp = daily.with_suffix(".md.tmp")
            tmp.write_text("\n".join(lines))
            os.replace(tmp, daily)
            return


def transcribe_ready(con: sqlite3.Connection) -> int:
    from . import transcribe as tx  # loads the model lazily

    cfg = cfgmod.load(DATA)
    names = pnames.load(DATA)
    overrides = hl.load_overrides(DATA / "notable_overrides.json")
    days: set[str] = set()

    now_ms = time.time() * 1000
    settled = []
    tiny_keys: set = set()
    for (device,) in con.execute("SELECT DISTINCT device FROM chunks").fetchall():
        convs, tiny, _pieces = plan_conversations(con, device, cfg)
        tiny_keys |= {(device, ms) for ms in tiny}
        for i, conv in enumerate(convs):
            # A conversation is over once more audio follows it (after a pause or a
            # length cut), or once the pendant has been quiet for SETTLE_S.
            if i == len(convs) - 1 and now_ms - conv[-1][1] < SETTLE_S * 1000:
                break
            settled.append((device, conv))
    keep = pick_conversations(con, settled)
    meetings_cache: dict = {}
    written = 0
    for k in sorted(range(len(settled)), key=lambda k: settled[k][1][0][1]):
        device, conv = settled[k]
        if k in keep:
            audio_s = len(conv) * FRAME_S
            start = dt.datetime.fromtimestamp(conv[0][1] / 1000, TZ)
            CLIPS.mkdir(parents=True, exist_ok=True)
            clip = CLIPS / f"{device}_{start:%Y%m%d_%H%M%S}.opus"
            breakpoints, pauses = write_clip(conv, clip)
            segs, _info = tx.transcribe(clip)
            if not segs:
                # Voice activity without words (traffic, music, rustling): don't clutter the day.
                log(f"{clip.name}: {audio_s:.0f}s audio, no speech; skipped")
                con.executemany("UPDATE chunks SET done=1 WHERE rowid=?", [(r[0],) for r in conv])
                con.commit()
                continue
            diar = find_speakers(clip, segs, cfg, voices.load_strangers(con), con, (device, conv[0][1]))
            shown = diar if diar and diar.show else None
            paras, spans = paragraphs_with_spans(segs, breakpoints, pauses, shown.labels if shown else None,
                                                 shown.turns if shown else None)
            summary = summarize.summarize("\n\n".join(f"[{t:%H:%M}] {p}" for t, p in paras), cfg, log=log,
                                         con=con)
            kind, importance, kind_reasons = classification(
                summary, "\n".join(p for _t, p in paras), audio_s, getattr(segs, "low_conf_share", 0.0),
                # a mixed voice (two people) is unknown: neither the wearer nor proof he's absent
                [(None, c.speech_s, None) if getattr(c, "mixed", False) else (c.name, c.speech_s, c.embedding)
                 for c in diar.clusters] if diar else [], cfg)
            if (device, conv[0][1]) in tiny_keys:   # a few seconds with no neighbour to join
                kind, importance, kind_reasons = "fragment", 0, [*kind_reasons, TINY_REASON]
            pendant = pnames.name_for(device, names)
            heading = tx.heading_text(start, summary.title, audio_s, pendant)
            found = meetings_for(conv[0][1], conv[-1][1], meetings_cache)
            meeting = found[0] if found else None
            note = None
            if meeting:
                # The Granola/Wispr note owns this meeting's follow-ups and summary.
                summary.action_items = []
                note = "\n\n".join(tx.meeting_note_line(m.link, m.source) for m in found)
            daily = tx.append_to_daily(summary.title, paras,
                                       duration_s=audio_s, recorded_at=start.replace(tzinfo=None),
                                       device_id=pendant, notes_dir=NOTES_DIR, note=note)
            people = sorted({c.name for c in diar.clusters if c.name}) if diar else []
            notable = hl.is_notable(key=hl.event_id(device, conv[0][1]), audio_s=audio_s,
                                    action_items=summary.action_items, title=summary.title,
                                    overview=summary.overview, rules=cfg.get("notable", {}),
                                    overrides=overrides, importance=importance) and not meeting
            row = {"device": device, "start_ms": conv[0][1], "end_ms": conv[-1][1], "audio_s": audio_s,
                   "day": f"{start:%Y-%m-%d}", "pendant": pendant, "title": summary.title,
                   "overview": summary.overview, "action_items": json.dumps(summary.action_items),
                   "category": summary.category, "people": json.dumps(people), "notable": int(notable),
                   "kind": kind, "importance": importance, "kind_reasons": json.dumps(kind_reasons),
                   "raw_heading": heading, "event_file": None, "clip": str(clip),
                   "meeting_note": meeting.link if meeting else None,
                   "meeting_source": meeting.source if meeting else None,
                   "meeting_notes": json.dumps([{"note": m.link, "source": m.source} for m in found]) if found else None,
                   "paragraph_ms": json.dumps([int(t.timestamp() * 1000) for t, _p in paras]),
                   "paragraph_offsets": json.dumps(spans)}
            if notable:
                try:
                    row["event_file"] = write_event(row)
                except OSError as e:
                    log(f"{clip.name}: event note failed ({e})")
            con.execute(f"INSERT OR REPLACE INTO conversations ({', '.join(row)}) "
                        f"VALUES ({', '.join('?' * len(row))})", list(row.values()))
            if diar:
                save_clusters(con, device, conv[0][1], clip, diar)
                try:   # auto matches may join the permanent voice library (rank 3)
                    voice_library.add_auto(con, DATA, cfg, device, conv[0][1], log=log)
                except Exception as e:  # never block transcription
                    log(f"voice library failed ({type(e).__name__}: {str(e)[:100]})")
                try:   # features are a by-product: never at the cost of the conversation
                    save_turns(con, device, conv[0][1], diar)
                except Exception as e:
                    log(f"{clip.name}: speaker turns not stored ({type(e).__name__})")
            for m in found:
                apply_meeting_names(con, device, conv[0][1], row["day"], heading, m, cfg)
            con.execute("INSERT INTO spans VALUES (?,?,?,?)",
                        (device, conv[0][1], conv[-1][1], audio_s))
            days.add(row["day"])
            log(f"{clip.name}: {audio_s:.0f}s audio, {len(segs)} segments, "
                f"{len(diar.clusters) if diar else 0} speakers{' (notable)' if notable else ''} -> {daily.name}")
            written += 1
        con.executemany("UPDATE chunks SET done=1 WHERE rowid=?", [(r[0],) for r in conv])
        con.commit()
    cal = CalendarLookup(con=con, wearer=wearer_label(cfg))
    days |= relink_meetings(con, cal)
    try:
        backfill_presence(con, cal)
    except Exception as e:  # noqa: BLE001 - never blocks anything
        log(f"calendar presence backfill failed ({type(e).__name__})")
    days |= calendar_pass(con, cfg, cal)
    try:
        days |= tasks.spoken_pass(con, NOTES_DIR, cfgmod.wearer(cfg.get("speakers")), log=log)
    except Exception as e:  # never block transcription; retried next run
        log(f"tasks pass failed ({type(e).__name__}: {str(e)[:120]})")
    try:
        days |= apply_stars(con, overrides)
    except Exception as e:  # never block transcription; pending stars retry next run
        log(f"placing stars failed ({type(e).__name__}: {str(e)[:120]})")
        con.commit()
    for day in sorted(days):
        try:
            write_highlights(con, day)
        except OSError as e:
            log(f"highlights for {day} failed ({e})")
    return written


def plan_main(day: str) -> None:
    """`--plan-day YYYY-MM-DD`: print how the day's chunks group without and with the tiny rule.
    Read-only: opens pages.db without migrating it and writes nothing."""
    try:
        dt.datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise SystemExit("--plan-day takes YYYY-MM-DD")
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    try:
        res = plan_day(con, day, cfgmod.load(DATA))
    finally:
        con.close()
    print(json.dumps(res, indent=2))


def main() -> None:
    global _PRIOR_RUN
    _PRIOR_RUN = None            # LifeOS closeness is looked up once per run
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / ".process.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another run is active")
            return
        con = db()
        # Finish or undo a delete/restore that was cut short. Until that has worked, this
        # run changes nothing (transcription waits for the next run; uploads keep landing).
        try:
            trash.recover(con, DATA, NOTES_DIR)
        except Exception as e:
            log(f"trash recovery failed ({type(e).__name__}: {str(e)[:160]}); skipping this run")
            return
        try:  # an undo of a speaker reassignment cut short: its files follow the database
            from . import reassign_undo
            reassign_undo.recover(con, DATA)
        except Exception as e:
            log(f"reassign undo recovery failed ({type(e).__name__}: {str(e)[:120]})")
        left = trash.unrecovered(DATA)
        if left:
            log(f"trash entries need recovery ({', '.join(left)}); skipping this run")
            return
        try:
            trash.purge(con, DATA, NOTES_DIR)
        except Exception as e:  # old trash waits for the next run
            log(f"trash purge failed ({type(e).__name__}: {str(e)[:120]})")
        try:  # old mean/count voiceprint files -> per-cluster .npz (once; no-op after)
            from . import speakers
            speakers.migrate_voiceprints(speakers.voiceprint_dir(DATA))
        except Exception as e:  # never block
            log(f"voiceprint migration failed ({type(e).__name__})")
        ingest(con)
        try:  # voice-sample readings: enroll them, keep their audio out of conversations
            enroll.process(con, DATA, log=log)
        except Exception as e:  # never block transcription
            log(f"enrollments failed ({type(e).__name__}: {str(e)[:120]})")
        n = transcribe_ready(con)
        try:  # the whole voice of each voice named by hand, kept before retention can take its clip
            voice_audio.sync_all(con, DATA, NOTES_DIR, cfgmod.load(DATA), log=log)
        except Exception as e:  # never blocks transcription
            log(f"voice audio failed ({type(e).__name__}: {str(e)[:120]})")
        try:  # daily audio retention (off by default); never blocks transcription
            retention.maybe_run(con, DATA, cfgmod.load(DATA), log=log)
        except Exception as e:
            log(f"retention failed ({type(e).__name__}: {str(e)[:120]})")
        voices.apply_relabels(con, NOTES_DIR)   # Voice Scoring answers the feed couldn't apply
        try:  # the quiet daily signal for Dailies (DeepSeek, today and yesterday); never blocks anything
            day_signals.compute(con, NOTES_DIR, cfgmod.load(DATA), log=log)
        except Exception as e:
            log(f"day signals failed ({type(e).__name__})")
        stats = con.execute("SELECT COUNT(*), COUNT(DISTINCT device) FROM chunks").fetchone()
        log(json.dumps({"transcribed": n, "chunks": stats[0], "devices": stats[1]}))
    # After the lock: LifeOS's likely-present answers for the voice cards' conversations, kept so the
    # cards open without waiting on LifeOS (calendar_hint.warm_present). Never blocks anything.
    try:
        from . import calendar_hint
        con = db()
        try:
            saved = calendar_hint.warm_present(con, log=log)
        finally:
            con.close()
        if saved:
            log(f"likely-present kept for {saved} conversation{'s' * (saved != 1)}")
    except Exception as e:  # noqa: BLE001
        log(f"likely-present warm-up failed ({type(e).__name__})")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--plan-day", metavar="YYYY-MM-DD",
                    help="dry run: count how that day's chunks group with and without the tiny-recording rule")
    args = ap.parse_args()
    plan_main(args.plan_day) if args.plan_day else main()
