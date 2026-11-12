"""Kind (conversation | media | fragment) and importance (0-2) of a recording.

The DeepSeek summary proposes both (`summarize.Summary.kind` / `.importance`);
`classify` combines that with local signals under explicit, configurable rules.
Nothing here deletes or hides anything: the app only dims and badges.
Stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass

KINDS = ("conversation", "media", "fragment")

DEFAULTS = {
    "fragment_words": 25,            # fewer words than this: fragment
    "fragment_low_conf_share": 0.4,  # more than this share of segments low confidence: fragment
    "fragment_wpm": 5,               # a recording this sparse (words/min) ...
    "fragment_min_audio_s": 120,     # ... and at least this long: fragment (noise whisper half-heard)
    "model_fragment_words": 80,      # the model says fragment and fewer words than this: fragment
    "media_wearer_max": 0.10,        # model says media and the wearer speaks at most this share: media
    "media_absent_labelled": 0.8,    # The wearer is absent when this share of speech is labelled someone else ...
    "media_unlabelled_sim": 0.4,     # ... or every unlabelled voice scores below this against his voiceprint
    "conversation_wearer_min": 0.25, # The wearer speaks more than this share: never media
    "media_wpm": 150,                # local-only media lean: dense scripted speech (words/min) ...
    "media_words": 150,              # ... of at least this many words, with the wearer absent
}


@dataclass
class Signals:
    words: int
    audio_s: float
    low_conf_share: float = 0.0          # share of whisper segments judged low confidence
    wearer_share: float | None = None    # share of speech time labelled as the wearer; None = voices unknown
    wearer_absent: bool | None = None    # evidence the wearer's voice is not there; None = unknown
    voices: int = 0                      # distinct speaker clusters


def words_per_minute(sig: Signals) -> float:
    return sig.words / (sig.audio_s / 60) if sig.audio_s > 0 else 0.0


def local_importance(summary, sig: Signals) -> int:
    """Importance from local signals alone (no model): 1 for a substantial
    conversation or one with follow-ups, else 0. Never 2: that needs judgment."""
    return 1 if summary.action_items or sig.words >= 400 else 0


def classify(summary, sig: Signals, cfg: dict | None = None) -> tuple[str, int, list[str]]:
    """(kind, importance, reasons). `summary.kind` / `.importance` are the model's
    opinion, or None when the heuristic summarizer produced the summary."""
    t = {**DEFAULTS, **(cfg or {})}
    model_kind = summary.kind if summary.kind in KINDS else None
    model_imp = summary.importance if isinstance(summary.importance, int) and 0 <= summary.importance <= 2 else None
    wearer = sig.wearer_share
    reasons: list[str] = []

    # A real conversation is never dimmed: when signals disagree or are unknown, it is one.
    effective_imp = model_imp if model_imp is not None else local_importance(summary, sig)
    short_is_junk = model_kind == "fragment" or (effective_imp == 0 and not summary.action_items)
    sparse_is_junk = model_kind != "conversation" or sig.wearer_absent is True

    if sig.words < t["fragment_words"] and short_is_junk:
        kind = "fragment"
        reasons.append(f"{sig.words} words (< {t['fragment_words']})")
    elif sig.low_conf_share > t["fragment_low_conf_share"]:
        kind = "fragment"
        reasons.append(f"{sig.low_conf_share:.0%} of segments low confidence")
    elif (sparse_is_junk and sig.audio_s >= t["fragment_min_audio_s"]
          and words_per_minute(sig) < t["fragment_wpm"]):
        kind = "fragment"
        reasons.append(f"{words_per_minute(sig):.1f} words/min over {sig.audio_s / 60:.0f} min (< {t['fragment_wpm']})")
    elif model_kind == "fragment" and sig.words < t["model_fragment_words"]:
        kind = "fragment"
        reasons.append(f"model: fragment, {sig.words} words (< {t['model_fragment_words']})")
    elif model_kind == "media":
        if wearer is not None and wearer > t["conversation_wearer_min"]:
            kind = "conversation"
            reasons.append(f"model: media, overridden: the wearer speaks {wearer:.0%}")
        elif sig.wearer_absent is True and wearer is not None and wearer <= t["media_wearer_max"]:
            kind = "media"
            reasons.append(f"model: media, the wearer absent ({wearer:.0%} of speech)")
        else:
            kind = "conversation"
            reasons.append("model: media, but no evidence the wearer is absent")
    elif (model_kind is None and sig.wearer_absent is True and wearer is not None
          and wearer <= t["media_wearer_max"] and sig.words >= t["media_words"] and words_per_minute(sig) >= t["media_wpm"]):
        kind = "media"
        reasons.append(f"local: dense speech ({words_per_minute(sig):.0f} wpm), the wearer absent")
    else:
        kind = "conversation"
        if model_kind == "fragment":
            reasons.append(f"model: fragment, but {sig.words} words")
        elif model_kind:
            reasons.append(f"model: {model_kind}")

    if kind != "conversation":
        importance = 0   # junk is never important, whatever the model thinks
    elif model_imp is not None:
        importance = model_imp
        if model_imp:
            reasons.append(f"model: importance {model_imp}")
    else:
        importance = local_importance(summary, sig)
        if importance:
            reasons.append("local: substantial or has follow-ups")
    return kind, importance, reasons


def safe_classify(summary, sig: Signals, cfg: dict | None = None, log=print) -> tuple[str, int, list[str]]:
    """classify(), but any failure yields a plain conversation: never blocks transcription."""
    try:
        return classify(summary, sig, cfg)
    except Exception as e:
        log(f"classification failed ({type(e).__name__}); conversation, importance 0")
        return "conversation", 0, ["classification failed"]


def evidence(clusters, wearer: str, cfg: dict | None = None) -> tuple[float | None, bool | None, int]:
    """(wearer's labelled share of speech, whether the wearer is provably absent, voice
    count) from [(label, speech_s, similarity_to_wearer_voiceprint_or_None), ...].

    Unlabelled speech is unknown, not absent: the wearer is absent only when at least
    `media_absent_labelled` of the speech is labelled as someone else, or the wearer's
    labelled share is within `media_wearer_max` and every unlabelled voice is known to
    score below `media_unlabelled_sim` against his voiceprint. No voices, or no speech
    time: (None, None, n)."""
    t = {**DEFAULTS, **(cfg or {})}
    clusters = [(l, s or 0, sim) for l, s, sim in clusters]
    total = sum(s for _l, s, _m in clusters)
    if not clusters or total <= 0:
        return None, None, len(clusters)
    is_wearer = lambda l: (l or "").lower() == wearer.lower()
    mine = sum(s for l, s, _m in clusters if is_wearer(l))
    other = sum(s for l, s, _m in clusters if l and not is_wearer(l))
    unlabelled = [m for l, _s, m in clusters if not l]
    share = mine / total
    absent = (other / total >= t["media_absent_labelled"]
              or (share <= t["media_wearer_max"]
                  and all(m is not None and m < t["media_unlabelled_sim"] for m in unlabelled)))
    return share, absent, len(clusters)
