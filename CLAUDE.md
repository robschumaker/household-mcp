# household-mcp — working rules for Claude Code

Read SPEC.md first. Build **phase 1 only** unless the owner says otherwise.

## Target machine
- Raspberry Pi 4B, hostname `raspberrypi`, user `pi`. LAN IP 192.0.2.4; reachable by hostname over Tailscale once the owner has set it up. SSH/SCP only — there is no browser on the Pi you can drive.
- Repo deploys to `/home/pi/household-mcp/` with rsync over SSH; the data store is `/home/pi/household/` (memory/, documents/, inbox/, lists/, log/). Python venv at `/home/pi/household-mcp/venv`.
- The Pi is also a kiosk display. Do **not** touch `/home/pi/Downloads/` or the units `dashboard.service`, `hubspace-bridge.service`, `pir-display.service`. Never reboot the Pi without asking.
- The server binds to `127.0.0.1:8000`. Tailscale Funnel (the owner sets it up) is the only public path. Never bind to 0.0.0.0.

## Stack
- Python 3.11+ (whatever the Pi has) and FastMCP (`jlowin/fastmcp`). **First step:** `pip show fastmcp`, then read the installed version's docs for the Google OAuth provider and the Streamable HTTP transport. Do not assume the API from memory; it changes between minor versions.
- Search: SQLite FTS5 over `memory/*.md`, rebuilt on each write. No embeddings, no ML models, nothing that pegs a Pi CPU.
- Drive mirror: `rclone bisync /home/pi/household gdrive:Household` on a systemd timer every 5 minutes, with a filter file that excludes `.env` and anything under `secrets/`. The `gdrive` remote is configured by the owner; do not run `rclone config` yourself. First run needs `--resync`.

## Secrets
- Never commit `.env`. Never print its contents. Never ask the owner to paste a secret into this session; they fill `/home/pi/household/.env` themselves from `.env.example`.
- The Keep master token (phase 2) is the owner's to generate. Do not script a Google login with their password.

## Definition of done (phase 1)
- Tools `recall`, `remember`, `list_topics`, `get_document`, `leave_note`, `inbox` behave as the SPEC.md tool table says, including the validation rules and the attribution stamp on every stored line.
- Google OAuth works end to end through the Funnel URL; any email outside `ALLOWED_EMAILS` gets a 403 before any tool runs.
- `household-mcp.service` and `household-sync.timer` installed and enabled on the Pi.
- `smoke_test.py` passes against the live Funnel URL. A dev-only bypass token may exist for local runs but must be impossible to enable in production (refuse to start if `DEV_BYPASS` is set and `BASE_URL` is not localhost).
- The acceptance test in SPEC.md passes.
- Tag `v0.1`.

## Ways of working
- Run locally on the laptop first (dev mode), then deploy to the Pi.
- Small commits with plain messages.
- Ask before anything destructive on the Pi: `rm -r`, disabling an existing unit, `apt remove`, editing anything outside the two directories above.
- Report what you ran and what came back. The owner often reads from a phone and cannot see the Pi.
- Prefer boring code: standard library where it will do, one file per concern (`server.py`, `store.py`, `auth.py`, `sync.sh`), no frameworks beyond FastMCP.
