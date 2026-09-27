"""Tests for ccc_server/shipped_check.py (MEMO-FIX-6 pre-spawn check)."""

import time

import pytest

import ccc_server.ship_graph as ship_graph
import ccc_server.shipped_check as shipped_check


SHIPPED_RESULT = {
    "shipped": True,
    "confidence": 0.9,
    "evidence": [
        {"repo": "claude-command-center", "commit": "abc123def456", "subject": "feat(x): did the thing"},
    ],
    "tickets": ["MEMO-FIX-1"],
}


def test_check_shipped_for_spawn_returns_none_for_empty_goal():
    assert shipped_check.check_shipped_for_spawn("") is None
    assert shipped_check.check_shipped_for_spawn("   ") is None


def test_check_shipped_for_spawn_returns_warning_on_high_confidence(monkeypatch):
    monkeypatch.setattr(ship_graph, "is_shipped", lambda topic: SHIPPED_RESULT)
    info = shipped_check.check_shipped_for_spawn("did the thing")
    assert info == {
        "shipped": True,
        "confidence": 0.9,
        "repo": "claude-command-center",
        "commit": "abc123def456",
        "subject": "feat(x): did the thing",
        "tickets": ["MEMO-FIX-1"],
    }


def test_check_shipped_for_spawn_returns_none_below_confidence_bar(monkeypatch):
    low = dict(SHIPPED_RESULT, confidence=0.5)
    monkeypatch.setattr(ship_graph, "is_shipped", lambda topic: low)
    assert shipped_check.check_shipped_for_spawn("did the thing") is None


def test_check_shipped_for_spawn_returns_none_when_not_shipped(monkeypatch):
    monkeypatch.setattr(
        ship_graph, "is_shipped",
        lambda topic: {"shipped": False, "confidence": 0.0, "evidence": [], "tickets": []},
    )
    assert shipped_check.check_shipped_for_spawn("brand new idea") is None


def test_check_shipped_for_spawn_returns_none_when_is_shipped_raises(monkeypatch):
    def boom(topic):
        raise RuntimeError("db locked")
    monkeypatch.setattr(ship_graph, "is_shipped", boom)
    assert shipped_check.check_shipped_for_spawn("did the thing") is None


def test_check_shipped_for_spawn_never_blocks_past_timeout(monkeypatch):
    def slow(topic):
        time.sleep(5)
        return SHIPPED_RESULT
    monkeypatch.setattr(ship_graph, "is_shipped", slow)
    start = time.monotonic()
    info = shipped_check.check_shipped_for_spawn("did the thing", timeout_s=0.2)
    elapsed = time.monotonic() - start
    assert info is None
    assert elapsed < 1.0, f"check_shipped_for_spawn blocked for {elapsed:.2f}s past its 0.2s cap"


def test_check_shipped_for_spawn_disabled_via_env(monkeypatch):
    monkeypatch.setenv("CCC_DISABLE_SHIPPED_CHECK", "1")
    calls = []
    monkeypatch.setattr(
        ship_graph, "is_shipped",
        lambda topic: (calls.append(topic), SHIPPED_RESULT)[1],
    )
    assert shipped_check.check_shipped_for_spawn("did the thing") is None
    assert calls == [], "is_shipped should not be called when the check is disabled"


def test_shipped_warning_line_format():
    info = {
        "repo": "claude-command-center",
        "commit": "abc123def456",
        "subject": "feat(x): did the thing",
        "confidence": 0.9,
    }
    line = shipped_check.shipped_warning_line(info)
    assert line.startswith(
        "Heads-up: this may already be shipped: feat(x): did the thing "
        "(claude-command-center abc123de), confidence 0.90. Verify before rebuilding."
    )
