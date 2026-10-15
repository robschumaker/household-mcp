#!/usr/bin/env python3
"""Get a Google Keep master token by signing in through a browser window, and install it on the Pi.

Run on the laptop:  venv/bin/python scripts/keep_login.py

1. launches the installed Brave with a throw-away profile (Playwright attached over CDP), or
   Playwright's Firefox if Brave is missing, at https://accounts.google.com/EmbeddedSetup
2. you sign in and click "I agree"; the script waits for the oauth_token cookie
3. the browser is closed, the cookie is exchanged with gpsoauth for the master token
4. KEEP_USERNAME and KEEP_MASTER_TOKEN are written into /home/pi/household/.env over SSH
   (file mode stays 600), via stdin so nothing appears on a command line
5. prints "stored"

Nothing is printed, logged or left on the laptop: the temporary Brave profile is deleted on exit;
the Firefox fallback keeps its profile in memory only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
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


BRAVE_CANDIDATES = ["brave-browser", "brave-browser-stable", "brave", "/opt/brave.com/brave/brave-browser"]


def find_brave() -> str | None:
    for c in BRAVE_CANDIDATES:
        p = shutil.which(c) or (c if os.path.isabs(c) and os.access(c, os.X_OK) else None)
        if p:
            return p
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_cookie(context, page, timeout: int) -> str:
    print("Sign in to Google in the window that just opened, then click 'I agree'. Waiting...", file=sys.stderr)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for c in context.cookies():
            if c.get("name") == "oauth_token" and c.get("value"):
                return c["value"]
        if page.is_closed():
            raise SystemExit("browser window was closed before sign-in finished")
        page.wait_for_timeout(1000)
    raise SystemExit("timed out waiting for sign-in")


def cookie_via_brave(brave: str, timeout: int) -> str:
    """Real Brave with a throw-away profile, driven over the DevTools protocol."""
    from playwright.sync_api import sync_playwright

    profile = tempfile.mkdtemp(prefix="keep-login-")
    port = _free_port()
    proc = subprocess.Popen(
        [brave, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
         "--no-first-run", "--no-default-browser-check", "--disable-sync", "--new-window", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,  # own process group: brave-browser is a wrapper that execs the real binary
    )
    try:
        with sync_playwright() as p:
            browser = None
            for _ in range(60):
                try:
                    browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    break
                except Exception:
                    if proc.poll() is not None:
                        raise SystemExit("Brave exited before the DevTools port came up")
                    time.sleep(0.5)
            if browser is None:
                raise SystemExit("could not connect to Brave over CDP")
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(SETUP_URL, wait_until="domcontentloaded")
            try:
                return _wait_for_cookie(context, page, timeout)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    finally:
        _stop_group(proc)
        for _ in range(10):  # Brave may still be flushing the profile as it dies
            shutil.rmtree(profile, ignore_errors=True)
            if not os.path.exists(profile):
                break
            time.sleep(0.5)


def _stop_group(proc: subprocess.Popen) -> None:
    """Terminate the browser and every helper process in its group, then wait for them to go."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        for _ in range(50):
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.2)


def cookie_via_firefox(timeout: int) -> str:
    """Fallback: Playwright's Firefox with an in-memory profile."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(SETUP_URL, wait_until="domcontentloaded")
        try:
            return _wait_for_cookie(context, page, timeout)
        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass


def get_oauth_cookie(timeout: int) -> str:
    brave = find_brave()
    if brave:
        print(f"using Brave at {brave}", file=sys.stderr)
        return cookie_via_brave(brave, timeout)
    print("Brave not found; using Playwright Firefox", file=sys.stderr)
    return cookie_via_firefox(timeout)


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
