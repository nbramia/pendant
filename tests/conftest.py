"""Shared fixtures."""
import pytest


@pytest.fixture(autouse=True)
def _no_live_model_apis(monkeypatch):
    """Tests never call Fireworks or TypeSafe; tests that need them patch these back."""
    from pendant_direct import jev, summarize
    monkeypatch.setattr(jev, "api_key", lambda: None)
    monkeypatch.setattr(summarize, "fireworks_key", lambda: None)


@pytest.fixture(autouse=True)
def _no_live_calendar(monkeypatch):
    """Tests never call the LifeOS API; tests that need it pass a fake `get`."""
    from pendant_direct import attendance

    def blocked(*a, **k):
        raise RuntimeError("live LifeOS calls are blocked in tests")
    real = attendance.__dict__.setdefault("_real_events_on", attendance.events_on)
    monkeypatch.setattr(attendance, "_get", blocked)
    monkeypatch.setattr(attendance, "events_on",
                        lambda day, get=None: real(day, get=get) if get else [])


@pytest.fixture(autouse=True)
def _no_live_telegram(monkeypatch):
    """No test can ever send Telegram: the default sender is a failing no-op and
    the notifier script path points nowhere. Tests inject their own sender."""
    from pendant_direct import health
    monkeypatch.setattr(health, "NOTIFY_SCRIPT", "/nonexistent/notify.py")
    monkeypatch.setattr(health, "default_sender", lambda message: False)


@pytest.fixture(autouse=True)
def _no_live_lifeos_tasks(monkeypatch):
    """No test creates a real LifeOS task; tests inject a fake `post`."""
    from pendant_direct import tasks

    def blocked(*a, **k):
        raise RuntimeError("live LifeOS task calls are blocked in tests")
    monkeypatch.setattr(tasks, "lifeos_post", blocked)


@pytest.fixture(autouse=True)
def _no_live_lifeos_people(monkeypatch):
    """No test reads real LifeOS people (attendance._get is blocked above) or keeps
    closeness state between tests; tests pass a fake `get`."""
    from pendant_direct import closeness
    closeness._cache.clear()
    from pendant_direct import lifeos_people
    lifeos_people._cache.clear()
    monkeypatch.setattr(lifeos_people, "_down_until", 0.0)
    monkeypatch.setattr(closeness, "_down_until", 0.0)
    from pendant_direct import calendar_hint
    calendar_hint._present.clear()
    monkeypatch.setattr(calendar_hint, "_present_down_until", 0.0)


@pytest.fixture(autouse=True)
def _wearer_emails(monkeypatch):
    """The wearer's calendar addresses come from config.json; tests use fixed fictional ones."""
    from pendant_direct import attendance
    monkeypatch.setattr(attendance, "WEARER_EMAILS", frozenset({"noah@work.example", "noah@home.example"}))


@pytest.fixture(autouse=True)
def _pendant_names(monkeypatch):
    """Pendant names come from pendants.json in the data folder; tests name the two fictional
    pendants "old" and "new" as a setup would."""
    from pendant_direct import names
    monkeypatch.setattr(names, "DEFAULTS", {"0F1E2D3C4B5A6978": "old", "A1B2C3D4E5F60718": "new"})


@pytest.fixture(autouse=True)
def _test_wearer(monkeypatch):
    """Tests run as a made-up wearer, Noah Park (code defaults to "Me" until `speakers.wearer` is set)."""
    from pendant_direct import attendance
    monkeypatch.setattr(attendance, "WEARER_NAME", "Noah Park")


@pytest.fixture(autouse=True)
def _test_wearer_defaults(monkeypatch):
    """The code's defaults have no wearer; tests run as if Noah had set himself up."""
    from pendant_direct import config
    monkeypatch.setitem(config.DEFAULTS["speakers"], "wearer", "Noah")
    monkeypatch.setitem(config.DEFAULTS["speakers"], "allowed", ["Noah"])


@pytest.fixture(autouse=True)
def _no_auto_name_floor(monkeypatch):
    """The synthetic voices in tests are seconds long: the 30 s floor for automatic names
    (speakers.min_auto_name_speech_s) is off unless a test turns it on."""
    from pendant_direct import config
    monkeypatch.setitem(config.DEFAULTS["speakers"], "min_auto_name_speech_s", 0.0)
