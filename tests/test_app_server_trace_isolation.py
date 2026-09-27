"""Tests must never append to the user's live app-server-trace.log.

The Codex app-server tests drive Mock transports with fixed clocks; their
trace records (child_pid=<Mock ...>, ts=1200.0) used to land in
~/.claude/command-center/logs/app-server-trace.log, the file used to debug
real app-server failures.
"""
import importlib
import json
from pathlib import Path

server = importlib.import_module("server")

REAL_TRACE = Path.home() / ".claude" / "command-center" / "logs" / "app-server-trace.log"


def test_trace_file_is_isolated_under_tests():
    trace = Path(server.APP_SERVER_TRACE_FILE).resolve()
    assert trace != REAL_TRACE.resolve()
    assert trace != (Path(server.COMMAND_CENTER_STATE_DIR) / "logs" / "app-server-trace.log").resolve()


def test_trace_records_land_in_the_isolated_file():
    marker = "ccc-1199-guard"
    server._app_server_trace("test-guard", marker=marker)
    lines = Path(server.APP_SERVER_TRACE_FILE).read_text().splitlines()
    assert any(json.loads(ln).get("marker") == marker for ln in lines)
