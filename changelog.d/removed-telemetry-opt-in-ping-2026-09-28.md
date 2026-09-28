Removed the opt-in daily telemetry ping and its consent banner: the
dashboard no longer shows an "enable telemetry" bar or onboarding
checkbox, `POST /v1/ping` is no longer sent by this build, and no new
`install_id` is ever generated. `/api/telemetry/opt-in` and
`/api/telemetry/heartbeat` (and the active-seconds accounting behind
them) are removed from the server. `/api/telemetry/status` stays as
public API and now reports `retired: true`. Existing `install-id` /
`telemetry.json` files on disk are left untouched, never deleted. The
stats page keeps a legacy "Opt-in pings" tab showing data from before the
retirement.
