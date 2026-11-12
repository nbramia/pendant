"""Friendly pendant names ("old", "new") instead of 16-hex device IDs.

They come from `~/pendant-direct-data/pendants.json` ({"HEX": "name"}); a pendant
without one shows its ID. Stdlib only: the feed imports it.
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULTS: dict[str, str] = {}


def load(data_dir: Path) -> dict[str, str]:
    """{DEVICE_HEX: name}, defaults merged with pendants.json (bad file = defaults)."""
    names = dict(DEFAULTS)
    try:
        extra = json.loads((Path(data_dir) / "pendants.json").read_text())
        if isinstance(extra, dict):
            names.update({str(k).upper(): str(v).strip() for k, v in extra.items() if str(v).strip()})
    except (OSError, ValueError):
        pass
    return names


def name_for(device: str, names: dict[str, str]) -> str:
    """The pendant's name, or its ID when it has none."""
    return names.get(device.upper(), device)
