"""Settings from `~/pendant-direct-data/config.json`, merged over defaults.

{
  "notes_dir": "~/Pendant/Raw",             # where daily notes go (see notes_dir())
  "lifeos_url": "http://localhost:8000",    # LifeOS web app, if you run one (optional)
  "summarizer": "fireworks" | "heuristic",
  "notable": {"duration_minutes": 25, "action_items_min": 2, "keywords": []},
  "speakers": {"enabled": true, "allowed": ["Your Name"], "wearer": "Your Name",
               "people": {"Alex": "<LifeOS entity_id>"}, "closeness": {"weight": 0.1},
               "cluster_threshold": 0.25, "match_threshold": 0.55,
               "match_threshold_calibrated": 1.5, "scoring": "mean",
               "turn_rescore": true, "turn_margin": 0.1, "turn_margin_calibrated": 1.0,
               "turn_person_floor": 0.4, "wearer_p": 0.9, "wearer_strict": 0.65,
               "wearer_strict_calibrated": 2.5, "wearer_model": null}
}

Stdlib only: the feed imports it.
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

DEFAULTS: dict = {
    "summarizer": "fireworks",
    # Estimated DeepSeek cost shown in the feed status, USD per million tokens (Fireworks standard).
    "llm": {"usd_per_m_prompt": 0.30, "usd_per_m_completion": 1.20},
    "notable": {"duration_minutes": 25, "action_items_min": 2, "keywords": []},
    # Tiny recordings (process.merge_tiny): a conversation under tiny_audio_s seconds joins its
    # neighbour on the same pendant within tiny_join_gap_s seconds, else it is a fragment. 0 = off.
    "grouping": {"tiny_audio_s": 20, "tiny_join_gap_s": 120},
    # Audio retention (retention.py): off until the wearer turns it on. Keeps `days` days of audio and,
    # with keep_starred, starred recordings forever; transcripts and voiceprints are never touched.
    "retention": {"enabled": False, "days": 7, "keep_starred": True},
    # Thresholds for classify.classify (kind and importance); see classify.DEFAULTS.
    "classify": {"fragment_words": 25, "fragment_low_conf_share": 0.4, "fragment_wpm": 5,
                 "fragment_min_audio_s": 120, "model_fragment_words": 80,
                 "media_wearer_max": 0.10, "media_absent_labelled": 0.8,
                 "media_unlabelled_sim": 0.4, "conversation_wearer_min": 0.25,
                 "media_wpm": 150, "media_words": 150},
    "speakers": {
        "enabled": True,
        # Consent gate: only these names may get a voiceprint.
        "allowed": [],
        # Whose voice to suggest for the dominant speaker before any voiceprint exists.
        "wearer": "",
        # Cosine similarity: segments merge into one speaker at or above this.
        "cluster_threshold": 0.25,
        # Cosine similarity between a speaker and a voiceprint needed to name them.
        "match_threshold": 0.55,
        # A voice is only named after the person who beats every other person's score for it
        # by at least this much (cosine); a weak or ambiguous match stays "Speaker X". 0 = off.
        "match_margin": 0.0,
        # Each name goes to at most one voice per conversation. With a cosine here (0.5 suits the
        # cluster-level eval), a voice may also share a name already given when its centroid is within
        # it of every voice holding the name (one person's voice split in two by the clustering); it
        # must still clear the person on its own. Turn rescoring lets an unnamed voice's turns join
        # that person on the same condition. null = never (the default: on the hand-labelled lines it
        # names more lines wrongly).
        "split_voice_cosine": None,
        # Nobody is named automatically (voiceprint matching, turn rescoring) after a person whose
        # voiceprint has fewer samples than this; they still count against other people's margin, keep
        # getting hints and Who's this cards, and come first in Voice Scoring. 0 = off.
        "min_samples": 2,
        # A voice with less speech than this (seconds) is named by an answer, a meeting or the calendar
        # but never becomes a voiceprint sample; `python -m pendant_direct.sample_hygiene` takes
        # existing short samples out. 0 = off (the default: at 20 s the hand-labelled lines get more
        # wrong names).
        "min_sample_speech_s": 0,
        # context.py: a person unexpected in the setting (work vs personal) needs a higher raw
        # score to be named (work_off for a work contact outside work, personal_in_meeting for
        # family or a friend in a meeting they aren't on). Off by default.
        "context": {"enabled": False, "work_off": 0.05, "personal_in_meeting": 0.03,
                    "work_start": 9, "work_end": 18},
        # How a voice is scored against a voiceprint: "mean" (cosine to the mean embedding,
        # match_threshold), "topk" (mean of the 3 closest samples' cosines, match_threshold)
        # or "calibrated" (top-k with s-norm against a cohort, match_threshold_calibrated).
        "scoring": "mean",
        # The calibrated cutoff, used while a person's cohort has at least 5 voices;
        # match_threshold is the fallback below that.
        "match_threshold_calibrated": 1.5,
        # After clustering, each speaker turn with at least 2 s of speech is scored on its
        # own against the conversation's voices and the voiceprints (with the scorer above)
        # and moves when another voice beats its own by this margin: cosine units for
        # "mean" and "topk" (and calibrated people on the raw fallback), s-norm units for
        # calibrated scores. A voice keeping under 60% of its speech is marked mixed.
        "turn_rescore": True,
        "turn_margin": 0.1,
        "turn_margin_calibrated": 1.0,
        # A turn of a voice named after someone moves to another person when it beats the first
        # by the margin and has at least this raw cosine with them (one turn scores lower
        # against a voiceprint than a whole voice does).
        "turn_person_floor": 0.4,
        # Near-field wearer signal (wearer.py): each rescored turn's level above the noise
        # floor, energy decay and spectral flatness, stored in `speaker_turns`. A logistic model
        # over those and the wearer's score (python -m pendant_direct.eval_speakers --fit-wearer
        # --save) goes in wearer_model; null = no model, naming as before. With a model, a voice
        # whose wearer probability is >= wearer_p is named the wearer, and one at or below
        # 1 - wearer_p loses his name unless its score for him reaches wearer_strict (cosine) or
        # wearer_strict_calibrated (s-norm, calibrated scoring).
        "wearer_features": True,
        "wearer_p": 0.9,
        "wearer_strict": 0.65,
        "wearer_strict_calibrated": 2.5,
        "wearer_model": None,
        # Turn boundaries (speakers.Boundaries; python -m pendant_direct.eval_speakers --turns).
        # A voice change with less speech than min_run_s is absorbed. blip_absorb: a run of at
        # most blip_words words between two runs of one other voice, or with under short_run_s of
        # speech, joins its neighbour unless the windows mostly inside it favour its own voice by
        # blip_margin (cosine); with no such window it is kept. change_points (off): inside a run,
        # where the cp_side_s of windows before a point and after it are less alike than
        # cp_threshold, the run splits; a voice's split pieces are grouped at cp_same, the group
        # most like the voice keeps it and the others join a voice they match or become new ones.
        # snap_words (off): a voice change moves up to this many words to just after a sentence end,
        # comma or pause when where it fell has none (0 = off).
        # denoise (off): spectral gating of the audio the voice model hears (never what Whisper hears).
        "min_run_s": 1.0,
        "blip_absorb": True,
        "blip_words": 3,
        "blip_margin": 0.15,
        "short_run_s": 1.5,
        "change_points": False,
        "cp_side_s": 2.0,
        "cp_threshold": 0.3,
        "cp_same": 0.5,
        "snap_words": 0,
        "denoise": False,
        # Speaker name -> LifeOS entity_id (python -m pendant_direct.link_people, or picking a
        # LifeOS person in the app). Metadata only: it never allows a name or makes a voiceprint.
        "people": {},
        # Naming prior from closeness (closeness.py): added to the match margin, only to order
        # people who each clear their own threshold. weight * strength/100, weight_calendar when
        # they are on the calendar event at that time, minus weight_age * min(days, 365)/365.
        "closeness": {"weight": 0.10, "weight_calendar": 0.10, "weight_age": 0.05},
        # Voice Scoring: a voice less similar than this to every voiceprint is
        # probably someone new and is always offered...
        "new_voice_threshold": 0.5,
        # ...the rest share this many cards per local day.
        "daily_budget": 8,
        # This close to a "not speech" exemplar sends a voice to the back of the queue.
        "noise_similarity": 0.6,
    },
}


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


DEFAULT_NOTES_DIR = Path.home() / "Pendant" / "Raw"
DEFAULT_WEARER = "Me"     # The wearer's speaker name until `speakers.wearer` is set


def wearer(sp: dict | None) -> str:
    """The wearer's speaker name (`speakers.wearer`), else "Me"."""
    return str((sp or {}).get("wearer") or "").strip() or DEFAULT_WEARER


