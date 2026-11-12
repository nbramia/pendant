"""Pendant health alerts: one Telegram message per pendant when its battery
drops to 10% or below, and one when its memory is 90% full.

The phone reports each connected pendant's battery and page counts to
`POST /pendant/feed/health`. Each alert is a per-pendant, per-kind state
machine persisted in DATA/alerts.json:

  battery: armed -> send at <=10%, disarm; re-arm at >=30%
  memory:  armed -> send at >=90% used, disarm; re-arm at <50% used

A failed send leaves the alert armed, so the next report retries it. Stdlib
only: the feed imports it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable

from . import names as pnames

from . import config as _config

BATTERY_ALERT_PCT = 10
BATTERY_REARM_PCT = 30
MEMORY_ALERT_PCT = 90
MEMORY_REARM_PCT = 50

# A script you provide that sends its one argument as an alert (Telegram, email, ...):
# PENDANT_NOTIFY_SCRIPT, else `notify_script` in config.json; without one, alerts aren't sent.
NOTIFY_SCRIPT = os.environ.get("PENDANT_NOTIFY_SCRIPT") or str(_config.load(_config.data_dir()).get("notify_script") or "")
DEVICE_RE = re.compile(r"[0-9A-Fa-f]{16}")
_lock = threading.Lock()


class BadReport(ValueError):
    pass


def default_sender(message: str) -> bool:
    """Send via the notify script; True only if it exited 0 (False without one: stays armed)."""
    if not NOTIFY_SCRIPT:
        return False
    try:
        r = subprocess.run(["/usr/bin/python3", os.path.expanduser(NOTIFY_SCRIPT), message],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:  # noqa: BLE001 - a failed send just stays armed
        return False


def _opt_int(v, lo: int, hi: int | None, field: str) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < lo or (hi is not None and v > hi):
        raise BadReport(f"bad {field}")
    return v


def parse_report(body) -> list[dict]:
    """Validate {"pendants": [{device, battery, free_pages, total_pages}]}."""
    if not isinstance(body, dict) or not isinstance(body.get("pendants"), list):
        raise BadReport("pendants must be a list")
    out = []
    for p in body["pendants"]:
        if not isinstance(p, dict):
            raise BadReport("pendant must be an object")
        dev = p.get("device")
        if not isinstance(dev, str) or not DEVICE_RE.fullmatch(dev):
            raise BadReport("bad device")
        total = _opt_int(p.get("total_pages"), 0, None, "total_pages")
        free = _opt_int(p.get("free_pages"), 0, None, "free_pages")
        if total is not None and free is not None and free > total:
            raise BadReport("free_pages exceeds total_pages")
        out.append({"device": dev.upper(), "battery": _opt_int(p.get("battery"), 0, 100, "battery"),
                    "free_pages": free, "total_pages": total})
    return out


def _load(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.replace(tmp, path)


def label(device: str, names: dict[str, str]) -> str:
    n = pnames.name_for(device, names)
    return f"{n.capitalize()} pendant" if n != device else f"Pendant {device[-4:]}"


def used_pct(free: int | None, total: int | None) -> float | None:
    if free is None or total is None or total <= 0:
        return None
    return (total - free) * 100 / total


def _step(armed: bool, value: float | None, fire: bool, rearm: bool,
          message: str, sender: Callable[[str], bool]) -> tuple[bool, bool]:
    """Returns (new armed state, whether a message went out)."""
    if value is None:
        return armed, False
    if armed and fire:
        if sender(message):
            return False, True
        return True, False
    if not armed and rearm:
        return True, False
    return armed, False


def process(data_dir: Path, reports: list[dict], sender: Callable[[str], bool] | None = None) -> list[str]:
    """Apply reports to the alert state; returns the messages actually sent."""
    sender = sender or default_sender
    path = Path(data_dir) / "alerts.json"
    names = pnames.load(data_dir)
    sent: list[str] = []
    with _lock:
        state = _load(path)
        before = json.dumps(state, sort_keys=True)
        for r in reports:
            st = state.setdefault(r["device"], {})
            who = label(r["device"], names)
            b = r["battery"]
            st["battery_armed"], did = _step(
                bool(st.get("battery_armed", True)), b,
                b is not None and b <= BATTERY_ALERT_PCT, b is not None and b >= BATTERY_REARM_PCT,
                f"{who} battery at {b}%", sender)
            if did:
                sent.append(f"{who} battery at {b}%")
                _save(path, state)  # disarm on disk before anything else can fail
            u = used_pct(r["free_pages"], r["total_pages"])
            msg = f"{who} memory {u:.0f}% full" if u is not None else ""
            st["memory_armed"], did = _step(
                bool(st.get("memory_armed", True)), u,
                u is not None and u >= MEMORY_ALERT_PCT, u is not None and u < MEMORY_REARM_PCT,
                msg, sender)
            if did:
                sent.append(msg)
                _save(path, state)
        if json.dumps(state, sort_keys=True) != before:
            _save(path, state)
    return sent
