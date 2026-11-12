"""pendant_direct.health: alert thresholds, hysteresis, per-pendant state, retry,
and the authenticated /pendant/feed/health endpoint."""
from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from pendant_direct import feed, health

OLD, NEW = "0F1E2D3C4B5A6978", "A1B2C3D4E5F60718"


class Sender:
    def __init__(self, ok=True):
        self.ok, self.msgs = ok, []

    def __call__(self, msg):
        self.msgs.append(msg)
        return self.ok


def rep(device=OLD, battery=None, free=None, total=None):
    return {"device": device, "battery": battery, "free_pages": free, "total_pages": total}


def run(tmp_path, s, *reports):
    out = []
    for r in reports:
        out += health.process(tmp_path, [r], sender=s)
    return out


def test_battery_sends_once_at_threshold(tmp_path):
    s = Sender()
    run(tmp_path, s, rep(battery=11), rep(battery=10), rep(battery=9), rep(battery=5), rep(battery=0))
    assert s.msgs == ["Old pendant battery at 10%"]


def test_battery_above_threshold_never_sends(tmp_path):
    s = Sender()
    run(tmp_path, s, rep(battery=100), rep(battery=11))
    assert s.msgs == []


def test_battery_hysteresis(tmp_path):
    s = Sender()
    run(tmp_path, s, rep(battery=9), rep(battery=20), rep(battery=29), rep(battery=9))
    assert len(s.msgs) == 1, "no re-arm until >=30%"
    run(tmp_path, s, rep(battery=30), rep(battery=8))
    assert s.msgs == ["Old pendant battery at 9%", "Old pendant battery at 8%"]


def test_memory_threshold_and_message(tmp_path):
    s = Sender()
    run(tmp_path, s, rep(free=11, total=100), rep(free=10, total=100), rep(free=1, total=100))
    assert s.msgs == ["Old pendant memory 90% full"]


def test_memory_hysteresis(tmp_path):
    s = Sender()
    run(tmp_path, s, rep(free=5, total=100), rep(free=50, total=100), rep(free=5, total=100))
    assert len(s.msgs) == 1, "50% used is not under 50%"
    run(tmp_path, s, rep(free=51, total=100), rep(free=5, total=100))
    assert len(s.msgs) == 2


def test_battery_and_memory_independent(tmp_path):
    s = Sender()
    run(tmp_path, s, rep(battery=5, free=80, total=100), rep(battery=5, free=5, total=100))
    assert s.msgs == ["Old pendant battery at 5%", "Old pendant memory 95% full"]


def test_pendants_independent(tmp_path):
    s = Sender()
    run(tmp_path, s, rep(OLD, battery=5), rep(NEW, battery=5), rep(OLD, battery=4), rep(NEW, battery=4))
    assert s.msgs == ["Old pendant battery at 5%", "New pendant battery at 5%"]
    run(tmp_path, s, rep(OLD, battery=60), rep(OLD, battery=5), rep(NEW, battery=5))
    assert s.msgs[2:] == ["Old pendant battery at 5%"]


def test_both_pendants_in_one_report(tmp_path):
    s = Sender()
    health.process(tmp_path, [rep(OLD, battery=5), rep(NEW, battery=50)], sender=s)
    assert s.msgs == ["Old pendant battery at 5%"]


def test_failure_stays_armed_then_retries(tmp_path):
    bad, good = Sender(ok=False), Sender()
    assert run(tmp_path, bad, rep(battery=5), rep(battery=5)) == []
    assert len(bad.msgs) == 2, "retried on every report"
    assert run(tmp_path, good, rep(battery=5), rep(battery=5)) == ["Old pendant battery at 5%"]
    assert len(good.msgs) == 1


def test_memory_failure_retries(tmp_path):
    bad, good = Sender(ok=False), Sender()
    run(tmp_path, bad, rep(free=1, total=100))
    run(tmp_path, good, rep(free=1, total=100))
    assert good.msgs == ["Old pendant memory 99% full"]


def test_null_fields_skip_and_keep_state(tmp_path):
    s = Sender()
    run(tmp_path, s, rep(battery=5), rep(battery=None), rep(battery=5))
    assert len(s.msgs) == 1
    run(tmp_path, s, rep(free=None, total=None), rep(free=5, total=None), rep(free=5, total=0))
    assert len(s.msgs) == 1


def test_state_persisted_across_calls_and_file(tmp_path):
    run(tmp_path, Sender(), rep(battery=5, free=5, total=100))
    st = json.loads((tmp_path / "alerts.json").read_text())
    assert st[OLD] == {"battery_armed": False, "memory_armed": False}
    s = Sender()
    run(tmp_path, s, rep(battery=5, free=5, total=100))
    assert s.msgs == []


def test_corrupt_state_file_resets_to_armed(tmp_path):
    (tmp_path / "alerts.json").write_text("{not json")
    s = Sender()
    run(tmp_path, s, rep(battery=5))
    assert len(s.msgs) == 1


