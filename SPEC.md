# Household MCP Server — Spec v0.1

Oct 15, 2026 · the owner

## Goal and non-goals

One household memory that the owner's Claude and the partner's ChatGPT both read and write, with the Google Home speakers meeting it through Keep and Hubitat. The store lives on the Pi in plain markdown that you own; neither vendor holds it.

In scope:

- Shared facts and decisions (the Europe trip, household plans) readable by both assistants without re-uploading anything
- Notes between agents: the partner's assistant can leave a note for Claude and vice versa
- Documents such as the current prospectus, fetched by name instead of uploaded
- The grocery list, with the Keep mirror so the speakers stay in sync, and guards against the thousand-Diet-Coke failure

Out of scope for now:

- Gemini Spark custom connectors: plan gating is unclear and the shared Drive folder already gives Gemini everything it needs to read
- Hubitat device control through the server: later, once the memory side is proven
- Anything the speakers should hear from the server: phase 3, after the store is stable

## Architecture

&#91;embedded content: household memory architecture · 3 clients, 1 server, 2 stores\]

Claude and ChatGPT reach the Pi through one public HTTPS name; the speakers never touch the server and instead share lists with it through Keep. Hubitat joins in phase 3 (events into the server, announcements out to the speakers) and is left off the picture until then.

## Data layout on the Pi

Everything lives under `/home/pi/household/` as plain files you can read with `cat` and back up with `cp`.

| Path | Holds | Format |
| --- | --- | --- |
| `memory/<topic>.md` | Shared facts and decisions, one file per topic (`europe-trip.md`, `household.md`, `people.md`) | Markdown, one fact per line |
| `documents/` | Files fetched by name: the current prospectus, the trip to-do list, anything either assistant should hand over whole | PDF, Markdown, text |
| `inbox/<assistant>.jsonl` | Open notes left for Claude, for ChatGPT or for Claude Code (`claude-code`), each optionally tagged with a topic; a note stays until `finish_note` moves it to `<assistant>.read.jsonl` | One JSON object per line |
| `lists/shopping.json` | The canonical grocery list and its Keep mirror state | JSON |
| `log/writes.jsonl` | Every write, who asked, which assistant, when | One JSON object per line |

Every memory line carries an attribution stamp so either of you can see where a fact came from:

```markdown
- [2026-10-15 owner/claude] Strasbourg day is Dec 20, train from Paris Est at 09:52
- [2026-10-16 partner/chatgpt] A relative wants a quiet morning on Dec 21
```

Search is full text with SQLite FTS5 over the memory files, rebuilt on each write. No embeddings: at household scale they add nothing and would be the one thing that makes the Pi sweat.

## Tool schema

Everything is a tool, never an MCP resource, because ChatGPT only consumes tools. Every call records the signed-in Google account and the calling assistant.

**Phase 1: memory and coordination**

| Tool | Arguments | Returns | Rule |
| --- | --- | --- | --- |
| `recall` | `query`, optional `topic` | Up to 20 matching lines with their stamps and topic | Full-text match, newest first |
| `remember` | `topic`, `fact` | The stored line as written, with its stamp | Appends one line; a topic that does not exist is created |
| `list_topics` | none | Topic names with line counts and last-updated dates | Cheap orientation call |
| `get_document` | `name` | The document's text, or a download link for a PDF | Names come from `documents/`; no path traversal |
| `leave_note` | `for` (claude, chatgpt or claude-code), `text`, optional `topic` | Confirmation with the note id | Lands in the other assistant's inbox |
| `inbox` | optional `topic`, optional `assistant` | Open notes for the calling assistant (Claude Code passes `assistant=claude-code`), oldest first; with `topic`, that topic's notes plus untagged ones | Never clears; `finish_note(id)` removes a note when its task is done |
| `finish_note` | `id`, optional `assistant` | The finished note and how many remain | Moves it to the read log |
| `merge_topics` | `sources`, `into` | Counts added and skipped | Sources retired to `log/merged-topics/<date>/` |
| `delete_topic` | `topic` | Line count and where the file went | Retired to `log/deleted-topics/<date>/` |

**Phase 2: lists**

| Tool | Arguments | Returns | Rule |
| --- | --- | --- | --- |
| `add_item` | `list`, `item`, optional `qty` (default 1) | The full list after the write | `qty` capped at 12; an existing item merges and bumps its count rather than adding a row |
| `remove_item` | `list`, `item` | The full list after the write | Case-insensitive match; no match returns the list unchanged with a note |
| `get_list` | `list` | The full list with counts and who added what | Reads the canonical copy, not Keep |

Read-back is the verification: a tool never answers "done", it answers with the state it produced, and the assistant reports that state to you.

