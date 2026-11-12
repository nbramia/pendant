"""Decryption keys for pendants provisioned with our own server key.

A pendant the app talks to records plaintext Opus, so normally there is no key.
Pendants once provisioned over USB (`crypto gen_keys`, see an earlier prototype)
keep encrypting until factory reset; their key files live outside git in
~/pendant-direct-data/keys/<DEVICE_HEX_ID>.json (mode 600).
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from . import crypto

KEYS_DIR = Path.home() / "pendant-direct-data" / "keys"


def load_keyset(device_id: str | None, keys_dir: Path = KEYS_DIR):
    if not device_id:
        return None
    kf = keys_dir / f"{device_id}.json"
    if not kf.exists():
        return None
    d = json.loads(kf.read_text())
    return crypto.keyset_from_existing(
        d["server_priv_pem"].encode(),
        base64.b64decode(d["device_pub_b64"]),
    )
