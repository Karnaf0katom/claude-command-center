"""Bounded repair of a missing native dependency in an existing Codex npm CLI."""
from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows CLIs remain on the existing resolver path.
    fcntl = None
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

_REPAIR_MUTEX = threading.Lock()
_REPAIR_COOLDOWN_S = 300
_TARGETS = {
    ("Darwin", "arm64"): ("darwin-arm64", "aarch64-apple-darwin"),
    ("Darwin", "x86_64"): ("darwin-x64", "x86_64-apple-darwin"),
    ("Linux", "aarch64"): ("linux-arm64", "aarch64-unknown-linux-musl"),
    ("Linux", "x86_64"): ("linux-x64", "x86_64-unknown-linux-musl"),
}


def ensure_codex_cli_health(executable):
    """Return health for a recognized global npm launcher, else None.

    Healthy installations only perform filesystem reads. Repair is limited
    to a user-owned installation with a pinned official native dependency,
    and only after the launcher reports that exact dependency missing.
    """
    if fcntl is None:
        return None
    try:
        launcher = Path(executable).resolve()
        if launcher.name != "codex.js" or launcher.parent.name != "bin":
            return None
        root = launcher.parent.parent
        if (root.name, root.parent.name, root.parents[1].name, root.parents[2].name) != ("codex", "@openai", "node_modules", "lib"):
            return None
        manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
        version = manifest.get("version")
        if manifest.get("name") != "@openai/codex" or not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
            return None
        target = _TARGETS.get((platform.system(), platform.machine()))
        if target is None:
            return None
        suffix, triple = target
        dependency = "@openai/codex-" + suffix
        if (manifest.get("optionalDependencies") or {}).get(dependency) != "npm:@openai/codex@" + version + "-" + suffix:
            return None
    except (OSError, ValueError, TypeError, AttributeError, IndexError):
        return None

    prefix = root.parents[3]
    candidates = (
        root / "node_modules" / dependency / "vendor" / triple / "bin" / "codex",
        root.parent / ("codex-" + suffix) / "vendor" / triple / "bin" / "codex",
        root / "vendor" / triple / "bin" / "codex",
    )

    def native_present():
        return any(candidate.is_file() and os.access(candidate, os.X_OK) for candidate in candidates)

    if native_present():
        return {"available": True, "repair_status": "healthy"}
    command = ["npm", "install", "-g", "--prefix", str(prefix),
               "@openai/codex@" + version, "--include=optional", "--ignore-scripts"]
    manual = shlex.join(command)

    def failed(status, explanation):
        return {"available": False, "code": "codex_native_package_missing",
                "repair_status": status, "repair_command": manual,
                "reason": f"Codex CLI {version} is missing its native package. {explanation} Run: {manual}"}

    def probe():
        return subprocess.run([str(launcher), "--version"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              errors="replace", check=False, timeout=5)

    try:
        result = probe()
    except (OSError, subprocess.TimeoutExpired):
        return failed("failed", "The launcher could not be checked; automatic repair was not attempted.")
    if result.returncode == 0:
        return {"available": True, "repair_status": "healthy"}
    if "Missing optional dependency " + dependency not in (result.stderr or ""):
        return failed("not_attempted", "The launcher reported a different error; automatic repair was not attempted.")
    try:
        if root.stat().st_uid != os.getuid() or prefix.stat().st_uid != os.getuid():
            return failed("not_attempted", "This installation belongs to a different user; repair it manually.")
    except OSError:
        return failed("not_attempted", "Installation ownership could not be checked.")
    npm = shutil.which("npm")
    if not npm:
        return failed("not_attempted", "npm is unavailable; install npm before retrying.")
    if not _REPAIR_MUTEX.acquire(blocking=False):
        return failed("repairing", "Automatic repair is in progress; retry shortly.")
    lock = None
    try:
        filename = prefix / ".ccc-codex-native-repair.lock"
        fd = os.open(filename, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        lock = os.fdopen(fd, "r+", encoding="utf-8")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return failed("repairing", "Automatic repair is in progress; retry shortly.")
        if native_present():
            return {"available": True, "repair_status": "healthy"}
        try:
            last = json.loads(lock.read(4096))
            recent = last.get("version") == version and time.time() - float(last.get("at", 0)) < _REPAIR_COOLDOWN_S
        except (ValueError, TypeError, AttributeError):
            recent = False
        if recent:
            return failed("cooldown", "Automatic repair was recently attempted. Retry in five minutes or repair manually.")
        lock.seek(0)
        lock.truncate()
        lock.write(json.dumps({"version": version, "at": time.time()}))
        lock.flush()
        command[0] = npm
        try:
            installed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, check=False, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return failed("failed", "Automatic repair could not finish; check npm and network access.")
        if installed.returncode != 0:
            return failed("failed", "Automatic repair failed; check npm and network access.")
        try:
            verified = probe()
        except (OSError, subprocess.TimeoutExpired):
            return failed("failed", "Automatic repair finished but CLI verification failed.")
        if not native_present() or verified.returncode != 0 or not verified.stdout.strip().endswith(" " + version):
            return failed("failed", "Automatic repair finished but CLI verification failed.")
        return {"available": True, "repair_status": "repaired"}
    except OSError:
        return failed("failed", "Automatic repair could not lock the installation; check its permissions.")
    finally:
        if lock is not None:
            lock.close()
        _REPAIR_MUTEX.release()
