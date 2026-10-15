#!/usr/bin/env python3
"""Get a Google Keep master token by signing in through a browser window, and install it on the Pi.

Run on the laptop:  venv/bin/python scripts/keep_login.py

1. opens a fresh Chromium window (Playwright) at https://accounts.google.com/EmbeddedSetup
2. you sign in and click "I agree"; the script waits for the oauth_token cookie
3. the browser is closed, the cookie is exchanged with gpsoauth for the master token
4. KEEP_USERNAME and KEEP_MASTER_TOKEN are written into /home/pi/household/.env over SSH
   (file mode stays 600), via stdin so nothing appears on a command line
5. prints "stored"

Nothing is printed, logged or written on the laptop: the browser profile is in memory only.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time

ANDROID_ID = "3a9f1c2b7d4e6a58"
SETUP_URL = "https://accounts.google.com/EmbeddedSetup"
WAIT_SECONDS = 15 * 60

REMOTE_INSTALL = r'''
import json, os, sys
d = json.load(sys.stdin); path = d["path"]; values = d["values"]
lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
out, done = [], set()
for line in lines:
    key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
    if key in values:
        out.append(f"{key}={values[key]}"); done.add(key)
    else:
        out.append(line)
for k, v in values.items():
    if k not in done:
        out.append(f"{k}={v}")
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write("\n".join(out) + "\n")
os.replace(tmp, path); os.chmod(path, 0o600)
print("ok")
'''


def get_oauth_cookie(timeout: int) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()  # in-memory profile: nothing persists on disk
        page = context.new_page()
        page.goto(SETUP_URL, wait_until="domcontentloaded")
        print("Sign in to Google in the window that just opened, then click 'I agree'. Waiting...", file=sys.stderr)
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                for c in context.cookies():
                    if c.get("name") == "oauth_token" and c.get("value"):
                        return c["value"]
                if page.is_closed():
                    raise SystemExit("browser window was closed before sign-in finished")
                page.wait_for_timeout(1000)
            raise SystemExit("timed out waiting for sign-in")
        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass


def install_on_pi(host: str, env_path: str, email: str, token: str) -> None:
    payload = json.dumps({"path": env_path, "values": {"KEEP_USERNAME": email, "KEEP_MASTER_TOKEN": token}})
    # the program travels base64-encoded as one argument (the remote shell re-splits argv);
    # the secret travels on stdin only
    prog = base64.b64encode(REMOTE_INSTALL.encode()).decode()
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host,
         f"python3 -c \"import base64;exec(base64.b64decode('{prog}'))\""],
        input=payload, capture_output=True, text=True,
    )
    if proc.returncode != 0 or proc.stdout.strip() != "ok":
        raise SystemExit("install on the Pi failed: " + (proc.stderr.strip().splitlines() or ["unknown error"])[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="pi@raspberrypi")
    ap.add_argument("--env-path", default="/home/pi/household/.env")
    ap.add_argument("--email", help="Google account email; prompted if omitted")
    a = ap.parse_args()

    import gpsoauth

    email = (a.email or input("Google account email for Keep: ")).strip()
    if not email:
        print("email is required", file=sys.stderr)
        return 2

    oauth_token = get_oauth_cookie(WAIT_SECONDS)
    res = gpsoauth.exchange_token(email, oauth_token, ANDROID_ID)
    del oauth_token
    token = res.get("Token")
    if not token:
        print(f"token exchange failed: {res.get('Error', 'no Token in response')}", file=sys.stderr)
        return 1
    install_on_pi(a.host, a.env_path, res.get("Email") or email, token)
    del token, res
    print("stored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
