"""Voice Scoring: which speaker clusters to ask the wearer about, and what his
answers change.

Confidence. Each cluster is scored against every enrolled person (calibration.py:
top-k sample cosine, s-norm against a cohort; raw top-k cosine when the cohort is
tiny): `similarity` is the best person's raw cosine, `match_score` their calibrated
score and `margin` how far it beats the second best (0 is the second best when there is
only one person). The cluster's `confidence` is min(score, 2 * margin) divided by
twice the threshold for that scale (so the match threshold sits at 0.5), clipped to
0..1: high only when the voice is close to one person and clearly not another. With no
voiceprints it is 0. A cluster closer to a stored stranger than to its best person
has confidence 0 and no voiceprint guess.

Candidates are clusters that pass the voice check and are unlabeled or were
named automatically (`labeled_by` match, meeting or calendar), lowest
confidence first. Clusters whose best score is below
`speakers.new_voice_threshold` (probably someone new; on the calibrated scale it is
scaled by match_threshold_calibrated / match_threshold) are always offered; the
rest share a budget of `speakers.daily_budget` (8) per local day, tracked in
`voice_offers`. Voices whose likely person (best match, at or above the new-voice
cutoff; not the wearer) has fewer than `speakers.min_samples` samples come first: nobody is named
after that person automatically yet, so an answer teaches the most. Clusters close to
a "not speech" exemplar go last and never skip the budget.

Answers. A voiceprint is a running mean of unit embeddings with a count, so a
cluster's sample can be taken back out exactly; `speaker_clusters.enrolled_in`
records which voiceprint a cluster's embedding was folded into ('' for none).
Strangers (Remove name: "back to an unnamed speaker") are stored one row per answer in `strangers`;
they are never merged into one identity and never stop similar voices being
asked about, they only stop those voices being matched to known people.
Transcript relabels are queued in `transcript_relabels` and applied to the
conversation's Raw section under the processor's lock (by the feed when the
lock is free, otherwise by the next processor run). A queued row renames a tag
in the whole section, or (`para_index` set) sets one paragraph's label.
Reassigning a single line (feed.reassign_line) embeds that paragraph's audio span
as a voiceprint sample keyed by its paragraph id (`paragraph_samples`).
Reassigning a voice across all past conversations (feed.bulk_preview / bulk_apply)
queues one relabel per matched voice and records what each voice was before in
`bulk_relabels` (one `batch` per apply). Every reassignment from a transcript has an
undo record (reassign_undo.py): relabels it queues carry its `undo_id`, and applying
them keeps the section's text before and after in `reassign_undo_sections`.
(numpy is imported lazily: process.py imports this module.)
"""
from __future__ import annotations

import datetime as dt
import fcntl
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from . import closeness

TZ = ZoneInfo("America/New_York")
SOMEONE = "Someone"                     # how older transcripts label a stranger (no longer written)
GENERIC = re.compile(r"^Speaker [A-Z]{1,2}$")   # an unnamed voice's tag
AUTO_LABELS = ("match", "meeting", "calendar")
DAILY_BUDGET = 8
NOISE_SIMILARITY = 0.6                  # this close to a "not speech" exemplar = probably noise
MARGIN_WEIGHT = 2.0

# speaker_clusters columns added after the table was created (process.py and
# the feed both migrate with this list)
CLUSTER_COLUMNS = (("voiced", "REAL"), ("noise", "INT DEFAULT 0"),
                   ("enrolled_in", "TEXT"), ("label_kind", "TEXT"), ("mixed", "INT DEFAULT 0"),
                   ("wearer_named", "INT DEFAULT 0"), ("match_score", "REAL"),
                   # how a name the wearer gave reached a voice he didn't tap himself: 'sitting' (his answer
                   # for the whole sitting) or 'bulk' (all past conversations). Such voices are named but are
                   # never voiceprint samples, kept voices or test answers; NULL for voices he named himself.
                   ("named_via", "TEXT"))