def wearer_first(sp: dict | None) -> str:
    """How prompts refer to the wearer: the first word of `speakers.wearer`, else "the wearer"."""
    w = str((sp or {}).get("wearer") or "").strip()
    return w.split()[0] if w else "the wearer"


def data_dir() -> Path:
    """The data folder: PENDANT_DATA, else ~/pendant-direct-data."""
    return Path(os.environ.get("PENDANT_DATA") or Path.home() / "pendant-direct-data").expanduser()


def notes_dir(data: Path | None = None) -> Path:
    """Where the daily transcript notes go (a folder in a Markdown vault, e.g. Obsidian's):
    PENDANT_NOTES_DIR, else `notes_dir` in the data folder's config.json, else ~/Pendant/Raw.
    Event notes and highlights go to its siblings (../Events, ../Highlights)."""
    env = os.environ.get("PENDANT_NOTES_DIR")
    if env:
        return Path(env).expanduser()
    try:
        cfg = json.loads((Path(data or data_dir()) / "config.json").read_text())
        if isinstance(cfg, dict) and isinstance(cfg.get("notes_dir"), str) and cfg["notes_dir"].strip():
            return Path(cfg["notes_dir"]).expanduser()
    except (OSError, ValueError):
        pass
    return DEFAULT_NOTES_DIR


