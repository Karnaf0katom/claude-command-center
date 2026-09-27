# Restart matrix (always on)

A committed fix is not a live fix. Python loads code once at process start, so
a change sits inert until the process running it restarts. **End every fix with
these three lines:**

```
Dashboard server restart needed:  Y/N
Worker restart needed:            Y/N
WatchTower server restart needed: Y/N
```

Quick rules (full table in `CLAUDE.md` § Restart matrix):

- `server.py` (or a module it imports) → **Dashboard Y**, and **Worker Y too** —
  `worker_engines.py` lazily `import server`, so the worker runs its own copy of
  that module state. Restarting only the dashboard looks exactly like "the fix
  didn't work".
- `ccc_worker.py`, `worker_engines.py`, `control_plane.py` → **Worker Y**.
- `static/*`, `docs/`, `changelog.d/`, `tests/`, markdown → **N/N/N** (static is
  served from disk per request; a browser reload is enough).
- WatchTower (`ai.watchtower.watcher`, `:8787`) lives in its own repo — CCC
  changes are **N** unless you edited WatchTower itself.

```bash
launchctl kickstart -k gui/$(id -u)/com.github.claude-command-center.worker
launchctl kickstart -k gui/$(id -u)/com.github.claude-command-center
```

Order does not matter: `run.sh` restarts a stale worker (content-hash check)
and waits for it before the dashboard starts. Restarting the worker marks
running queue items "needs reconciliation", so only restart when the change
actually requires it.

**If `launchctl kickstart` says "Could not find service" for the dashboard
label**, don't assume the fix is `./run.sh --install-service` — check
`pgrep -f "MacOS/CCC"` first. The .app shares its launchd Label with its own
bundle identifier (`com.github.claude-command-center`), so while the .app is
open, `launchctl bootstrap` for that same Label always fails with a bare
`Bootstrap failed: 5: Input/output error` (bundle-ID collision in the gui/<uid>
session — confirmed via `launchctl dumpstate | grep application.com.github...`
showing an active per-PID "application" domain for the running app). The
.app self-manages its own `server.py` child whenever nothing else is already
serving the port — quit and relaunch the .app to restore it instead of
fighting the launchd install path. `run.sh` now detects this and prints the
same guidance (OPS-1246).
