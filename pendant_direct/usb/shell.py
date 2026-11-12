"""Minimal driver for the Limitless pendant's Zephyr shell over USB CDC-ACM.

Reads end on the `uart:~$` prompt; the port is reopened if the pendant
re-enumerates (it resets periodically). /dev/ttyACM0 is root:dialout, so the
user must be in `dialout`.
"""
import re
import sys
import time

import serial

PORT = "/dev/ttyACM0"
BAUD = 115200
PROMPT = b"uart:~$"
ANSI = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]|\x1b\[\?[0-9]+[hl]|\x08|\x1b\[[0-9]*[JKD]")

def clean(b: bytes) -> str:
    b = ANSI.sub(b"", b)
    return b.replace(b"\r", b"").decode("utf-8", "replace")

class Shell:
    def __init__(self, port=PORT, baud=BAUD, timeout=0.4):
        self.port, self.baud, self.timeout = port, baud, timeout
        self.s = serial.Serial(port, baud, timeout=timeout)
    def _reopen(self):
        try:
            self.s.close()
        except Exception:
            pass
        # wait for the port to re-enumerate after a device reset
        for _ in range(40):
            try:
                self.s = serial.Serial(self.port, self.baud, timeout=self.timeout)
                time.sleep(2.0)  # let the Zephyr shell settle
                return True
            except Exception:
                time.sleep(0.5)
        return False

    def cmd(self, c: str, wait=1.2, _retry=2) -> str:
        try:
            self.s.reset_input_buffer()
            self.s.write((c + "\r\n").encode())
            self.s.flush()
            buf = b""; hard = time.time() + max(wait, 4.0)
            while time.time() < hard:
                chunk = self.s.read(4096)
                if chunk:
                    buf += chunk
                # the shell prompt reappears once the command's output is complete —
                # stop immediately instead of waiting out a fixed timeout (big speedup)
                if b"uart:~$" in buf[-80:] and len(buf) > len(c) + 4:
                    break
        except (serial.SerialException, OSError):
            if _retry > 0 and self._reopen():
                return self.cmd(c, wait=wait, _retry=_retry - 1)
            raise
        out = clean(buf)
        # drop the echoed command + trailing prompt
        lines = [l for l in out.split("\n")]
        return "\n".join(lines).strip()
    def close(self): self.s.close()

if __name__ == "__main__":
    sh = Shell()
    cmds = sys.argv[1:] or ["kernel version"]
    for c in cmds:
        print(f"\n===== `{c}` =====")
        print(sh.cmd(c, wait=1.6))
    sh.close()