SQL = """
CREATE TABLE IF NOT EXISTS strangers (
    id INTEGER PRIMARY KEY, cluster_id INT UNIQUE, embedding BLOB, created_at REAL);
CREATE TABLE IF NOT EXISTS noise_exemplars (
    id INTEGER PRIMARY KEY, cluster_id INT UNIQUE, embedding BLOB, created_at REAL);
CREATE TABLE IF NOT EXISTS voice_offers (
    day TEXT, cluster_id INT, budgeted INT DEFAULT 0, skipped INT DEFAULT 0,
    answered INT DEFAULT 0, PRIMARY KEY (day, cluster_id));
CREATE TABLE IF NOT EXISTS transcript_relabels (
    id INTEGER PRIMARY KEY, device TEXT, start_ms INT, old_tag TEXT, new_tag TEXT,
    created_at REAL, done_at REAL, para_index INT);
CREATE TABLE IF NOT EXISTS paragraph_samples (
    id TEXT PRIMARY KEY, key INT, holder TEXT, embedding BLOB, updated_at REAL);
CREATE TABLE IF NOT EXISTS bulk_relabels (
    id INTEGER PRIMARY KEY, batch TEXT, cluster_id INT, old_tag TEXT, old_label TEXT,
    old_labeled_by TEXT, old_label_kind TEXT, old_enrolled_in TEXT, new_tag TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS reassign_undo (
    id TEXT PRIMARY KEY, created_at REAL, scope TEXT, device TEXT, start_ms INT, name TEXT,
    state TEXT, undone_at REAL);
CREATE TABLE IF NOT EXISTS reassign_undo_sections (
    undo_id TEXT, device TEXT, start_ms INT, day TEXT, heading TEXT, before TEXT, after TEXT,
    PRIMARY KEY (undo_id, device, start_ms));
"""
STRANGER_HOLDER = "#stranger"          # paragraph_samples.holder for a line whose name was removed
# The reassignment being recorded on this thread (reassign_undo.py): relabels queued
# meanwhile carry its id, so their applied sections are kept for its undo.
UNDO = threading.local()


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(SQL)
    have = {r[1] for r in con.execute("PRAGMA table_info(speaker_clusters)")}
    if have:
        for col, typ in CLUSTER_COLUMNS:
            if col not in have:
                con.execute(f"ALTER TABLE speaker_clusters ADD COLUMN {col} {typ}")
    for table, col, typ in (("transcript_relabels", "para_index", "INT"),
                            ("transcript_relabels", "undo_id", "TEXT"),
                            ("conversations", "paragraph_offsets", "TEXT")):
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if have and col not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    con.commit()


def _unit(x: np.ndarray) -> np.ndarray:
    import numpy as np
    x = np.asarray(x, dtype=np.float32)
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-9)


