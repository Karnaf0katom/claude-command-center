# Restart matrix (always on)

A committed fix isn't live until the process running it restarts. End every fix with:

```
Dashboard server restart needed:  Y/N
Worker restart needed:            Y/N
WatchTower server restart needed: Y/N
```

- `server.py` or any module it imports → Dashboard **Y** and Worker **Y** (`worker_engines.py` lazily imports `server`, so the worker runs its own copy).
- `ccc_worker.py`, `worker_engines.py`, `control_plane.py` → Worker **Y**.
- `static/*`, `docs/`, `changelog.d/`, `tests/`, markdown → N/N/N (browser reload is enough).
- WatchTower (`ai.watchtower.watcher`, `:8787`) is a separate repo → N unless you edited it.

Restart (order doesn't matter; `run.sh` refreshes a stale worker; worker restart marks running queue items "needs reconciliation"):
`launchctl kickstart -k gui/$(id -u)/com.github.claude-command-center` (add `.worker` for the worker).
"Could not find service" while the .app is open: see `docs/agent-rules.md` (bundle-ID collision).
