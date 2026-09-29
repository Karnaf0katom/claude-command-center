import json
import os
import subprocess
import threading
from pathlib import Path
from unittest import mock

import server


def npm_installation(tmp_path):
    prefix = tmp_path / "prefix"
    root = prefix / "lib" / "node_modules" / "@openai" / "codex"
    executable = root / "bin" / "codex.js"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o755)
    (root / "package.json").write_text(json.dumps({
        "name": "@openai/codex", "version": "0.159.1",
        "optionalDependencies": {"@openai/codex-darwin-arm64": "npm:@openai/codex@0.159.1-darwin-arm64"},
    }))
    binary = root / "node_modules" / "@openai" / "codex-darwin-arm64" / "vendor" / "aarch64-apple-darwin" / "bin" / "codex"
    return prefix, root, executable, binary


def missing_native(argv, **kwargs):
    if "--version" in argv:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Missing optional dependency @openai/codex-darwin-arm64")
    return subprocess.CompletedProcess(argv, 1, stdout="", stderr="offline")


def test_resolver_reports_failed_native_repair(tmp_path, monkeypatch):
    _, _, executable, _ = npm_installation(tmp_path)
    monkeypatch.setenv("CCC_CODEX_BIN", str(executable))
    with mock.patch("platform.system", return_value="Darwin"), mock.patch("platform.machine", return_value="arm64"), mock.patch("subprocess.run", side_effect=missing_native), mock.patch("shutil.which", return_value="/example/npm"):
        result = server._resolve_codex_bin()
    assert result["available"] is False
    assert "repair" in result["reason"].lower()
    assert "0.159.1" in result["reason"]


def test_repair_pins_installed_version_and_verifies_binary(tmp_path):
    from ccc_server.codex_cli_health import ensure_codex_cli_health
    prefix, _, executable, binary = npm_installation(tmp_path)
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        if "install" in argv:
            binary.parent.mkdir(parents=True)
            binary.write_text("binary fixture")
            binary.chmod(0o755)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if binary.exists():
            return subprocess.CompletedProcess(argv, 0, stdout="codex-cli 0.159.1\n", stderr="")
        return missing_native(argv, **kwargs)

    with mock.patch("platform.system", return_value="Darwin"), mock.patch("platform.machine", return_value="arm64"), mock.patch("subprocess.run", side_effect=run), mock.patch("shutil.which", return_value="/example/npm"):
        result = ensure_codex_cli_health(str(executable))
    assert result["available"]
    assert result["repair_status"] == "repaired"
    install = next(command for command in commands if "install" in command)
    assert "@openai/codex@0.159.1" in install
    assert "--include=optional" in install
    assert "--ignore-scripts" in install
    assert install[install.index("--prefix") + 1] == str(prefix)
    assert len([command for command in commands if "--version" in command]) == 2


def test_healthy_native_package_does_not_launch_process(tmp_path):
    from ccc_server.codex_cli_health import ensure_codex_cli_health
    _, _, executable, binary = npm_installation(tmp_path)
    binary.parent.mkdir(parents=True)
    binary.write_text("fixture")
    binary.chmod(0o755)
    with mock.patch("platform.system", return_value="Darwin"), mock.patch("platform.machine", return_value="arm64"), mock.patch("subprocess.run") as run:
        assert ensure_codex_cli_health(str(executable))["available"]
    run.assert_not_called()


def test_failed_repair_has_cooldown_and_manual_command(tmp_path):
    from ccc_server.codex_cli_health import ensure_codex_cli_health
    _, _, executable, _ = npm_installation(tmp_path)
    with mock.patch("platform.system", return_value="Darwin"), mock.patch("platform.machine", return_value="arm64"), mock.patch("subprocess.run", side_effect=missing_native) as run, mock.patch("shutil.which", return_value="/example/npm"):
        first = ensure_codex_cli_health(str(executable))
        second = ensure_codex_cli_health(str(executable))
    assert not first["available"] and not second["available"]
    assert second["repair_status"] == "cooldown"
    assert "npm" in second["repair_command"]
    assert len([call for call in run.call_args_list if "install" in call.args[0]]) == 1


def test_non_missing_dependency_error_never_installs(tmp_path):
    from ccc_server.codex_cli_health import ensure_codex_cli_health
    _, _, executable, _ = npm_installation(tmp_path)
    with mock.patch("platform.system", return_value="Darwin"), mock.patch("platform.machine", return_value="arm64"), mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="Different launcher error")) as run:
        result = ensure_codex_cli_health(str(executable))
    assert not result["available"]
    assert run.call_count == 1


def test_non_npm_binary_is_left_alone(tmp_path):
    from ccc_server.codex_cli_health import ensure_codex_cli_health
    executable = tmp_path / "codex"
    executable.write_text("binary")
    with mock.patch("subprocess.run") as run:
        assert ensure_codex_cli_health(str(executable)) is None
    run.assert_not_called()


def test_concurrent_requests_do_not_start_two_repairs(tmp_path):
    from ccc_server.codex_cli_health import ensure_codex_cli_health
    _, _, executable, binary = npm_installation(tmp_path)
    installing = threading.Event()
    release = threading.Event()
    results = []
    installs = []

    def run(argv, **kwargs):
        if "install" in argv:
            installs.append(argv)
            installing.set()
            assert release.wait(5)
            binary.parent.mkdir(parents=True)
            binary.write_text("fixture")
            binary.chmod(0o755)
            return subprocess.CompletedProcess(argv, 0)
        if binary.exists():
            return subprocess.CompletedProcess(argv, 0, stdout="codex-cli 0.159.1", stderr="")
        return missing_native(argv, **kwargs)

    with mock.patch("platform.system", return_value="Darwin"), mock.patch("platform.machine", return_value="arm64"), mock.patch("subprocess.run", side_effect=run), mock.patch("shutil.which", return_value="/example/npm"):
        worker = threading.Thread(target=lambda: results.append(ensure_codex_cli_health(str(executable))))
        worker.start()
        try:
            assert installing.wait(5)
            second = ensure_codex_cli_health(str(executable))
            assert second["repair_status"] == "repairing"
            assert len(installs) == 1
        finally:
            release.set()
            worker.join(5)
    assert results[0]["available"]


def test_cli_repair_message_reaches_desktop_composer():
    from ccc_server import codex_client
    import pytest
    message = "Codex native package repair failed. Run npm install -g @openai/codex@0.159.1 --include=optional"
    with mock.patch.object(codex_client, "_client_catalog", return_value={"ok": False, "code": "codex_native_package_missing", "error": message}):
        with pytest.raises(ValueError, match="native package repair failed") as caught:
            codex_client._client_operation({"method": "turn/start"})
    assert str(caught.value) == message


def test_platform_without_fcntl_preserves_existing_resolver(tmp_path, monkeypatch):
    from ccc_server import codex_cli_health
    _, _, executable, _ = npm_installation(tmp_path)
    monkeypatch.setenv("CCC_CODEX_BIN", str(executable))
    with mock.patch.object(codex_cli_health, "fcntl", None), mock.patch("subprocess.run") as run:
        assert server._resolve_codex_bin()["available"]
    run.assert_not_called()
