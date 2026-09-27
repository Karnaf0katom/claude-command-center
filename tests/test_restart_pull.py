"""Manual restarts fast-forward the install dir first, and never destroy work.

A restart that re-execs the code already on disk reads to the user as "the
restart did nothing". `_pull_before_restart` picks up origin/main first, but
only ever by fast-forward, and never blocks the restart itself.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402


def _fake_git(log, branch="main", heads=("aaa", "bbb"), fail=None):
    heads = list(heads)

    def _git(args, cwd, timeout=10):
        args = list(args)
        log.append(args)
        if fail and args[0] == fail:
            return 1, "", "nope"
        if args[0] == "rev-parse" and "--abbrev-ref" in args:
            return 0, branch + "\n", ""
        if args[0] == "rev-parse":
            return 0, (heads.pop(0) if len(heads) > 1 else heads[0]) + "\n", ""
        return 0, "", ""
    return _git


@pytest.fixture
def install(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(server, "_install_dir", lambda: tmp_path)
    monkeypatch.delenv("CCC_RESTART_PULL", raising=False)
    return tmp_path


def test_fast_forwards_and_reports_the_change(install, monkeypatch):
    log = []
    monkeypatch.setattr(server, "_git", _fake_git(log))
    res = server._pull_before_restart()
    assert res == {"ok": True, "before": "aaa", "after": "bbb", "changed": True}
    assert ["merge", "--ff-only", "--quiet", "origin/main"] in log
    assert not any(a[0] == "reset" for a in log)


def test_unchanged_head_is_not_a_change(install, monkeypatch):
    monkeypatch.setattr(server, "_git", _fake_git([], heads=("aaa",)))
    assert server._pull_before_restart()["changed"] is False


def test_skips_off_main(install, monkeypatch):
    log = []
    monkeypatch.setattr(server, "_git", _fake_git(log, branch="feat/x"))
    res = server._pull_before_restart()
    assert res["ok"] is True and "skipped" in res
    assert not any(a[0] in ("fetch", "merge") for a in log)


def test_refused_fast_forward_is_reported_not_raised(install, monkeypatch):
    monkeypatch.setattr(server, "_git", _fake_git([], fail="merge"))
    res = server._pull_before_restart()
    assert res["ok"] is False and "fast-forward refused" in res["error"]


def test_opt_out(install, monkeypatch):
    log = []
    monkeypatch.setattr(server, "_git", _fake_git(log))
    monkeypatch.setenv("CCC_RESTART_PULL", "0")
    assert "skipped" in server._pull_before_restart()
    assert log == []


def test_self_update_never_resets_hard():
    import inspect
    src = inspect.getsource(server._self_update)
    assert '"reset"' not in src
    assert '"--ff-only"' in src
