"""Shared pytest fixtures.

`app.reliability` defaults to a real, persistent SQLite file
(`reliability.db`) so the running web app keeps history across requests.
Tests must not read or write that file — a developer having played with
the app locally (or a previous test run) would otherwise make the
scheduler's reliability tie-break, and therefore any assertion about which
interpreter got picked, non-deterministic. This autouse fixture points
every test at a fresh, empty temp database instead.

`app.settings` is a module-level mutable singleton for the same reason
`app.reliability`'s default db path is a module global: every rule/
scheduler/coverage function reads the *current* value at call time so a
planner's change takes effect immediately, with no caching to invalidate.
That convenience becomes a hazard in tests: without a reset, a test that
calls `settings.update(...)` would leak its change into every test that
runs after it in the same session. This fixture resets to defaults before
every test.
"""

from __future__ import annotations

import pytest

from app import db, reliability, settings


@pytest.fixture(autouse=True)
def isolated_reliability_db(tmp_path, monkeypatch):
    monkeypatch.setattr(reliability, "DEFAULT_DB_PATH", tmp_path / "reliability_test.db")


@pytest.fixture(autouse=True)
def isolated_app_db(tmp_path, monkeypatch):
    # Same reasoning as isolated_reliability_db: app.db is the durable copy
    # of PlanningStore/settings state for the *running app*. Tests must
    # never read or write it, or a developer's local admin/settings
    # testing (or test execution order) would leak into assertions about
    # scheduler output.
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "app_test.db")


@pytest.fixture(autouse=True)
def reset_settings():
    settings.reset()
    yield
    settings.reset()
