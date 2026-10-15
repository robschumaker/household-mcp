# household-mcp

A household memory server on the Raspberry Pi: one MCP endpoint that the owner's Claude and the partner's ChatGPT both read and write. The full design is in `SPEC.md`; `CLAUDE.md` holds the working rules.

## Phase 1 (v0.1) memory and coordination, phase 2 (v0.2) lists

| File | Purpose |
| --- | --- |
| `server.py` | FastMCP server: the six tools, `/health`, signed document downloads, Streamable HTTP on `127.0.0.1:8000/mcp` |
| `store.py` | The file store under `/home/pi/household`: markdown topics with attribution stamps, FTS5 search, documents, inboxes, write log |
| `auth.py` | `.env` loading, Google OAuth provider (scopes `openid` + `email`), the allow-list gate (403), the local-only `DEV_BYPASS`, caller detection |
| `sync.sh`, `rclone-filter.txt` | `rclone bisync /home/pi/household gdrive:`; `.env`, `sa.json` and `secrets/` never leave the Pi |
| `systemd/` | `household-mcp.service`, `household-sync.service` + `household-sync.timer` (every 5 min) |
| `keep_sync.py` | Google Keep mirror of the shopping list and the House notes dictation inbox, run by `household-keep.timer` every 5 min |
| `scripts/keep_token.py` | One-time, run by the owner on the Pi: exchanges an oauth_token cookie for the Keep master token and writes it into `.env` |
| `tests_keep_sync.py` | Fake-Keep unit test for the reconciliation, the 20-item hold and the notes ingest |
| `install.sh` | Deploy from the laptop: rsync, venv, units, restart |
| `smoke_test.py` | Standard-library checks against a running server (see its docstring) |

Tools: `recall`, `remember`, `list_topics`, `get_document`, `leave_note`, `inbox`, and for lists `add_item`, `remove_item`, `get_list`. Every stored line looks like `- [2026-10-15 owner/claude] fact`.

Lists live in `lists/shopping.json` (the only list for now). `qty` is capped at 12, an existing item merges and bumps its count, names are matched case-insensitively, and every write returns the full list.

The Keep mirror treats Keep as a mirror, never the source of truth. Each run: items added at a speaker join the canonical list with the stamp `speaker`; items checked off in Keep are removed; then Keep is rewritten to match the Pi (qty shows as `Milk x2`). More than 20 new items in one run is held instead: `lists/shopping.review.json` is written, Claude gets one inbox note, and nothing changes until Keep is cleaned up. Unchecked entries in the `House notes` list become notes in Claude's inbox and are checked off. Until `KEEP_MASTER_TOKEN` is set the timer run exits 0 doing nothing.

Which assistant is calling is worked out from the OAuth client name Claude or ChatGPT registered, then the User-Agent, then the position of the signed-in email in `ALLOWED_EMAILS` (first is Claude's user, second is ChatGPT's). Every write records it in `log/writes.jsonl`.

## Run locally

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cat > /tmp/dev.env <<'E'
BASE_URL=http://localhost:8000
ALLOWED_EMAILS=owner@example.com,partner@example.com
STORE_DIR=/tmp/household-store
DEV_BYPASS=devtoken
E
ENV_FILE=/tmp/dev.env venv/bin/python server.py
venv/bin/python smoke_test.py --url http://localhost:8000 --token devtoken --dev-emails owner@example.com,partner@example.com
```

`DEV_BYPASS` only works when `BASE_URL` is localhost; the server refuses to start otherwise.

## Deploy

```bash
./install.sh            # pi@raspberrypi
ssh pi@raspberrypi 'cd household-mcp && ./sync.sh --resync'   # first Drive mirror only
venv/bin/python smoke_test.py --url https://raspberrypi.tail1234.ts.net
```

Logs: `journalctl -u household-mcp -f` on the Pi; the Drive mirror writes `data/sync.log`, the Keep mirror `data/keep_sync.log` and `journalctl -u household-keep`.

Keep token, once, on the Pi as pi:

```bash
cd /home/pi/household-mcp && venv/bin/python scripts/keep_token.py
```
