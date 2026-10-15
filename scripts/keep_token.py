#!/usr/bin/env python3
"""Exchange a Google web oauth_token cookie for a Keep master token and store it in .env.

Run on the Pi as pi:  /home/pi/household-mcp/venv/bin/python scripts/keep_token.py
Prompts for the Google email and the oauth_token cookie value (hidden), then writes
KEEP_USERNAME and KEEP_MASTER_TOKEN into /home/pi/household/.env (mode 600).
Prints only "stored". The token is never printed or logged.
"""

import getpass
import os
import sys

ANDROID_ID = "3a9f1c2b7d4e6a58"  # fixed, arbitrary 16-hex device id
ENV_PATH = os.environ.get("ENV_FILE", "/home/pi/household/.env")


def write_env(path: str, values: dict) -> None:
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    done = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in values:
            out.append(f"{key}={values[key]}")
            done.add(key)
        else:
            out.append(line)
    for k, v in values.items():
        if k not in done:
            out.append(f"{k}={v}")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def main() -> int:
    import gpsoauth

    email = input("Google email: ").strip()
    oauth_token = getpass.getpass("oauth_token cookie value (hidden): ").strip()
    if not email or not oauth_token:
        print("email and oauth_token are both required", file=sys.stderr)
        return 2
    res = gpsoauth.exchange_token(email, oauth_token, ANDROID_ID)
    token = res.get("Token")
    if not token:
        print(f"exchange failed: {res.get('Error', 'no Token in response')}", file=sys.stderr)
        return 1
    write_env(ENV_PATH, {"KEEP_USERNAME": email, "KEEP_MASTER_TOKEN": token})
    print("stored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
