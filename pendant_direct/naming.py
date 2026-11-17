"""The naming space: which model, and which audio, a voice is compared with voiceprints in.

The processor names in ECAPA windows: a voice's 1.5 s window embeddings averaged, the clustering's
own. The offline eval (`eval_speakers --spaces`, and line mode with `--set embedder=NAME
naming=window|whole`) can name in another space: `embedder` picks the model (`ecapa` or a
WeSpeaker model from embedders.py) and `naming` the audio, `window` or `whole` (a voice's speech
joined into pieces of up to 20 s, each embedded as one utterance; see speakers.Namer).
Clustering and turn boundaries always use the ECAPA windows. Voiceprints belong to one space, so
the eval rebuilds them in another from each hand-named voice's audio (`voice_audio`): its
voice-library clip, else its conversation clip cut at its stored snippet.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np

from . import embedders, speakers

DEFAULT = ("ecapa", "window")


def space(sp: dict) -> tuple[str, str]:
    """(embedder, naming) from eval settings (`embedder`, `naming`; default ECAPA windows), checked."""
    name = str(sp.get("embedder", DEFAULT[0]))
    kind = str(sp.get("naming", DEFAULT[1]))
    if name not in embedders.NAMES:
        raise ValueError(f"speakers.embedder {name!r}: one of {', '.join(embedders.NAMES)}")
    if kind not in ("window", "whole"):
        raise ValueError(f"speakers.naming {kind!r}: window or whole")
    return name, kind


def label(sp_or_space) -> str:
    name, kind = space(sp_or_space) if isinstance(sp_or_space, dict) else sp_or_space
    return f"{name}/{kind}"


def model(name: str, data: Path, ecapa=None, models: Path | None = None):
    """The embedder for `name`: ECAPA (given, else speakers.get_embedder) or a verified WeSpeaker model
    (from `models`, else <data>/models)."""
    if name == "ecapa":
        return ecapa or speakers.get_embedder(data)
    if models is not None:
        return embedders.WeSpeakerEmbedder(Path(models) / embedders.MODELS[name].file, name=name)
    return embedders.load(data, name)


def namer(sp: dict, data: Path, ecapa=None, models: Path | None = None, wrap=None):
    """speakers.Namer for the configured space; None for ECAPA windows (naming as the clustering
    sees it). `wrap(embedder, tag)` may wrap the model (the eval's cache)."""
    name, kind = space(sp)
    if (name, kind) == DEFAULT:
        return None
    emb = None
    if name != "ecapa":
        emb = model(name, data, ecapa, models)
        emb = wrap(emb, name) if wrap else emb
    elif kind == "window":
        emb = None
    return speakers.Namer(embedder=emb, whole=kind == "whole")


def library_clips(data: Path) -> dict[int, Path]:
    """{cluster id: its voice-library clip}."""
    from . import voice_library
    out = {}
    for idx in voice_library.root(data).glob("*/index.json"):
        try:
            entries = json.loads(idx.read_text())
        except (OSError, ValueError):
            continue
        for e in entries:
            src = str(e.get("src", ""))
            f = idx.parent / f"{voice_library.clip_id(src)}.opus"
            if src.startswith("cluster:") and f.exists():
                out[int(src.split(":", 1)[1])] = f
    return out


def voice_audio(con: sqlite3.Connection, data: Path, ids, lib: dict | None = None) -> dict[int, np.ndarray]:
    """{cluster id: 16 kHz audio} for the voices whose audio is still kept: the voice-library clip,
    else the conversation clip cut at the voice's stored snippet (start_s..end_s)."""
    lib = library_clips(data) if lib is None else lib
    out: dict[int, np.ndarray] = {}
    cut = []
    for cid in ids:
        if cid in lib:
            out[cid] = speakers.load_audio(lib[cid])
            continue
        row = con.execute("SELECT clip, start_s, end_s FROM speaker_clusters WHERE id = ?", (cid,)).fetchone()
        if row and row[0] and (Path(data) / "clips" / Path(row[0]).name).exists():
            cut.append((str(Path(data) / "clips" / Path(row[0]).name), float(row[1]), float(row[2]), cid))
    last, a = None, None
    for path, s, e, cid in sorted(cut):        # one conversation clip decoded at a time
        if path != last:
            last, a = path, speakers.load_audio(Path(path))
        seg = a[int(s * speakers.SAMPLE_RATE):int(e * speakers.SAMPLE_RATE)]
        if len(seg) >= speakers.SAMPLE_RATE:
            out[cid] = seg
    return out


def embed_audio(emb, audio: np.ndarray, whole: bool) -> np.ndarray:
    """A voice's unit embedding from its audio: one utterance (whole), or the mean of 1.5 s windows
    every 0.75 s (window)."""
    if whole:
        return speakers.normalize(np.asarray(emb.embed_wavs([audio]), dtype=np.float32)[0])
    dur = len(audio) / speakers.SAMPLE_RATE
    starts = np.arange(0.0, max(dur - speakers.WINDOW_S, 0.0) + 1e-9, speakers.HOP_S)
    spans = [(float(t), float(min(t + speakers.WINDOW_S, dur))) for t in starts]
    e = speakers.normalize(np.asarray(emb.embed_audio(audio, spans), dtype=np.float32))
    return speakers.normalize(e.mean(0))