def test_names_file_and_unknown_device(tmp_path):
    (tmp_path / "pendants.json").write_text(json.dumps({"AAAAAAAAAAAA1234": "spare"}))
    s = Sender()
    run(tmp_path, s, rep("AAAAAAAAAAAA1234", battery=5), rep("BBBBBBBBBBBB5678", battery=5))
    assert s.msgs == ["Spare pendant battery at 5%", "Pendant 5678 battery at 5%"]


def test_parse_report_valid_and_normalizes():
    out = health.parse_report({"pendants": [rep(OLD.lower(), 50, 10, 100), rep(NEW)]})
    assert out[0]["device"] == OLD and out[1]["battery"] is None


@pytest.mark.parametrize("body", [
    [], {}, {"pendants": "x"}, {"pendants": [1]},
    {"pendants": [{"battery": 5}]},
    {"pendants": [rep("nothex")]},
    {"pendants": [rep(battery=101)]}, {"pendants": [rep(battery=-1)]},
    {"pendants": [rep(battery="5")]}, {"pendants": [rep(battery=True)]},
    {"pendants": [rep(battery=5.5)]},
    {"pendants": [rep(free=-1, total=10)]}, {"pendants": [rep(free=11, total=10)]},
])
def test_parse_report_rejects(body):
    with pytest.raises(health.BadReport):
        health.parse_report(body)


def test_default_sender_runs_notifier(monkeypatch):
    calls = []
    monkeypatch.undo()  # drop conftest's stub so the real function runs; script path still guarded below
    monkeypatch.setattr(health, "NOTIFY_SCRIPT", "/x/notify.py")

    def fake(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(health.subprocess, "run", fake)
    assert health.default_sender("hi") is True
    assert calls == [["/usr/bin/python3", "/x/notify.py", "hi"]]


def test_default_sender_failure_modes(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(health, "NOTIFY_SCRIPT", "/x/notify.py")
    monkeypatch.setattr(health.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
    assert health.default_sender("hi") is False

    def boom(*a, **k):
        raise OSError("no")
    monkeypatch.setattr(health.subprocess, "run", boom)
    assert health.default_sender("hi") is False


def test_conftest_guard_blocks_real_sends():
    assert health.default_sender("must not send") is False


# --- endpoint -------------------------------------------------------------

@pytest.fixture()
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("PENDANT_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("PENDANT_NOTES_DIR", str(tmp_path / "notes"))
    s = Sender()
    monkeypatch.setattr(health, "default_sender", s)
    server = feed.make_server(0, "tok")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", s, tmp_path / "data"
    server.shutdown()


def post(url, body, token="tok", raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request(url + "/pendant/feed/health", data=data, method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read())


def test_endpoint_sends_once(srv):
    url, s, data = srv
    body = {"pendants": [rep(OLD, battery=9, free=5, total=100)]}
    code, out = post(url, body)
    assert code == 200 and out["alerts_sent"] == 2
    code, out = post(url, body)
    assert out["alerts_sent"] == 0
    assert s.msgs == ["Old pendant battery at 9%", "Old pendant memory 95% full"]
    assert (data / "alerts.json").exists()


def test_endpoint_requires_token(srv):
    url, s, _ = srv
    with pytest.raises(urllib.error.HTTPError) as e:
        post(url, {"pendants": [rep(battery=1)]}, token="wrong")
    assert e.value.code == 401 and s.msgs == []


@pytest.mark.parametrize("body,raw", [
    ({"pendants": [rep("zz", battery=5)]}, None),
    ({"pendants": [rep(battery=500)]}, None),
    ({"nope": 1}, None),
    (None, b"{broken"),
    (None, b"[1,2]"),
])
def test_endpoint_400_on_bad_input(srv, body, raw):
    url, s, _ = srv
    with pytest.raises(urllib.error.HTTPError) as e:
        post(url, body, raw=raw)
    assert e.value.code == 400 and s.msgs == []


def test_endpoint_failed_send_retries_next_report(srv, monkeypatch):
    url, _, _ = srv
    bad = Sender(ok=False)
    monkeypatch.setattr(health, "default_sender", bad)
    body = {"pendants": [rep(OLD, battery=5)]}
    assert post(url, body)[1]["alerts_sent"] == 0
    good = Sender()
    monkeypatch.setattr(health, "default_sender", good)
    assert post(url, body)[1]["alerts_sent"] == 1


def test_crash_after_send_does_not_resend(tmp_path):
    first = Sender()

    def crashing(msg):
        if first.msgs:
            raise SystemExit
        return first(msg)
    with pytest.raises(SystemExit):
        health.process(tmp_path, [rep(battery=5, free=5, total=100)], sender=crashing)
    assert first.msgs == ["Old pendant battery at 5%"]
    st = json.loads((tmp_path / "alerts.json").read_text())
    assert st[OLD]["battery_armed"] is False
    nxt = Sender()
    health.process(tmp_path, [rep(battery=5, free=5, total=100)], sender=nxt)
    assert nxt.msgs == ["Old pendant memory 95% full"], "battery alert is not re-sent"
