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
| `voice/` | Phase 4 voice assistant: `daemon.py` (wake word -> record -> whisper -> Claude -> Piper on the ReSpeaker), `brain.py` (the Claude turn with the store's tools in process), `audio.py`, `bench.py`; runs as `household-voice.service` from `venv-voice` |
| `nas_backup.py` | Nightly tar of the store (minus `.env`) uploaded to the Synology NAS at 192.0.2.10, folder `Backups/household`, over the File Station API; 14 dated copies kept; runs from pi's crontab at 03:30 |
| `scripts/keep_login.py` | One-time, run by the owner on the laptop: opens a Chromium window for Google sign-in, exchanges the oauth_token cookie for the Keep master token, installs it into the Pi's `.env` over SSH |
| `tests_keep_sync.py` | Fake-Keep unit test for the reconciliation, the 20-item hold and the notes ingest |
| `install.sh` | Deploy from the laptop: rsync, venv, units, restart |
| `smoke_test.py` | Standard-library checks against a running server (see its docstring) |

Tools: `recall`, `remember`, `list_topics`, `get_document`, `leave_note`, `inbox`, and for lists `add_item`, `remove_item`, `get_list`. Every stored line looks like `- [2026-10-15 owner/claude] fact`.

Notes carry an optional topic tag, and `inbox` takes an optional `topic` filter that returns that topic's notes plus untagged ones, so a chat about one subject does not see notes meant for another. Reading never clears a note: it stays until the reader calls `finish_note(id)`, which moves it to `<assistant>.read.jsonl`. `merge_topics(sources, into)` folds topics together (duplicate facts skipped) and `delete_topic` retires one; both move the old files under `log/` rather than erasing them. Three assistants have inboxes: `claude` (the owner's claude.ai Claude: trips, itinerary, household plans), `chatgpt` (the partner's) and `claude-code` (Claude Code on the owner's laptop: the Pi, this server, laptop work). claude.ai and Claude Code arrive through the same claude.ai connector, so the server reads the MCP `clientInfo` first and otherwise cannot tell them apart; Claude Code therefore calls `inbox` with `assistant=claude-code` and leaves notes addressed to `claude` alone. A Claude-family caller may read either Claude inbox; ChatGPT only its own.

Lists live in `lists/shopping.json` (the only list for now). `qty` is capped at 12, an existing item merges and bumps its count, names are matched case-insensitively, and every write returns the full list.

The Keep mirror treats Keep as a mirror, never the source of truth. Each run: items added at a speaker join the canonical list with the stamp `speaker`; items checked off in Keep are removed; items removed on the Pi (`remove_item`) are checked off in Keep rather than re-imported; then Keep is rewritten to match the Pi (qty shows as `Milk x2`). More than 20 new items in one run is held instead: `lists/shopping.review.json` is written, Claude gets one inbox note, and nothing changes until Keep is cleaned up. Unchecked entries in the `House notes` list become notes in Claude Code's inbox (`claude-code`) and are checked off. Until `KEEP_MASTER_TOKEN` is set the timer run exits 0 doing nothing.

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

Keep token, once, on the laptop (needs `pip install -r requirements-laptop.txt` and `playwright install chromium`):

```bash
cd ~/household-mcp && venv/bin/python scripts/keep_login.py
```

## Voice (phase 4)

A "Hey Claude" assistant on a Seeed ReSpeaker XVF3800 USB mic array plugged into the Pi, with a speaker on the array's amplifier output so its echo cancellation hears what the Pi says. Pipeline: openWakeWord (`hey_claude.onnx`, Apache-2.0, from huggingface.co/gdiamos/hey-claude) -> energy end-pointed recording -> speech recognition -> one Claude turn (Claude Code headless on the owner's subscription, one process kept warm with the store attached as a stdio MCP server, `voice/brain_cc.py` + `voice/store_mcp.py`; or the Claude API via `voice/brain.py` if `ANTHROPIC_API_KEY` is set) with `recall`, `remember`, `list_topics`, `get_list`, `add_item`, `remove_item`, `leave_note` calling `store.py` directly -> speech -> the ReSpeaker. Recognition and speech go to Deepgram (Nova-3 and Aura-2) when `DEEPGRAM_API_KEY` is set, because on this Pi 4 faster-whisper and Piper both run at about real time even with CPU priority (13 s to transcribe a 6 s sentence), which is too slow to talk to; Deepgram answers in 0.6 s and speaks in under 2 s. Without the key it falls back to local whisper and Piper. After each reply it listens a few more seconds for a follow-up without the wake word, and keeps the conversation for five minutes. Writes are stamped `voice/claude-voice`.

Setup on the Pi, beyond `./install.sh`: `sudo apt install libportaudio2`; put `hey_claude.onnx` and a Piper voice in `/home/pi/voice-models/`; log Claude Code in as pi on the Pi (`~/.local/bin/claude`, then /login) or set `ANTHROPIC_API_KEY`; set `DEEPGRAM_API_KEY` with `scripts/set_pi_secret.sh NAME` from a normal terminal (prompts for the value, never on a command line) and the `VOICE_*` lines in `.env`; `sudo systemctl enable --now household-voice.service`. The daemon exits quietly if the key is missing and fails (and restarts) if the ReSpeaker is not found. `voice/bench.py` times Piper and whisper models without a microphone.

Known wake-word limits of the published model, measured with Piper voices on the laptop: wakes reliably on US voices, misses a British voice entirely, and also wakes on "hey Clyde" and "clawed" (same phonemes). Train a replacement with openWakeWord's `automatic_model_training` notebook if that bites.

Measured on the Pi with Claude Code: a turn that calls a tool takes 4 to 6 s, one without 1.5 to 4 s, after a one-off warm-up at start. Model choice (default, sonnet, haiku) made little difference; the time is Claude Code and the tool round trip, not generation.
