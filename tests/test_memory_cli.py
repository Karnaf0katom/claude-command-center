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
