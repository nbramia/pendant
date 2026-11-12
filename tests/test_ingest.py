"""Tests for the ingest HTTP endpoint, run against a real server on an ephemeral port."""
from __future__ import annotations

import hashlib
import http.client
import threading

import pytest

from pendant_direct import ingest

TOKEN = "test-token-123"


@pytest.fixture
def server(tmp_path):
    spool = tmp_path / "spool"
    srv = ingest.make_server(spool, TOKEN, port=0)
    srv.RequestHandlerClass.log_message = lambda *a, **k: None
    t = threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True)
    t.start()
    yield srv.server_address[1], spool
    srv.shutdown()
    srv.server_close()


def request(port, method, path, body=b"", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    out = resp.status, resp.read().decode().strip()
    conn.close()
    return out


def put(port, name, body, *, token=TOKEN, sha=None):
    headers = {"Authorization": f"Bearer {token}"}
    if sha is not False:
        headers["X-Content-SHA256"] = sha or hashlib.sha256(body).hexdigest()
    return request(port, "PUT", ingest.PREFIX + name, body, headers)


def test_bad_token_is_401(server):
    port, spool = server
    assert put(port, "DEV_20260101T000000Z.pmsg", b"x", token="wrong")[0] == 401
    assert list(spool.iterdir()) == []


@pytest.mark.parametrize("name", ["../evil.pmsg", "seg.txt", "has%20space.pmsg", "x" * 200 + ".pmsg"])
def test_bad_name_is_400(server, name):
    port, spool = server
    assert put(port, name, b"x") == (400, "bad name")
    assert list(spool.iterdir()) == []


def test_bad_extension_is_400(server):
    port, _ = server
    assert put(port, "DEV_20260101T000000Z.wav", b"x") == (400, "bad name")


def test_missing_hash_is_400(server):
    port, spool = server
    assert put(port, "DEV_20260101T000000Z.pmsg", b"abc", sha=False)[0] == 400
    assert put(port, "DEV_20260101T000000Z.pmsg", b"abc", sha="nothex")[0] == 400
    assert list(spool.iterdir()) == []


def test_hash_mismatch_is_422_and_leaves_nothing(server):
    port, spool = server
    status, _ = put(port, "DEV_20260101T000000Z.pmsg", b"abc", sha="0" * 64)
    assert status == 422
    assert list(spool.iterdir()) == []


def test_good_upload_echoes_sha_and_is_stored(server):
    port, spool = server
    body = bytes(range(256)) * 300
    sha = hashlib.sha256(body).hexdigest()
    assert put(port, "DEV_20260101T000000Z.pmsg", body) == (200, sha)
    stored = spool / "DEV_20260101T000000Z.pmsg"
    assert stored.read_bytes() == body
    assert [p.name for p in spool.iterdir()] == [stored.name]  # no .part left


def test_identical_reupload_is_200(server):
    port, spool = server
    body = b"segment bytes"
    sha = hashlib.sha256(body).hexdigest()
    assert put(port, "DEV_20260101T000000Z.pmsg", body) == (200, sha)
    assert put(port, "DEV_20260101T000000Z.pmsg", body) == (200, sha)
    assert (spool / "DEV_20260101T000000Z.pmsg").read_bytes() == body


def test_log_files_accepted(server):
    port, spool = server
    assert put(port, "DEV_20260101T000000Z.log", b"hello")[0] == 200
    assert (spool / "DEV_20260101T000000Z.log").exists()


def test_health(server):
    port, _ = server
    assert request(port, "GET", "/pendant/health") == (200, "ok")
    assert request(port, "GET", "/elsewhere")[0] == 404