def load(data_dir: Path) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    try:
        over = json.loads((Path(data_dir) / "config.json").read_text())
        if isinstance(over, dict):
            _merge(cfg, over)
    except (OSError, ValueError):
        pass
    return cfg


SCORING_MODES = ("mean", "topk", "calibrated")


def scoring_mode(cfg: dict) -> str:
    """`speakers.scoring`, "mean" when missing or unknown."""
    m = cfg.get("scoring", DEFAULTS["speakers"]["scoring"])
    return m if m in SCORING_MODES else "mean"


def effective_match_threshold(cfg: dict) -> float:
    """The cutoff the matcher applies on the scale of `speakers.scoring`: the s-norm threshold for
    "calibrated", `match_threshold` (a mean or top-k cosine) otherwise."""
    sp = DEFAULTS["speakers"]
    if scoring_mode(cfg) == "calibrated":
        return float(cfg.get("match_threshold_calibrated", sp["match_threshold_calibrated"]))
    return float(cfg.get("match_threshold", 0.5))


ENTITY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z '-]{0,39}$")


def clean_name(name: str) -> str:
    """A speaker name as stored (trimmed, title-cased). Raises ValueError when invalid."""
    name = " ".join(str(name or "").split())
    if not _NAME_RE.match(name):
        raise ValueError("invalid name")
    return name.title()


def _read_raw(path: Path) -> dict:
    if path.exists():
        # Never rewrite a config we can't read: that would erase other settings.
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("config.json is not an object")
        return raw
    return {}


def _write_raw(path: Path, raw: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(raw, indent=2) + "\n")
    os.replace(tmp, path)


def linked_entity(people: dict | None, name: str) -> str | None:
    """The LifeOS entity_id linked to `name` (case-insensitive), or None."""
    for k, v in (people or {}).items():
        if str(k).lower() == str(name).lower() and isinstance(v, str) and v:
            return v
    return None


class LinkConflict(ValueError):
    """The name is already linked to a different LifeOS person."""


def link_people(data_dir: Path, links: dict[str, str], replace: bool = False) -> None:
    """Merge {name: entity_id} into `speakers.people` in config.json, atomically. A name
    already linked to a different entity raises LinkConflict unless `replace` (an explicit
    relink). Raises ValueError for an unreadable config or an invalid id."""
    for v in links.values():
        if not isinstance(v, str) or not ENTITY_RE.match(v):
            raise ValueError("invalid entity_id")
    path = Path(data_dir) / "config.json"
    raw = _read_raw(path)
    sp = raw.get("speakers")
    if not isinstance(sp, dict):
        sp = raw["speakers"] = {}
    people = sp.get("people")
    if not isinstance(people, dict):
        people = sp["people"] = {}
    if not replace:
        for name, eid in links.items():
            have = linked_entity(people, name)
            if have and have != eid:
                raise LinkConflict(name)
    for name, eid in links.items():
        for k in [k for k in people if str(k).lower() == name.lower()]:
            del people[k]
        people[name] = eid
    _write_raw(path, raw)


def allow_name(data_dir: Path, name: str) -> str:
    """Add name (trimmed, title-cased; letters, spaces, ' and -; 1-40 chars) to
    `speakers.allowed` in config.json, atomically, and return the stored
    spelling (an existing case-insensitive match is returned unchanged).
    Raises ValueError for an invalid name."""
    name = clean_name(name)
    path = Path(data_dir) / "config.json"
    if path.exists():
        # Never rewrite a config we can't read: that would erase other settings.
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("config.json is not an object")
    else:
        raw = {}
    sp = raw.get("speakers")
    if not isinstance(sp, dict):
        sp = raw["speakers"] = {}
    allowed = sp.get("allowed")
    if not isinstance(allowed, list):
        allowed = list(DEFAULTS["speakers"]["allowed"])
    for a in allowed:
        if str(a).lower() == name.lower():
            return str(a)
    sp["allowed"] = [*allowed, name]
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(raw, indent=2) + "\n")
    os.replace(tmp, path)
    return name
