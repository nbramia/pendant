#!/usr/bin/env python3
"""Pendant ingest endpoint for the iPhone app.

PUT /pendant/ingest/<segment-name> with `Authorization: Bearer <token>` and
`X-Content-SHA256`. The body is written to a temp file, fsync'd, checked
against the hash, then renamed into the spool. Only then does the server
answer 200 with the hash, which is the phone's signal to delete its copy.
Re-uploading an identical segment is a no-op 200.

Stdlib only. Binds to localhost; Tailscale Serve exposes it on the tailnet.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PREFIX = "/pendant/ingest/"
NAME_RE = re.compile(r"^[A-Za-z0-9_.:+-]{1,128}\.(pmsg|log)$")
MAX_BYTES = 64 * 1024 * 1024

DEFAULT_SPOOL = Path.home() / "pendant-direct-data" / "spool"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "pendant-direct" / "token"


def load_token(token_file: Path) -> str:
    token = token_file.read_text().strip() if token_file.exists() else ""
    if not token:
        sys.exit(f"no token in {token_file}; ingestion disabled")
    return token


class Handler(BaseHTTPRequestHandler):
    token = ""
    spool = DEFAULT_SPOOL

    def _reply(self, code: int, body: str) -> None:
        data = (body + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self) -> None:  # noqa: N802
        auth = self.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {self.token}"):
            return self._reply(401, "unauthorized")
        if not self.path.startswith(PREFIX):
            return self._reply(404, "not found")
        name = self.path[len(PREFIX):]
        if not NAME_RE.match(name):
            return self._reply(400, "bad name")
        want = (self.headers.get("X-Content-SHA256") or "").lower()
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if not re.fullmatch(r"[0-9a-f]{64}", want) or not 0 <= length <= MAX_BYTES:
            return self._reply(400, "need X-Content-SHA256 and Content-Length")

        final = self.spool / name
        if final.exists() and hashlib.sha256(final.read_bytes()).hexdigest() == want:
            return self._reply(200, want)

        tmp = self.spool / f".{name}.part"
        h = hashlib.sha256()
        remaining = length
        with open(tmp, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1 << 16))
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
                remaining -= len(chunk)
            f.flush()
            os.fsync(f.fileno())
        if remaining or h.hexdigest() != want:
            tmp.unlink(missing_ok=True)
            return self._reply(422, "hash mismatch or short body")
        os.replace(tmp, final)
        dfd = os.open(self.spool, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        self.log_message("stored %s (%d B)", name, length)
        return self._reply(200, want)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/pendant/health":
            return self._reply(200, "ok")
        return self._reply(404, "not found")


def make_server(spool: Path, token: str, port: int = 9795) -> ThreadingHTTPServer:
    """A server bound to 127.0.0.1:port (0 = any free port) storing into spool."""
    spool.mkdir(parents=True, exist_ok=True)
    handler = type("BoundHandler", (Handler,), {"spool": spool, "token": token})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main() -> None:
    env = os.environ.get
    spool = Path(env("PENDANT_SPOOL", DEFAULT_SPOOL)).expanduser()
    token = load_token(Path(env("PENDANT_TOKEN_FILE", DEFAULT_TOKEN_FILE)).expanduser())
    make_server(spool, token, int(env("PENDANT_INGEST_PORT", "9795"))).serve_forever()


if __name__ == "__main__":
    main()
