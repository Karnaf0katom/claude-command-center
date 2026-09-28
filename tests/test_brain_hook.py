"""Tests for ccc_server/brain_hook.py (MEMORY-1 optional brain plugin hook)."""

import json
import os
import stat

import ccc_server.brain_hook as brain_hook


def _write_config(path, data):
    path.write_text(json.dumps(data))


def _make_plugin_exe(tmp_path, script):
    plugin_dir = tmp_path / "plugin"
    bin_dir = plugin_dir / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "brain"
    exe.write_text(script)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return plugin_dir


def _use_config(monkeypatch, path):
    monkeypatch.setattr(brain_hook, "BRAIN_CONFIG_FILE", path)
    brain_hook._CONFIG_CACHE["sig"] = None
    brain_hook._CONFIG_CACHE["config"] = None


def test_no_config_file_is_a_noop(tmp_path, monkeypatch):
    _use_config(monkeypatch, tmp_path / "missing.json")
    assert brain_hook.brain_plugin_executable() is None
    assert brain_hook.is_enabled_for_repo("/some/repo") is False
    assert brain_hook.session_start_index("/some/repo") is None
    assert brain_hook.session_start_prefix("/some/repo") == ""


def test_configured_but_repo_not_in_enabled_list(tmp_path, monkeypatch):
    plugin_dir = _make_plugin_exe(tmp_path, "#!/bin/sh\necho hi\n")
    config_path = tmp_path / "brain-plugin.json"
    _write_config(config_path, {
        "plugin_path": str(plugin_dir),
        "enabled_repos": [str(tmp_path / "other-repo")],
    })
    _use_config(monkeypatch, config_path)
    assert brain_hook.brain_plugin_executable() == plugin_dir / "bin" / "brain"
    assert brain_hook.is_enabled_for_repo(str(tmp_path / "my-repo")) is False
    assert brain_hook.session_start_index(str(tmp_path / "my-repo")) is None


def test_enabled_repo_runs_plugin_and_returns_index(tmp_path, monkeypatch):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    plugin_dir = _make_plugin_exe(
        tmp_path, "#!/bin/sh\necho \"session context blob\"\n",
    )
    config_path = tmp_path / "brain-plugin.json"
    _write_config(config_path, {
        "plugin_path": str(plugin_dir),
        "enabled_repos": [str(repo)],
    })
    _use_config(monkeypatch, config_path)
    assert brain_hook.is_enabled_for_repo(str(repo)) is True
    assert brain_hook.session_start_index(str(repo)) == "session context blob"
    assert brain_hook.session_start_prefix(str(repo)) == "session context blob\n\n"


def test_empty_plugin_output_yields_no_prefix(tmp_path, monkeypatch):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    plugin_dir = _make_plugin_exe(tmp_path, "#!/bin/sh\n\n")
    config_path = tmp_path / "brain-plugin.json"
    _write_config(config_path, {
        "plugin_path": str(plugin_dir),
        "enabled_repos": [str(repo)],
    })
    _use_config(monkeypatch, config_path)
    assert brain_hook.session_start_index(str(repo)) is None
    assert brain_hook.session_start_prefix(str(repo)) == ""


def test_disable_env_var_overrides_config(tmp_path, monkeypatch):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    plugin_dir = _make_plugin_exe(tmp_path, "#!/bin/sh\necho hi\n")
    config_path = tmp_path / "brain-plugin.json"
    _write_config(config_path, {
        "plugin_path": str(plugin_dir),
        "enabled_repos": [str(repo)],
    })
    _use_config(monkeypatch, config_path)
    monkeypatch.setenv("CCC_DISABLE_BRAIN_HOOK", "1")
    assert brain_hook.brain_plugin_executable() is None
    assert brain_hook.session_start_index(str(repo)) is None


def test_unparseable_config_is_treated_as_absent(tmp_path, monkeypatch):
    config_path = tmp_path / "brain-plugin.json"
    config_path.write_text("{not json")
    _use_config(monkeypatch, config_path)
    assert brain_hook.brain_plugin_executable() is None
    assert brain_hook.is_enabled_for_repo("/some/repo") is False


def test_missing_bin_brain_executable_is_none(tmp_path, monkeypatch):
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    config_path = tmp_path / "brain-plugin.json"
    _write_config(config_path, {"plugin_path": str(plugin_dir), "enabled_repos": []})
    _use_config(monkeypatch, config_path)
    assert brain_hook.brain_plugin_executable() is None