Validation lives in code, not in prompts: quantity caps, duplicate merging, list-name allow-list, document-name allow-list, and a 2,000-character ceiling on any single fact or note.

## Auth and exposure

Two Google accounts can call the server and nobody else; the public name is a Tailscale Funnel hostname, so no domain, DNS record or router port changes.

**Google OAuth client (one-time, about 15 minutes)**

1. Google Cloud console: new project `household-mcp`, OAuth consent screen set to External.
2. Scopes: only `openid` and `email`. Both are non-sensitive, so the app can be published without Google's verification review and tokens do not expire every 7 days the way Testing mode forces.
3. Create an OAuth client of type Web application. Redirect URI: `https://<pi-funnel-name>.ts.net/auth/callback`.
4. Put the client id and secret in `/home/pi/household/.env`, mode 600, never in the repo.

**Server side**

- FastMCP's Google provider handles the OAuth dance, including the dynamic client registration ChatGPT expects.
- After sign-in the server checks the token's email against an allow-list of two addresses in `.env`. Any other Google account gets a 403 before any tool runs.
- The server binds to `127.0.0.1:8000` only; Funnel is the sole way in.

**Tailscale Funnel (about 10 minutes)**

1. `tailscale up` on the Pi and on your laptop; enable HTTPS certificates and Funnel in the tailnet admin console.
2. `tailscale funnel 8000` on the Pi. The console prints the public name, something like `raspberrypi.tail1234.ts.net`.
3. That name goes into the OAuth redirect URI above and into both connector settings.

Funnel carries public traffic by design. The Google sign-in is what gates access, so the server is never exposed without the allow-list in place. Bonus: Tailscale on the laptop gives you SSH and SCP to the Pi from anywhere.

## Drive sync

The Pi keeps the store mirrored into a `Household` folder in your Google Drive, so the memory is readable by all three assistants and backed up without the server being involved.

- A separate rclone remote on the Pi, with its own token. Nothing on the laptop's rclone mount changes, and no existing file moves.
- `rclone bisync /home/pi/household gdrive:Household` on a systemd timer every 5 minutes. Two-way: a prospectus I update in Drive shows up in `documents/` on the Pi; a fact ChatGPT writes on the Pi shows up in Drive.
- Conflicts are rare (two writers on one line within five minutes) and bisync keeps both copies with a suffix rather than losing one.
- The `.env` file and the Keep master token are excluded from the sync with a filter file; secrets never leave the Pi.

The `Household` folder is also the one shared with the partner as editor, so Drive sharing and the memory mirror are a single folder, not two systems.

## Grocery list and the Keep mirror

The canonical list lives on the Pi; Google Keep is a mirror so the speakers stay in sync. If the mirror breaks you lose speaker sync for a while, never the list.

**Why Keep, and why it is the fragile part**

- The Google Home speakers write shopping lists to Keep and nothing else; third-party list apps were dropped.
- Google's official Keep API exists only for enterprise accounts. For a personal Gmail the route is gkeepapi, an unofficial client with a master-token login. The Home Assistant Google Keep Sync integration runs on the same library.
- It works for long stretches, then breaks for a few weeks when Google changes its auth. Treat it as a mirror, never as the source of truth.

**Sync loop (every 5 minutes, its own systemd timer)**

1. Pull Keep's `Shopping` list; any item not in the canonical list was added at a speaker, so it is added with the stamp `speaker`.
2. Any item checked off in Keep is removed from the canonical list.
3. Push the canonical list back so Keep shows exactly what the Pi has.
4. Log every difference. A list that grows by more than 20 items in one sync is held for review instead of pushed; that is the thousand-Diet-Coke guard at the mirror level.

**Guards on the tool side**

- `qty` capped at 12; an existing item merges and bumps its count rather than adding a row.
- Item names normalised (trimmed, lower-cased for matching, display case kept).
- The tool returns the full list after every write; that read-back is what the assistant reports.

**Master token**

