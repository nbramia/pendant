"""Watch for the pendant on USB and run the USB drain once per plug-in.

Polls for /dev/ttyACM0 belonging to the pendant (USB 2fe3:0110), waits for
the Zephyr shell to settle, runs `pendant_direct.usb.drain`, and re-arms when
the pendant is unplugged. Runs as a `systemctl --user` service.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

PORT = Path("/dev/ttyACM0")
VENDOR, PRODUCT = "2fe3", "0110"
POLL_S = 2.0
BOOT_GRACE_S = 4.0  # let the Zephyr CDC-ACM shell settle after enumeration


def _read(p: Path) -> str:
    try:
        return p.read_text().strip().lower()
    except OSError:
        return ""


def is_pendant_present() -> bool:
    if not PORT.exists():
        return False
    # Walk up the sysfs device chain looking for the USB vendor/product.
    dev = Path("/sys/class/tty/ttyACM0/device").resolve()
    for _ in range(8):
        v, p = dev / "idVendor", dev / "idProduct"
        if v.exists() and p.exists():
            return _read(v) == VENDOR and _read(p) == PRODUCT
        if dev.parent == dev:
            break
        dev = dev.parent
    return True  # port exists but couldn't verify vendor — assume it's ours


def run_transfer() -> None:
    # A subprocess, so a crash or hang can't take the watcher down. The drain is
    # resumable: unplugging mid-way leaves a cursor for the next plug-in.
    rc = subprocess.run([sys.executable, "-m", "pendant_direct.usb.drain"]).returncode
    print(f"[watch] drain exited rc={rc}", flush=True)


def main() -> int:
    print("[watch] pendant watcher started; waiting for USB plug-in...", flush=True)
    armed = True  # ready to fire on next appearance
    present = is_pendant_present()
    if present:
        print("[watch] pendant already connected — transferring now", flush=True)
        time.sleep(BOOT_GRACE_S)
        run_transfer()
        armed = False
    while True:
        time.sleep(POLL_S)
        now = is_pendant_present()
        if now and armed:
            print("[watch] pendant connected", flush=True)
            time.sleep(BOOT_GRACE_S)
            run_transfer()
            armed = False
        elif not now:
            if not armed:
                print("[watch] pendant removed; re-armed", flush=True)
            armed = True


if __name__ == "__main__":
    raise SystemExit(main())
