Removed the opt-in daily telemetry ping and its consent banner: the
dashboard no longer shows an "enable telemetry" bar or onboarding
checkbox, `POST /v1/ping` is no longer sent by this build, and no new
`install_id` is ever generated. The active-seconds accounting behind the
old heartbeat is gone. `/api/telemetry/opt-in` and
`/api/telemetry/heartbeat` stay as inert no-op stubs (they're public API,
and a dashboard tab left open from before the upgrade keeps calling the
heartbeat every 30s until it reloads) instead of 404ing.
`/api/telemetry/status` stays as public API and now reports
`retired: true`. Existing `install-id` / `telemetry.json` files on disk
are left untouched, never deleted. The stats page keeps a legacy "Opt-in
pings" tab showing data from before the retirement.
