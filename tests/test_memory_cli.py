"""Unit tests for the `ccc recall` / `ccc shipped` CLI verbs in ./ccc.

./ccc has no .py suffix (it's the installed CLI entry point), so it's loaded
by file path rather than a normal import — same technique test_intro_video_
coverage.py uses for slides.py.
"""

import importlib.machinery
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
_CCC_CLI_PATH = str(REPO_ROOT / "ccc")
# No .py suffix, so spec_from_file_location can't infer a loader on its own.
_loader = importlib.machinery.SourceFileLoader("ccc_cli_memory_test", _CCC_CLI_PATH)
_spec = importlib.util.spec_from_file_location("ccc_cli_memory_test", _CCC_CLI_PATH, loader=_loader)
ccc_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ccc_cli)


def _args(**kw):
    base = {"server": "http://127.0.0.1:9999", "json": False}
    base.update(kw)
    return SimpleNamespace(**base)


def test_cmd_recall_prints_human_readable_rows(monkeypatch, capsys):
    payload = {
        "query": "confetti",
        "results": [
            {"session_id": "abc123", "title": "Add confetti", "repo": "widget-repo",
             "date": "2026-09-20", "snippet": "add a confetti animation on save"},
        ],
    }
    monkeypatch.setattr(ccc_cli, "_get_json", lambda base, path, timeout=10: payload)
    rc = ccc_cli.cmd_recall(_args(query=["confetti"], limit=10))
    out = capsys.readouterr().out
    assert rc == 0
    assert "abc123" in out
    assert "widget-repo" in out
    assert "add a confetti animation on save" in out


