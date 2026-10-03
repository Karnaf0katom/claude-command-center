# Voice mode (Codex realtime)

Talk to Claude Command Center by voice: press the mic button in the top bar,
speak, and a voice answers questions about your board: what's waiting on
you, what a session is doing, how the queues look. It can also act: "send
the ads session a nudge" becomes a confirmation card in the dashboard that
runs only when you click **Confirm**.

## Requirements

- **Codex CLI 0.160.0 or newer** installed (`codex app-server` ships the
  realtime voice host this builds on).
- **An OpenAI API key in a BYOK profile** (Settings > BYOK, provider
  `openai`). The realtime lane is billed by OpenAI to that key, per minute
  of audio.
- **A WebRTC-capable browser** (Chrome, Edge, Safari, Firefox) and a
  microphone.

> **ChatGPT-login limitation:** the realtime lane requires API-key auth.
> A Codex install signed in with a ChatGPT subscription alone returns
> `realtime conversation requires API key auth`. The OpenAI BYOK key is
> what makes it work. Voice mode injects the key into the Codex child
> process's environment only: it is never written to disk, never passed on
> the command line, never logged, and never returned from any API. Your
> `~/.codex/auth.json` is not read or modified.

> **Experimental:** `thread/realtime/*` is an experimental Codex app-server
> API. Method names and event shapes can change between CLI releases.

## Using it

1. Click the **mic button** in the top bar to open the voice panel.
2. Press **Start talking** and allow microphone access. The panel shows
   `connecting` then `listening`.
3. Speak naturally. Transcript lines for you and the voice stream into
   the panel in real time, and the reply is spoken aloud.
4. If the voice proposes an action (injecting into a session, spawning
   one, filing a WatchTower ticket), a card appears in the panel. Nothing
   runs until you click **Confirm** or **Dismiss**: spoken "yes" is never
   enough, by design.
5. Press **Stop** (or close the tab) to end the session. The red dot on
   the mic button means a session is live and billing.

## Settings (Settings > Voice mode)

- **Voice**: pick any realtime voice the installed Codex advertises
  (defaults to `marin`).
- **BYOK profile**: which keychain profile supplies the OpenAI key.
  Auto picks the first profile that has one.
- **Max session**: hard stop (default 15 minutes).
- **Stop after silence**: auto-stop after this much quiet (default 120
  seconds).
- **Save transcripts**: off by default. When on, transcripts land in
  `~/.claude/command-center/voice/transcripts/` (mode 0600). Raw audio is
  never stored anywhere.

## How it works

- The browser's `RTCPeerConnection` peers directly with the Codex voice
  host on loopback, so audio never transits the CCC server. CCC relays only
  the SDP offer/answer and streams transcript/state events to the panel
  over SSE (`/api/voice/events`).
- The backing Codex thread is ephemeral, sandboxed read-only, with
  `approvalPolicy: never`. Its only window into CCC is four dynamic
  tools: `ccc_attention`, `ccc_session`, `ccc_queues` (all read-only)
  and `ccc_propose_action`, which can only create a pending-action card
  through the same confirmation path the Ask agent uses. Approval
  requests from the model are denied outright.
- Each session seeds a small briefing from already-cached board data
  (attention feed + queue rollup) so the voice can answer "what needs
  me" without any scanning.
- **One voice session at a time** per CCC server; a second start
  returns `voice_busy`.
- Sessions end on stop, on silence, at the max length, on tab close
  (beacon + heartbeat watchdog), or when the server exits. Every session
  records a usage-ledger entry (duration, provider `openai`).

## API

| Route | Purpose |
|---|---|
| `POST /api/voice/start` | Start a session. Body: `sdp_offer` (WebRTC offer), optional `voice`, `profile`. Returns `session_id` + `sdp_answer`. |
| `POST /api/voice/stop` | Stop the active session (`session_id`, `reason`). |
| `POST /api/voice/heartbeat` | Browser liveness check-in (`session_id`). |
| `GET /api/voice/status` | Active/last session, config, which BYOK profiles hold an OpenAI key. |
| `GET /api/voice/events?session_id=…&after=N` | SSE: `state`, `transcript_delta`, `transcript`, `action`, `tool`, `error`, `closed`. |
| `GET /api/voice/voices` | Voice catalog (live `listVoices` once a session has run, else built-in list). |
| `GET/POST /api/voice/config` | Read/write voice settings. |

Typed errors: `voice_no_openai_key`, `voice_busy`, `voice_no_codex`,
`voice_bad_request`, `voice_start_failed`, `voice_no_session`.

## Cost and privacy

- OpenAI bills realtime audio per minute against the BYOK key. A short
  check-in is cents; the 15-minute default cap exists so a forgotten live
  session can't run for hours. The silent-timeout (2 min default) stops
  sessions you walk away from.
- Mic audio goes browser → local voice host → OpenAI realtime API. CCC
  sees transcripts only, keeps them in memory for the session, and never
  stores raw audio.
- Board data reaches the model only inside the realtime session you
  started.

## Troubleshooting

- **"Voice needs an OpenAI API key"**: add one under Settings > BYOK
  (provider `openai`), then pick that profile in Settings > Voice mode.
- **"Codex CLI not found"**: install/update Codex CLI (0.160.0+); CCC's
  usual codex binary resolution applies.
- **Mic permission denied**: the browser blocked it; allow mic for the
  CCC origin and retry.
- **Nothing is heard back**: check the panel state pill: `thinking`
  means the backing thread is working; `listening` with silence after a
  reply usually means system output volume or a muted speaker icon in
  the panel.
- **`voice_busy`**: another tab or window has the live session; only one
  runs at a time.

## Known limits

- Experimental API surface; Codex CLI upgrades can shift behavior.
- Single session, single dashboard instance.
- The voice can describe board state but not read full transcripts aloud
  (by design: the read-only tools return summaries, not raw logs).
