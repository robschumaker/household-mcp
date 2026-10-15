I'm handing you household-mcp-staging.zip. Unzip it into ~/household-mcp, make that directory a git repo, and read CLAUDE.md and SPEC.md before doing anything else.

Build phase 1 of the household MCP server exactly as SPEC.md specifies: tools recall, remember, list_topics, get_document, leave_note and inbox; FastMCP over Streamable HTTP bound to 127.0.0.1:8000; Google OAuth with the two-address allow-list read from .env; the markdown store under /home/pi/household with the attribution stamp on every line; SQLite FTS5 search; household-mcp.service; the rclone bisync timer for the Drive mirror; and smoke_test.py.

Start by checking the installed FastMCP version and reading its docs for the Google provider and the HTTP transport. Run everything locally on this laptop first, then deploy to the Pi over SSH (host raspberrypi, user pi). Do not touch the dashboard files or services on the Pi. Stop and ask before anything destructive. Never ask me for a secret; the real values are in /home/pi/household/.env on the Pi, which I fill in myself.

When phase 1 is done, commit, tag v0.1, and give me in plain words the exact remaining steps I do by hand to finish the two connectors.
