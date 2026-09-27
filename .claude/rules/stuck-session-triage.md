# Stuck session triage (always on)

A session that ended its turn cleanly is not proven healthy. When a user says a session is stuck
or a message sits on "sending…", believe them and check for an undelivered inject first:
`GET /api/session/<sid>/inject-receipt` (non-null `outstanding` = unproven delivery), then
`activity.log` (`INJECT ... queued=True`, `Q_HELD`, `RECOVER*`, `FORCE_RESTART`). Esc/Stop can't un-stick an idle child;
`POST /api/session/<sid>/force-restart` re-delivers. Never kill a child mid-turn or running a tool.
Full checklist: `docs/stuck-session-triage.md`.
