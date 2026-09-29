"""Run one shell command in a visible terminal window (CCC-1218).

Backs the "Run" button on a session's "Needs you" card: the agent asked the
user to run a command, the user confirmed it in the browser, and this types
it into a new Terminal/iTerm2 window where its output stays visible.

The command reaches AppleScript as an argv item, never spliced into script
source, so quotes/backslashes in it can't break out of the AppleScript
string. The caller (server.py) keeps this local-only.
"""
import os
import platform
import shlex
import subprocess

MAX_COMMAND_LEN = 2000

_TERMINAL_SCRIPT = (
    "on run argv",
    'tell application "Terminal"',
    "activate",
    "do script (item 1 of argv)",
    "end tell",
    "end run",
)

_ITERM_SCRIPT = (
    "on run argv",
    'tell application "iTerm2"',
    "activate",
    "set newWin to (create window with default profile)",
    "tell current session of newWin to write text (item 1 of argv)",
    "end tell",
    "end run",
)


def build_shell_line(command, cwd=None):
    """Validate `command` and prefix a `cd` into `cwd` when it exists."""
    cmd = str(command or "").strip()
    if not cmd:
        raise ValueError("missing command")
    if len(cmd) > MAX_COMMAND_LEN:
        raise ValueError("command too long")
    if any(ch in cmd for ch in ("\n", "\r", "\x00")):
        raise ValueError("multi-line commands are not supported")
    folder = os.path.expanduser(str(cwd or "").strip())
    if folder and os.path.isdir(folder):
        return "cd " + shlex.quote(folder) + " && " + cmd
    return cmd


def osascript_argv(line, terminal_app="Terminal"):
    script = _ITERM_SCRIPT if terminal_app == "iTerm2" else _TERMINAL_SCRIPT
    argv = ["osascript"]
    for stmt in script:
        argv += ["-e", stmt]
    argv.append(line)
    return argv


def run_in_terminal(command, cwd=None, terminal_app="Terminal", popen=subprocess.Popen):
    if platform.system() != "Darwin":
        return {"ok": False, "error": "running in a terminal is macOS-only"}
    try:
        line = build_shell_line(command, cwd)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    app = "iTerm2" if terminal_app == "iTerm2" else "Terminal"
    try:
        popen(osascript_argv(line, app), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "terminal_app": app, "command": line}
