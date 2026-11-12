"""Local speech-to-text and the daily vault file.

faster-whisper large-v3-turbo on CPU (int8), the same model linux-whisper uses;
the CT2 build is in the Hugging Face cache. Nothing leaves the machine.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from . import config as _config

MODEL_ID = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
NOTES_DIR = _config.notes_dir()

_model = None


def get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(MODEL_ID, device="cpu", compute_type="int8",
                              cpu_threads=min(8, os.cpu_count() or 4))  # share the box with other services
    return _model


class Seg(tuple):
    """A whisper segment: unpacks as (start_s, end_s, text) like a plain tuple and
    also carries `.words`, a tuple of (start_s, end_s, word) from word timestamps
    (empty when the segment came without them)."""

    def __new__(cls, start, end, text, words=()):
        self = super().__new__(cls, (start, end, text))
        self.words = tuple(words)
        return self


class Segments(list):
    """The kept segments, plus `low_conf_share`: the share of whisper's non-empty
    segments dropped as low confidence (see `believable`)."""
    low_conf_share = 0.0


def transcribe(path: Path, language: str = "en"):
    """Return (segments, info); segments = [Seg(start_s, end_s, text), ...], each
    unpackable as (start_s, end_s, text) and carrying its word timings."""
    # Measured on a real 22-minute pendant conversation (2026-11-08):
    # - No second VAD pass: the pendant already records only on voice, and
    #   Silero VAD kept 272 of 1308 s, dropping quiet far-field speech
    #   (755 words -> 1217 words without it).
    # - Not conditioning on previous text, plus a repetition penalty and n-gram
    #   blocking, stops whisper's loops ("I'm so upset" x4): 13 repeated
    #   sentences -> 3.
    raw, info = get_model().transcribe(
        str(path),
        language=language,
        beam_size=5,
        vad_filter=False,
        condition_on_previous_text=False,
        repetition_penalty=1.1,
        no_repeat_ngram_size=4,
        compression_ratio_threshold=2.0,
        no_speech_threshold=0.5,
        word_timestamps=True,   # speaker turns are split at the word where the voice changes
    )
    raw = [s for s in raw if s.text.strip()]
    kept = [Seg(s.start, s.end, s.text.strip(),
                [(w.start, w.end, w.word.strip()) for w in (s.words or []) if w.word.strip()])
            for s in raw if believable(s.text, s.avg_logprob)]
    out = Segments(within_audio(kept, getattr(info, "duration", None)))
    out.low_conf_share = 1 - len(kept) / len(raw) if raw else 0.0
    return out, info


# Whisper's stock outputs for noise and silence.
STOCK = {"you", "thank you", "thanks", "thanks for watching", "thank you for watching", "bye", "okay"}
MIN_LOGPROB = -1.0       # hallucinated segments measured -1.01..-1.37; real speech median -0.5
STOCK_MIN_LOGPROB = -0.6


def believable(text: str, avg_logprob: float) -> bool:
    """Drop low-confidence segments (whisper inventing speech in rustle and room
    noise) and stock phrases unless confidently heard."""
    if avg_logprob < MIN_LOGPROB:
        return False
    norm = text.strip().lower().strip(".!?, ")
    return not (norm in STOCK and avg_logprob < STOCK_MIN_LOGPROB)


def within_audio(segs, duration):
    """Drop segments whisper places after the audio ends (hallucinations such as
    "Thank you." on trailing silence) and clamp the rest to the audio."""
    if not duration:
        return segs
    return [Seg(s, min(e, duration), t,
                [(ws, min(we, duration), w) for ws, we, w in getattr(seg, "words", ()) if ws < duration])
            for seg in segs for s, e, t in [seg] if s < duration - 0.05]


def daily_path(day: dt.date, notes_dir: Path = NOTES_DIR) -> Path:
    return notes_dir / f"{day:%Y-%m-%d}.md"


def heading_text(recorded_at: dt.datetime, title: str, duration_s: float,
                 device: str | None = None) -> str:
    """`HH:MM — Title (XmYYs) · device`: a section heading without the `## `,
    which is also what [[YYYY-MM-DD#...]] links point at."""
    d = duration_s
    return (f"{recorded_at:%H:%M} — {title} ({int(d // 60)}m{int(d % 60):02d}s)"
            f"{f' · {device}' if device else ''}")


def meeting_note_line(link: str, source: str) -> str:
    return f"*Also recorded in [[{link}]] ({source.capitalize()}).*"


def append_to_daily(title: str, paragraphs, *, duration_s: float, recorded_at: dt.datetime,
                    device_id: str | None = None, notes_dir: Path = NOTES_DIR,
                    note: str | None = None) -> Path:
    """Append one conversation to <notes_dir>/YYYY-MM-DD.md, creating
    the file with frontmatter and a date heading if it's new.

    paragraphs: [(wall-clock datetime, text), ...]; each is written as
    `[HH:MM] text` so pauses and long stretches show their time. device_id is
    what follows the `·` in the heading (the pendant's name)."""
    notes_dir.mkdir(parents=True, exist_ok=True)
    daily = daily_path(recorded_at.date(), notes_dir)
    parts = []
    if not daily.exists():
        parts.append(
            f"---\ndate: {recorded_at:%Y-%m-%d}\nsource: pendant\ntype: transcript\n---\n"
            f"# {recorded_at:%A, %B %-d, %Y}\n\n"
            "Pendant recordings via the Pendant iPhone app, transcribed locally "
            "(faster-whisper large-v3-turbo).\n")
    parts.append(f"## {heading_text(recorded_at, title, duration_s, device_id)}\n")
    if note:
        parts.append(note + "\n")
    body = "\n\n".join(f"[{t:%H:%M}] {text}" for t, text in paragraphs)
    parts.append((body or "_(no speech detected)_") + "\n")
    parts.append("---\n")
    with daily.open("a") as f:
        f.write("\n".join(parts) + "\n")
    return daily
