#!/usr/bin/env python3
"""Decode spooled .pmsg segments (u32-LE length + PendantAllMsg records) into
Ogg Opus files plus a page summary, for spot checks:

  .venv/bin/python -m pendant_direct.pmsg SEGMENT.pmsg [...] -o OUTDIR
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from .decode import iter_audio_chunks_from_messages
from .keys import load_keyset
from .oggopus import OggOpusWriter
from .process import records


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("segments", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("decoded"))
    ap.add_argument("--device", help="device hex id, for decryption keys (default: from file name)")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for seg in args.segments:
        device = args.device or seg.name.split("_")[0]
        keys = load_keyset(device)
        kinds = Counter()
        indices = []
        out = args.out / (seg.stem + ".opus")
        w = OggOpusWriter(out)
        for chunk in iter_audio_chunks_from_messages(records(seg), keys=keys):
            kinds[chunk.ingest_type] += 1
            indices.append(chunk.page_index)
            if chunk.opus:
                w.add_packet(chunk.opus)
        w.close()
        span = f"{min(indices)}..{max(indices)}" if indices else "none"
        print(f"{seg.name}: chunks by ingest type {dict(kinds)}, page indices {span}, "
              f"distinct pages {len(set(indices))} -> {out}")


if __name__ == "__main__":
    main()