Generated once on the Pi with the gkeepapi helper (or the Home Assistant community's Docker one-liner), stored in `.env` with mode 600, excluded from the Drive sync. It is a powerful credential; a dedicated Google account that owns only the lists is the cleaner option if you would rather not use yours.

## Google Home seams (phase 3)

Google Home has no inbound API for a personal account and the speakers do not consume MCP, so the server meets them at the four places they already touch the outside world.

| Seam | Direction | How | Needs |
| --- | --- | --- | --- |
| Dictation inbox | Voice in, free text | A Keep list named `House notes`; the Keep sync ingests each entry as a note for Claude or as a memory fact | Nothing new; same gkeepapi mirror pointed at a second list |
| Voice commands | Voice in, one bit | Google Home routine flips a Hubitat virtual switch; Hubitat already POSTs device events to the Pi, so the server gets a hook | A virtual switch per phrase in Hubitat |
| Announcements | Server out | The Pi casts text-to-speech to a Nest speaker, via Hubitat's Chromecast integration or pychromecast on the LAN | Speaker IPs on the LAN; nothing from Google |
| Calendar | Server out | Events written through the official Google Calendar API land on the shared calendar, which the speakers read back in the morning | Calendar scope added to the OAuth client |

Device state ("is the barn door open") goes through the Hubitat Maker API, the same device set Google Home controls, so the server and the speakers never disagree. Nest-branded hardware, if any, has its own official Device Access API.

When Home Assistant lands on the Pi, these seams become HA integrations and the server talks to HA instead.

## Client wiring and standing instructions

Neither assistant checks the server on its own; each needs a standing order, or the memory is a library nobody visits.

**Claude (owner)**

1. Settings, Connectors, Add custom connector. Name `Household`, URL `https://<pi-funnel-name>.ts.net/mcp`. Sign in with your Google account when prompted.
2. Enable it for a conversation from the + menu.
3. Preference line for Claude's memory, saved once the server is live: in any trip, household or shared-plans chat, call `inbox` first, passing `topic` when the chat is about one thing (for example europe-december-2026-trip) so other subjects' notes stay out of the way, then `recall`. Record decisions with `remember`. Leave the partner's assistant or Claude Code a note with `leave_note`, tagged with a `topic`, when something changes they should know. Notes are never cleared by reading: when a note's task is done, call `finish_note` with its id.

**ChatGPT (partner)**

1. Settings, Apps and Connectors, Advanced, Developer mode on. This is set up on the web, then works on their phone.
2. Create a connector: name `Household`, the same URL. Choose OAuth; ChatGPT discovers the rest. Sign in with their Google account.
3. Enable the connector in each chat where she wants it (ChatGPT does not keep it on by default).
4. Custom instructions, pasted as written:

```markdown
When a conversation involves our household, trips, shared plans, shopping or anything we share, start by calling the Household connector's inbox tool (pass topic when the chat is about one subject, such as europe-december-2026-trip) and then recall. Record decisions and new facts with remember. If something changes that the owner or their assistant should know, use leave_note for claude with a topic tag. Reading the inbox never removes a note: when I have dealt with a note, call finish_note with its id. When I add to a shopping list, use add_item and tell me the full list it returns.
```

**Gemini and the speakers**

No connector. Gemini reads the shared `Household` Drive folder natively, and the speakers get lists through Keep. If Google opens custom connectors to the AI Pro plan, the same URL works there too.

## Build plan

Four phases, each usable on its own; nothing in a later phase is needed for an earlier one to pay off. Times are my guesses for your hands-on part; the code side is mine.

| Phase | What ships | Owner does | Claude does | Your time |
| --- | --- | --- | --- | --- |
| 0. Drive share | The partner's ChatGPT reads the prospectus directly | Share the `Household` folder with the partner; the partner connects Drive in ChatGPT | Nothing | 15 min |
| 1. Memory server | `recall`, `remember`, `list_topics`, `get_document`, `leave_note`, `inbox`; Funnel; Google sign-in; Drive mirror; both connectors wired | Create the OAuth client, run `tailscale funnel`, copy the server to the Pi, set up both connectors and the standing instructions | `server.py`, systemd units, rclone timer, `.env` template, an install script, a smoke test | One evening, 2 to 3 h |
| 2. Lists | `add_item`, `remove_item`, `get_list`; Keep mirror with its guards | Generate the Keep master token; create the `Shopping` and `House notes` lists in Keep if they do not exist | `keep_sync.py`, its timer, the review hold, the dictation-inbox ingest | 1 h |
| 3. Google Home seams | Routine hooks via Hubitat, speaker announcements, Calendar writes | Create the virtual switches in Hubitat; add the Calendar scope to the OAuth client | Hook endpoint, pychromecast announcer, Calendar tool | 1 to 2 h, when wanted |

After phase 1 the acceptance test is simple: the partner asks ChatGPT what the current Strasbourg plan is and gets the answer from `recall` without uploading anything; I leave the partner's assistant a note and it shows up in their next chat.

Delivery of code: a small git repo (`household-mcp`) you clone onto the Pi; each phase is a tagged release so you can stop at any one.

## Open decisions

Five answers unblock phase 1; the rest can wait.

- [ ] the partner's ChatGPT plan is Plus or higher (needed for both the Drive app and custom connectors)
- [ ] The Pi 4B's RAM: `free -h` next time you are on it. Anything from 2 GB up is fine for this; it only matters for the Home Assistant migration later
- [ ] Which Google account owns the OAuth client and the Drive `Household` folder: yours, or a dedicated household account
- [ ] Keep master token: your account, or a dedicated account that owns only the lists
- [ ] Go-ahead on this spec, after which I write the phase 1 code

Later:

- [ ] Which Keep lists exist today, and their exact names
- [ ] Whether you own any Nest-branded hardware (phase 3 only)
- [ ] Whether the `House notes` dictation list should feed memory directly or only Claude's inbox

Assumptions I made: the Pi stays on 24/7 as it does for the dashboard; the Household folder is a normal My Drive folder, not a Workspace Shared Drive; FastMCP's Google provider is used as of its 2.12-era API and will be checked against the installed version on day one.

## Implementation runbook

Do these in order. Claude Code does the build step and everything inside it; your hands-on parts add up to about two hours, spread over an evening. Replace `<funnel-name>` everywhere with the name Tailscale prints in the Funnel step.

**Today, before anything else (15 min)**

- [ ] Drive: create a `Household` folder in My Drive. Put a copy or a shortcut of the current prospectus in it and leave the original where it is. Share the folder with the partner's Google account as Editor.
- [ ] the partner: ChatGPT, Settings, Apps and Connectors, Google Drive, connect their Google account. Test by @mentioning the prospectus in a chat.

**Repo (10 min)**

- [ ] On the laptop: `mkdir ~/household-mcp && cd ~/household-mcp && git init`. Unzip the staged bundle into it.
- [ ] Export this doc as Markdown and save it in the repo as `SPEC.md`, so Claude Code builds from the latest version.
- [ ] `git add -A && git commit -m "spec and scaffolding"`. The `.env.example` stays a template; the real `.env` lives only on the Pi.

**Tailscale and Funnel (15 min)**

- [ ] Laptop: install Tailscale, `sudo tailscale up`, sign in.
- [ ] Pi: `curl -fsSL https://tailscale.com/install.sh | sh`, then `sudo tailscale up` and sign in with the same account.
- [ ] Admin console at login.tailscale.com: DNS, enable HTTPS certificates.
- [ ] Pi: `sudo tailscale funnel --bg 8000`. The first run prints a link to enable Funnel for the tailnet; approve it, run the command again, and note the public name it prints (`raspberrypi.tailXXXX.ts.net`).
- [ ] Check from the laptop: `ssh pi@raspberrypi` works off the LAN now.

**Google OAuth client (15 min)**

- [ ] console.cloud.google.com: new project `household-mcp`.
- [ ] APIs and Services, OAuth consent screen: External, app name Household, your email, scopes `openid` and `email`, then Publish app. No verification review is needed for those two scopes.
- [ ] Credentials, Create credentials, OAuth client ID, Web application. Authorized redirect URI: `https://<funnel-name>/auth/callback`. Copy the client ID and secret.
- [ ] Pi: `mkdir -p /home/pi/household`, copy `.env.example` there as `.env`, fill in the client ID, secret, `BASE_URL` and the two `ALLOWED_EMAILS`, then `chmod 600 /home/pi/household/.env`.

**rclone remote on the Pi (10 min)**

- [ ] Pi: `rclone config`, new remote named `gdrive`, type Google Drive, full `drive` scope. When it asks whether to use a web browser, answer No; it prints an `rclone authorize` command to run on the laptop, which hands back a token to paste.
- [ ] Confirm with `rclone lsd gdrive:Household`.

**Hand off to Claude Code (one evening)**

- [ ] `cd ~/household-mcp && claude`, then paste the contents of `KICKOFF.md`.
- [ ] When it asks for the Funnel name or the Pi hostname, answer. When it asks for a secret, point it at `/home/pi/household/.env` instead.
- [ ] Done when `systemctl status household-mcp` is active on the Pi, `https://<funnel-name>/mcp` answers, and `smoke_test.py` passes.

**Connectors (15 min)**

- [ ] Claude: Settings, Connectors, Add custom connector, name `Household`, URL `https://<funnel-name>/mcp`, sign in with Google. Then tell me the household server is live and I will save my standing instruction.
- [ ] the partner, on the web: ChatGPT Settings, Apps and Connectors, Advanced, Developer mode on; Create connector, name `Household`, same URL, OAuth, sign in with their Google account. Paste the custom instructions from the Client wiring section.
- [ ] Allow-list check: sign in from any third Google account; it must be refused.

**Acceptance test**

- [ ] I store a Strasbourg fact with `remember`; the partner asks ChatGPT for the Strasbourg plan and gets it from `recall`.
- [ ] I leave the partner's assistant a note; it shows up in their next chat through `inbox`.

**Phase 2, when you want it**

- [ ] Generate the Keep master token on the Pi with gkeepapi's documented method, add `KEEP_USERNAME` and `KEEP_MASTER_TOKEN` to `.env`, and make sure the `Shopping` and `House notes` lists exist in Keep.
- [ ] Tell Claude Code: build phase 2 of SPEC.md.