def _embeddings(con: sqlite3.Connection, table: str) -> np.ndarray | None:
    import numpy as np
    try:
        rows = con.execute(f"SELECT embedding FROM {table} ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        return None
    vecs = [np.frombuffer(r[0], dtype=np.float32) for r in rows if r[0]]
    if not vecs or len({len(v) for v in vecs}) != 1:
        return None
    return np.stack(vecs)


def load_strangers(con: sqlite3.Connection) -> np.ndarray | None:
    return _embeddings(con, "strangers")


def load_noise(con: sqlite3.Connection) -> np.ndarray | None:
    return _embeddings(con, "noise_exemplars")


# ---------------------------------------------------------------------------
# scoring and selection (pure)

@dataclass
class Score:
    best_name: str | None     # closest voiceprint (None when there are none, or a stranger is closer)
    similarity: float         # raw cosine to the closest voiceprint (top-k sample cosine when calibrating)
    margin: float             # match_score minus the second best (or minus 0 with one voiceprint)
    confidence: float
    stranger_closer: bool
    noise_like: bool
    match_score: float = 0.0  # what `threshold` applies to: the calibrated s-norm score, else `similarity`
    threshold: float = 0.5    # match cutoff on that scale (speakers.match_threshold[_calibrated])
    calibrated: bool = False

    @property
    def matched(self) -> bool:
        return bool(self.best_name and self.match_score >= self.threshold)


def score(embs: np.ndarray, voiceprints: dict, strangers: np.ndarray | None = None,
          noise: np.ndarray | None = None, noise_similarity: float = NOISE_SIMILARITY, *,
          calibration=None, exclude=None, self_ids=None, threshold: float = 0.5,
          prior: dict | None = None) -> list[Score]:
    """Score each embedding against the enrolled people. With `calibration`
    (calibration.Calibration) a person's score is the mean of their closest
    samples' cosines, normalized against a cohort (see calibration.py); `exclude`
    (a conversation key, or one per row) and `self_ids` (a cluster id per row)
    keep the scored voice out of its own score. Otherwise voiceprints are
    {name: (mean embedding, count)} and `threshold` is the match cutoff. `prior`
    ({name: float}, closeness.py) reorders people who each clear their own cutoff
    (the best is the one with the largest margin plus prior); it never lifts anyone
    over a cutoff."""
    import numpy as np
    n = len(embs)
    if not n:
        return []
    from .speakers import score_matrix
    x = _unit(embs)
    m = score_matrix(x, voiceprints, threshold, calibration, exclude=exclude, self_ids=self_ids)
    names, sims, raw, thr, cal = m.names, m.match, m.raw, m.threshold, m.calibrated
    has = bool(names)
    if has:
        # people are ranked by how far they clear their own cutoff (one person may be on the
        # raw scale while another is calibrated); on one scale this is just the score order
        excess = sims - thr
        order = np.argsort(-closeness.rerank_key(excess, closeness.vector(names, prior)), axis=1)
        rows = np.arange(n)
        best = sims[rows, order[:, 0]]
        thr_b = thr[rows, order[:, 0]]
        second_x = excess[rows, order[:, 1]] if len(names) > 1 else np.full(n, -np.inf)
        second_x = np.maximum(np.where(np.isfinite(second_x), second_x, -np.inf), -thr_b)
    stranger = (x @ _unit(strangers).T).max(axis=1) if strangers is not None and len(strangers) else np.full(n, -np.inf)
    noisy = (x @ _unit(noise).T).max(axis=1) if noise is not None and len(noise) else np.full(n, -np.inf)
    out = []
    for i in range(n):
        if not has or not np.isfinite(best[i]):
            out.append(Score(None, 0.0, 0.0, 0.0, False, bool(noisy[i] >= noise_similarity), 0.0, 0.5, False))
            continue
        j = order[i, 0]
        # like with like: the stranger's cosine against the same top-k score used for matching
        closer = bool(stranger[i] > raw[i, j])
        margin = float(best[i] - thr_b[i] - second_x[i])
        if closer:
            conf = 0.0
        elif cal[i, j]:
            # the match cutoff sits at confidence 0.5, as 0.5 cosine did before
            conf = float(np.clip(min(best[i], MARGIN_WEIGHT * margin) / (2 * thr_b[i]), 0.0, 1.0))
        else:
            conf = float(np.clip(min(best[i], MARGIN_WEIGHT * margin), 0.0, 1.0))
        sim = float(raw[i, j])
        out.append(Score(best_name=None if closer else names[j], similarity=sim, margin=margin, confidence=conf,
                         stranger_closer=closer,
                         noise_like=bool(noisy[i] >= noise_similarity and noisy[i] > sim),
                         match_score=float(best[i]), threshold=float(thr_b[i]), calibrated=bool(cal[i, j])))
    return out


def priority(c: dict) -> tuple:
    """Sort key: noise-like last; voices a sitting or bulk answer named (`reached`: confirming one
    teaches its voiceprint) first, the person with the fewest samples and the longest voice first;
    before any wearer voiceprint exists, the
    wearer suggestions first; then voices whose likely person has a thin voiceprint
    (`thin`: under speakers.min_samples samples, so an answer teaches the most); then
    least confident, newest first."""
    return (bool(c.get("noise_like")), not c.get("reached"), c.get("reached_rank") or (0, 0),
            c.get("suggested") is None, not c.get("thin"), c["confidence"], -c.get("when", 0))


def select(cands: list[dict], offered: dict[int, dict], *, budget: int = DAILY_BUDGET,
           new_voice_threshold: float, limit: int = 50) -> tuple[list[dict], list[tuple[int, int]]]:
    """Today's cards from voice-checked candidates (each with id, similarity,
    confidence, noise_like, when, and optionally `score` and `new_cut`, the match
    score and its own new-voice cutoff on the calibrated scale). `offered` is today's
    voice_offers by cluster id ({"budgeted", "skipped"}). A new voice (score, else
    similarity, below new_cut, else new_voice_threshold, not noise-like) and a mixed voice (turn
    rescoring took most of its speech away) are always offered; anything else
    already offered today is offered again; otherwise it takes a budget slot
    while budget lasts. Skipped clusters (any day: `offered_on`) are left out. Returns (cards in
    priority order, [(cluster id, budgeted)] offers to record)."""
    used = sum(1 for o in offered.values() if o.get("budgeted"))
    out: list[dict] = []
    new: list[tuple[int, int]] = []
    for c in sorted(cands, key=priority):
        if len(out) >= limit:
            break
        o = offered.get(c["id"])
        if o and o.get("skipped"):
            continue
        always = ((c.get("score", c["similarity"]) < c.get("new_cut", new_voice_threshold) or c.get("mixed")
                   or c.get("reached"))
                  and not c.get("noise_like"))
        if o:
            out.append(c)
        elif always:
            out.append(c)
            new.append((c["id"], 0))
        elif used < budget:
            used += 1
            out.append(c)
            new.append((c["id"], 1))
    return out, new


def sample_ok(speech_s, sp: dict) -> bool:
    """Whether a voice with `speech_s` seconds of speech may become a voiceprint sample
    (speakers.min_sample_speech_s; 0 = any). A shorter voice is still named by an answer, a
    meeting or the calendar; it just doesn't teach the voiceprint."""
    from .config import DEFAULTS
    min_s = float(sp.get("min_sample_speech_s", DEFAULTS["speakers"]["min_sample_speech_s"]) or 0)
    return min_s <= 0 or float(speech_s or 0) >= min_s


def prior_enrollment(enrolled_in: str | None, labeled_by: str | None, label: str | None) -> str | None:
    """The voiceprint this cluster's embedding is folded into, if any. Rows
    written before `enrolled_in` existed are inferred: user, calendar and
    meeting labels were enrolled exactly when `label` is set; matches never."""
    if enrolled_in is not None:
        return enrolled_in or None
    if labeled_by in ("user", "calendar", "meeting"):
        return label or None
    return None


# ---------------------------------------------------------------------------
# daily offers

def local_day(now: dt.datetime | None = None) -> str:
    return f"{(now or dt.datetime.now(TZ)).astimezone(TZ):%Y-%m-%d}"


def offered_on(con: sqlite3.Connection, day: str) -> dict[int, dict]:
    """Today's offers by cluster id, plus every cluster skipped on any day (`skipped`, never budgeted
    today): a skip lasts for good, since the wearer skips a voice he doesn't want to tag or a clip
    that is no good to train on, and neither changes overnight."""
    rows = con.execute("SELECT cluster_id, budgeted, skipped, answered FROM voice_offers WHERE day = ?",
                       (day,)).fetchall()
    out = {r[0]: {"budgeted": r[1], "skipped": r[2], "answered": r[3]} for r in rows}
    for (cid,) in con.execute("SELECT DISTINCT cluster_id FROM voice_offers WHERE skipped = 1 AND day != ?", (day,)):
        out.setdefault(cid, {"budgeted": 0, "skipped": 1, "answered": 0})["skipped"] = 1
    return out


def record_offers(con: sqlite3.Connection, day: str, offers: list[tuple[int, int]]) -> None:
    con.executemany("INSERT OR IGNORE INTO voice_offers (day, cluster_id, budgeted) VALUES (?,?,?)",
                    [(day, cid, b) for cid, b in offers])


def mark(con: sqlite3.Connection, day: str, cid: int, field: str) -> None:
    """Set answered or skipped on today's offer row (creating it if needed)."""
    assert field in ("answered", "skipped")
    con.execute("INSERT OR IGNORE INTO voice_offers (day, cluster_id) VALUES (?, ?)", (day, cid))
    con.execute(f"UPDATE voice_offers SET {field} = 1 WHERE day = ? AND cluster_id = ?", (day, cid))


# ---------------------------------------------------------------------------
# transcript relabels

def queue_relabel(con: sqlite3.Connection, cid: int, device: str, start_ms: int,
                  old_tag: str | None, new_tag: str) -> bool:
    """Queue renaming this cluster's paragraphs in its conversation from
    old_tag to new_tag. Not queued when nothing changes, or when another
    cluster of the same conversation shows the same tag (its paragraphs can't
    be told apart, so the text is left alone)."""
    if not old_tag or old_tag == new_tag:
        return False
    shared = con.execute("SELECT COUNT(*) FROM speaker_clusters WHERE device = ? AND start_ms = ? "
                         "AND tag = ? AND id != ?", (device, start_ms, old_tag, cid)).fetchone()[0]
    if shared:
        return False
    con.execute("INSERT INTO transcript_relabels (device, start_ms, old_tag, new_tag, created_at, undo_id) "
                "VALUES (?,?,?,?,?,?)", (device, start_ms, old_tag, new_tag, time.time(), undo_id()))
    return True


_LABEL = re.compile(r"\*\*([^*\n]{1,40}):\*\*")


def _tag_names():
    letters = [chr(c) for c in range(ord("A"), ord("Z") + 1)]
    yield from (f"Speaker {a}" for a in letters)
    yield from (f"Speaker {a}{b}" for a in letters for b in letters)


def fresh_tag(con: sqlite3.Connection, device: str, start_ms: int, section: str | None,
              own: str | None = None, cid: int | None = None) -> str:
    """An unnamed speaker tag ("Speaker X") nothing in this conversation uses: no voice's
    tag, no label in its Raw section text (`section`), no relabel still queued for it.
    With `own` (voice `cid`'s current tag) already such a tag and no other voice
    sharing it, that tag is kept."""
    others = {r[0] for r in con.execute("SELECT tag FROM speaker_clusters WHERE device = ? AND start_ms = ? "
                                        "AND id IS NOT ?", (device, start_ms, cid)) if r[0]}
    if own and GENERIC.match(own) and own not in others:
        return own
    used = set(others) | ({own} if own else set()) | set(_LABEL.findall(section or ""))
    try:
        used |= {r[0] for r in con.execute("SELECT new_tag FROM transcript_relabels WHERE device = ? "
                                           "AND start_ms = ? AND done_at IS NULL", (device, start_ms)) if r[0]}
    except sqlite3.OperationalError:
        pass
    used = {u.lower() for u in used}
    return next(t for t in _tag_names() if t.lower() not in used)


def undo_id() -> str | None:
    """The reassignment being recorded on this thread, if any."""
    return getattr(UNDO, "id", None)


def queue_paragraph_relabel(con: sqlite3.Connection, device: str, start_ms: int, index: int,
                            new_tag: str) -> None:
    """Queue setting paragraph `index` (counting stamped paragraphs) of this
    conversation's Raw section to `**new_tag:**`."""
    con.execute("INSERT INTO transcript_relabels (device, start_ms, old_tag, new_tag, created_at, para_index, "
                "undo_id) VALUES (?,?,?,?,?,?,?)", (device, start_ms, None, new_tag, time.time(), index, undo_id()))


_PARA = re.compile(r"^(\[\d{1,2}:\d{2}\] (?:⭐ )?)(?:\*\*[^*\n]{1,40}:\*\* )?")


def paragraph_lines(lines: list[str]) -> list[int]:
    """Indexes (into `lines`) of the stamped paragraphs."""
    return [i for i, line in enumerate(lines) if _PARA.match(line)]


def label_paragraph(text: str, heading: str, index: int, new_tag: str) -> str:
    """The daily file text with the `index`th stamped paragraph of `## heading`
    labeled `**new_tag:**` (replacing its label, or adding one). Setting the same
    label twice changes nothing more; an unknown section or index changes nothing."""
    from .process import section_bounds
    lines = text.split("\n")
    b = section_bounds(lines, heading)
    if not b:
        return text
    body = lines[b[0]:b[1]]
    idx = paragraph_lines(body)
    if not 0 <= index < len(idx):
        return text
    k = idx[index]
    m = _PARA.match(body[k])
    body[k] = f"{m[1]}**{new_tag}:** {body[k][m.end():]}"
    lines[b[0]:b[1]] = body
    return "\n".join(lines)


def relabel_section(text: str, heading: str, old_tag: str, new_tag: str,
                    skip: frozenset[int] | set[int] = frozenset()) -> str:
    """The daily file text with `**old_tag:**` renamed to `**new_tag:**` inside
    `## heading` only. Applying it twice changes nothing more. Paragraphs whose
    index (among the stamped paragraphs) is in `skip` keep their label: those are
    lines reassigned one by one, which a voice-wide rename must not touch."""
    from .meetings import rename_tags
    from .process import section_bounds
    lines = text.split("\n")
    b = section_bounds(lines, heading)
    if not b:
        return text
    if skip:
        body = lines[b[0]:b[1]]
        for n, k in enumerate(paragraph_lines(body)):
            if n in skip:
                continue
            m = _PARA.match(body[k])
            rest = body[k][m.end():]
            if m.group(0).endswith(f"**{old_tag}:** "):
                body[k] = f"{m[1]}**{new_tag}:** {rest}"
        lines[b[0]:b[1]] = body
        return "\n".join(lines)
    body = "\n".join(lines[b[0]:b[1]])
    lines[b[0]:b[1]] = rename_tags(body, {old_tag: new_tag}).split("\n")
    return "\n".join(lines)


def line_overrides(con: sqlite3.Connection, device: str, start_ms: int, before_id: int | None = None) -> set[int]:
    """Paragraph indices of this conversation that were reassigned one by one
    (queued line relabels, earlier than `before_id` when given). A voice-wide
    relabel leaves them alone."""
    q = ("SELECT DISTINCT para_index FROM transcript_relabels WHERE device = ? AND start_ms = ? "
         "AND para_index IS NOT NULL")
    args: list = [device, start_ms]
    if before_id is not None:
        q += " AND id < ?"
        args.append(before_id)
    return {r[0] for r in con.execute(q, args)}


def section_text(text: str, heading: str) -> str | None:
    """The body of `## heading` (the lines after the heading, up to the next one), or None."""
    from .process import section_bounds
    lines = text.split("\n")
    b = section_bounds(lines, heading)
    return "\n".join(lines[b[0]:b[1]]) if b else None


def _keep_section(con: sqlite3.Connection, undo: str, device: str, start_ms: int, day: str, heading: str,
                  before: str | None, after: str | None) -> None:
    """For a recorded reassignment's undo: the section before its first relabel and after its latest."""
    con.execute("INSERT OR IGNORE INTO reassign_undo_sections (undo_id, device, start_ms, day, heading, before) "
                "VALUES (?,?,?,?,?,?)", (undo, device, start_ms, day, heading, before))
    con.execute("UPDATE reassign_undo_sections SET after = ? WHERE undo_id = ? AND device = ? AND start_ms = ?",
                (after, undo, device, start_ms))


def apply_relabels(con: sqlite3.Connection, notes_dir: Path) -> set[str]:
    """Apply queued relabels in order. Call only while holding the processor's
    lock (the processor appends to the same daily files). Returns the days
    whose Raw file changed. A relabel queued by a recorded reassignment
    (`undo_id`) keeps the section's text before and after it for that undo."""
    try:
        rows = con.execute("SELECT r.id, r.old_tag, r.new_tag, c.day, c.raw_heading, r.para_index, "
                           "r.device, r.start_ms, r.undo_id "
                           "FROM transcript_relabels r "
                           "LEFT JOIN conversations c ON c.device = r.device AND c.start_ms = r.start_ms "
                           "WHERE r.done_at IS NULL ORDER BY r.id").fetchall()
    except sqlite3.OperationalError:
        return set()
    changed: set[str] = set()
    for rid, old, new, day, heading, para, device, start_ms, undo in rows:
        daily = Path(notes_dir) / f"{day}.md" if day else None
        if daily and heading and daily.exists():
            text = daily.read_text()
            out = (label_paragraph(text, heading, para, new) if para is not None
                   else relabel_section(text, heading, old, new, line_overrides(con, device, start_ms, rid)))
            if out != text:
                tmp = daily.with_suffix(".md.relabel.tmp")
                tmp.write_text(out)
                os.replace(tmp, daily)
                changed.add(day)
            if undo:
                _keep_section(con, undo, device, start_ms, day, heading, section_text(text, heading),
                              section_text(out, heading))
        con.execute("UPDATE transcript_relabels SET done_at = ? WHERE id = ?", (time.time(), rid))
    con.commit()
    return changed


def try_apply_relabels(data_dir: Path, notes_dir: Path, con: sqlite3.Connection) -> bool:
    """Apply queued relabels now if the processor isn't running; otherwise its
    next run does. Returns True when applied here."""
    if getattr(UNDO, "locked", False):     # this thread holds the processor's lock (a recorded change)
        apply_relabels(con, notes_dir)
        return True
    lock_path = Path(data_dir) / ".process.lock"
    with open(lock_path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        try:
            apply_relabels(con, notes_dir)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return True
