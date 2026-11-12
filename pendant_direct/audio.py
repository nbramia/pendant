"""Conversation audio for the app's player.

AVPlayer cannot play Ogg/Opus, so the feed serves a cached AAC/M4A transcode of the
conversation's clip (`<data>/audio_cache/<clip stem>.m4a`, built on first request with
PyAV, so the feed must run from .venv). The clip itself is never touched; the cache is
disposable and is dropped whenever its clip is gone.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

CACHE_DIR = "audio_cache"
BITRATE = 40_000
RATE = 24_000
_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()
_SLOT = threading.Semaphore(1)   # one transcode at a time: it is CPU-bound and the box runs the pipeline


def clip_path(data_dir: Path, clip: str | None) -> Path | None:
    """The clip file under <data>/clips (by file name only), or None if it is not on disk."""
    if not clip:
        return None
    p = Path(data_dir) / "clips" / Path(clip).name
    return p if p.is_file() else None


def cache_path(data_dir: Path, clip: Path) -> Path:
    return Path(data_dir) / CACHE_DIR / (clip.stem + ".m4a")


def transcode(src: Path, dst: Path) -> None:
    """Ogg/Opus -> mono AAC in an MP4 container with the index up front (faststart), so a player can start before it has the whole file. A damaged tail is dropped, not fatal."""
    import av
    tmp = dst.with_name(dst.name + ".tmp")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        with av.open(str(src)) as inp, av.open(str(tmp), "w", format="mp4", options={"movflags": "+faststart"}) as out:
            ins = inp.streams.audio[0]
            rate = RATE   # speech: 24 kHz mono encodes about twice as fast as 48 kHz
            st = out.add_stream("aac", rate=rate)
            st.layout = "mono"
            st.bit_rate = BITRATE
            rs = av.AudioResampler(format="fltp", layout="mono", rate=rate)
            frames = 0
            try:
                for frame in inp.decode(ins):
                    for f in rs.resample(frame):
                        frames += 1
                        for p in st.encode(f):
                            out.mux(p)
            except av.error.InvalidDataError:
                # Real clips can end in a damaged page: keep what decoded (the start of the file
                # keeps its timeline, so line offsets stay right), but a clip with nothing is an error.
                if not frames:
                    raise
            for f in rs.resample(None):
                for p in st.encode(f):
                    out.mux(p)
            for p in st.encode(None):
                out.mux(p)
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


def playable(data_dir: Path, clip: str | None) -> Path | None:
    """The cached M4A for a conversation's clip (transcoded now if needed), None if the clip is gone."""
    src = clip_path(data_dir, clip)
    if src is None:
        return None
    dst = cache_path(data_dir, src)
    if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    with _GUARD:
        lock = _LOCKS.setdefault(str(dst), threading.Lock())
    with lock:
        with _SLOT:   # other conversations wait here; cached ones never reach this point
            if not (dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime):
                transcode(src, dst)
    return dst


RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | str:
    """(first, last) inclusive for a satisfiable single range, None for no/ignored header, "bad" for 416."""
    if not header:
        return None
    m = RANGE.match(header.strip())
    if not m or (m[1] == "" and m[2] == ""):
        return None   # multi-range or malformed: ignore and serve the whole file (RFC 9110)
    if m[1] == "":
        n = int(m[2])
        return "bad" if n == 0 or size == 0 else (max(0, size - n), size - 1)
    first = int(m[1])
    last = min(int(m[2]), size - 1) if m[2] else size - 1
    return "bad" if first >= size or last < first else (first, last)