def test_cmd_recall_json_flag_prints_raw_payload(monkeypatch, capsys):
    payload = {"query": "confetti", "results": []}
    monkeypatch.setattr(ccc_cli, "_get_json", lambda base, path, timeout=10: payload)
    rc = ccc_cli.cmd_recall(_args(query=["confetti"], limit=10, json=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == payload


def test_cmd_recall_no_results_says_so(monkeypatch, capsys):
    payload = {"query": "nothing-like-this", "results": []}
    monkeypatch.setattr(ccc_cli, "_get_json", lambda base, path, timeout=10: payload)
    rc = ccc_cli.cmd_recall(_args(query=["nothing-like-this"], limit=10))
    out = capsys.readouterr().out
    assert rc == 0
    assert "no sessions found" in out


def test_cmd_recall_missing_query_errors(monkeypatch, capsys):
    monkeypatch.setattr(ccc_cli.sys.stdin, "isatty", lambda: True)
    rc = ccc_cli.cmd_recall(_args(query=[], limit=10))
    assert rc == 2
    assert "missing query" in capsys.readouterr().err


def test_cmd_shipped_prints_verdict_and_evidence(monkeypatch, capsys):
    payload = {
        "topic": "confetti",
        "shipped": True,
        "confidence": 0.92,
        "evidence": [{
            "repo": "widget-repo", "commit": "abc1234",
            "subject": "feat(widgets): add confetti animation",
        }],
        "tickets": ["WIDGETS-42"],
    }
    monkeypatch.setattr(ccc_cli, "_get_json", lambda base, path, timeout=10: payload)
    rc = ccc_cli.cmd_shipped(_args(topic=["confetti"]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "SHIPPED" in out
    assert "0.92" in out
    assert "widget-repo" in out
    assert "WIDGETS-42" in out


def test_cmd_shipped_not_shipped(monkeypatch, capsys):
    payload = {"topic": "x", "shipped": False, "confidence": 0.5, "evidence": [], "tickets": []}
    monkeypatch.setattr(ccc_cli, "_get_json", lambda base, path, timeout=10: payload)
    rc = ccc_cli.cmd_shipped(_args(topic=["x"]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOT SHIPPED" in out


def test_cmd_shipped_missing_topic_errors(monkeypatch, capsys):
    monkeypatch.setattr(ccc_cli.sys.stdin, "isatty", lambda: True)
    rc = ccc_cli.cmd_shipped(_args(topic=[]))
    assert rc == 2
    assert "missing topic" in capsys.readouterr().err


def test_cmd_history_prints_commits_and_sessions_newest_first(monkeypatch, capsys):
    payload = {
        "path": "app.py",
        "repo": "widget-repo",
        "history": [
            {"kind": "commit", "hash": "abc1234", "date": "2026-09-20",
             "why": "feat(widgets): add confetti animation"},
            {"kind": "session", "session_id": "session-abc", "date": "2026-09-19",
             "why": "Add confetti"},
        ],
    }
    monkeypatch.setattr(ccc_cli, "_get_json", lambda base, path, timeout=10: payload)
    rc = ccc_cli.cmd_history(_args(path=["app.py"], limit=20, repo=None))
    out = capsys.readouterr().out
    assert rc == 0
    assert "abc1234" in out
    assert "feat(widgets): add confetti animation" in out
    assert "session-abc" in out


def test_cmd_history_json_flag_prints_raw_payload(monkeypatch, capsys):
    payload = {"path": "app.py", "repo": "widget-repo", "history": []}
    monkeypatch.setattr(ccc_cli, "_get_json", lambda base, path, timeout=10: payload)
    rc = ccc_cli.cmd_history(_args(path=["app.py"], limit=20, repo=None, json=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == payload


def test_cmd_history_no_results_says_so(monkeypatch, capsys):
    payload = {"path": "app.py", "repo": "", "history": []}
    monkeypatch.setattr(ccc_cli, "_get_json", lambda base, path, timeout=10: payload)
    rc = ccc_cli.cmd_history(_args(path=["app.py"], limit=20, repo=None))
    out = capsys.readouterr().out
    assert rc == 0
    assert "no history found" in out


def test_cmd_history_missing_path_errors(monkeypatch, capsys):
    monkeypatch.setattr(ccc_cli.sys.stdin, "isatty", lambda: True)
    rc = ccc_cli.cmd_history(_args(path=[], limit=20, repo=None))
    assert rc == 2
    assert "missing path" in capsys.readouterr().err


def test_cmd_history_repo_flag_added_to_query_string(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        ccc_cli, "_get_json",
        lambda base, path, timeout=10: calls.append(path) or {"history": []})
    ccc_cli.cmd_history(_args(path=["app.py"], limit=20, repo="widget-repo"))
    assert "repo=widget-repo" in calls[-1]


def test_cmd_decisions_prints_sessions_and_snippets(monkeypatch, capsys):
    payload = {
        "topic": "queue engine",
        "results": [
            {"session_id": "session-abc", "title": "Pick a queue engine",
             "repo": "widget-repo", "date": "2026-09-20",
             "snippet": "we decided to go with sqlite instead of postgres"},
        ],
    }
    monkeypatch.setattr(ccc_cli, "_get_json", lambda base, path, timeout=10: payload)
    rc = ccc_cli.cmd_decisions(_args(topic=["queue", "engine"], limit=10))
    out = capsys.readouterr().out
    assert rc == 0
    assert "session-abc" in out
    assert "widget-repo" in out
    assert "instead of postgres" in out


def test_cmd_decisions_json_flag_prints_raw_payload(monkeypatch, capsys):
    payload = {"topic": "queue engine", "results": []}
    monkeypatch.setattr(ccc_cli, "_get_json", lambda base, path, timeout=10: payload)
    rc = ccc_cli.cmd_decisions(_args(topic=["queue", "engine"], limit=10, json=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == payload


def test_cmd_decisions_no_results_says_so(monkeypatch, capsys):
    payload = {"topic": "x", "results": []}
    monkeypatch.setattr(ccc_cli, "_get_json", lambda base, path, timeout=10: payload)
    rc = ccc_cli.cmd_decisions(_args(topic=["x"], limit=10))
    out = capsys.readouterr().out
    assert rc == 0
    assert "no decisions found" in out


def test_cmd_decisions_missing_topic_errors(monkeypatch, capsys):
    monkeypatch.setattr(ccc_cli.sys.stdin, "isatty", lambda: True)
    rc = ccc_cli.cmd_decisions(_args(topic=[], limit=10))
    assert rc == 2
    assert "missing topic" in capsys.readouterr().err
