#!/usr/bin/env python3
"""Smoke test for household-mcp. Standard library only.

  python smoke_test.py --url https://raspberrypi.tailXXXX.ts.net
      unauthenticated checks: /health, OAuth discovery, 401 on /mcp
  python smoke_test.py --url http://localhost:8000 --token <DEV_BYPASS>
      plus the authenticated tool round-trip and the 403 allow-list check (dev only)
  python smoke_test.py --url https://... --token <FastMCP access token>
      authenticated round-trip through the real OAuth path
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

FAILS = 0


def check(name, ok, detail=""):
    global FAILS
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILS += 1


def http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


class Mcp:
    def __init__(self, url, token):
        self.url, self.token, self.n = url.rstrip("/") + "/mcp", token, 0

    def rpc(self, method, params=None, notify=False):
        self.n += 1
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            msg["id"] = self.n
        hdr = {"content-type": "application/json", "accept": "application/json, text/event-stream",
               "authorization": f"Bearer {self.token}", "MCP-Protocol-Version": "2025-06-18"}
        status, _, body = http("POST", self.url, msg, hdr)
        if notify:
            return status, None
        text = body.decode(errors="replace")
        if text.startswith("event:") or "\ndata:" in text or text.startswith("data:"):
            for line in text.splitlines():
                if line.startswith("data:"):
                    text = line[5:].strip()
        return status, (json.loads(text) if text else None)

    def call(self, tool, args):
        status, resp = self.rpc("tools/call", {"name": tool, "arguments": args})
        res = (resp or {}).get("result") or {}
        if res.get("isError"):
            return status, None, " ".join(c.get("text", "") for c in res.get("content", []))
        sc = res.get("structuredContent", res)
        if isinstance(sc, dict) and set(sc) == {"result"}:
            sc = sc["result"]
        return status, sc, None


def unauthenticated(base):
    s, _, b = http("GET", base + "/health")
    check("GET /health", s == 200 and b"household-mcp" in b, f"status {s}")
    s, _, b = http("GET", base + "/.well-known/oauth-protected-resource/mcp")
    if s != 200:
        s, _, b = http("GET", base + "/.well-known/oauth-protected-resource")
    if s == 404 and base.startswith("http://localhost") or base.startswith("http://127.0.0.1"):
        print("SKIP OAuth protected-resource metadata (dev bypass has no OAuth routes)")
    else:
        ok = s == 200 and base.rstrip("/") in b.decode(errors="replace")
        check("OAuth protected-resource metadata", ok, f"status {s}")
    s, h, _ = http("POST", base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                   {"content-type": "application/json", "accept": "application/json, text/event-stream"})
    check("POST /mcp without token is 401", s == 401, f"status {s}, WWW-Authenticate={h.get('www-authenticate')}")


def authenticated(base, token, other_token=None):
    m = Mcp(base, token)
    s, r = m.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "smoke_test", "version": "1"}})
    check("initialize", s == 200 and "result" in (r or {}), f"status {s}")
    s, r = m.rpc("tools/list")
    names = sorted(t["name"] for t in (r or {}).get("result", {}).get("tools", []))
    want = ["add_item", "get_document", "get_list", "inbox", "leave_note", "list_topics", "recall", "remember", "remove_item"]
    check("tools/list has the nine phase-1 + phase-2 tools", names == want, ", ".join(names))
    sch = {t["name"]: t["inputSchema"] for t in (r or {}).get("result", {}).get("tools", [])}
    check("leave_note takes `for` and `text`", set(sch.get("leave_note", {}).get("properties", {})) == {"for", "text"})

    s, out, err = m.call("remember", {"topic": "Smoke Test", "fact": "smoke test fact  with   spaces"})
    check("remember stores a stamped line", not err and out and out["line"].startswith("- [") and "smoke test fact with spaces" in out["line"], err or out.get("line", ""))
    s, out, err = m.call("recall", {"query": "smoke spaces"})
    check("recall finds it", not err and out and out["count"] >= 1 and out["lines"][0]["topic"] == "smoke-test", err or f"{out and out['count']} hits")
    s, out, err = m.call("recall", {"query": "smoke", "topic": "no-such-topic"})
    check("recall with unknown topic returns nothing", not err and out and out["count"] == 0, err or "")
    s, out, err = m.call("list_topics", {})
    check("list_topics includes smoke-test", not err and any(t["topic"] == "smoke-test" and t["lines"] >= 1 for t in out), err or "")
    s, out, err = m.call("remember", {"topic": "smoke-test", "fact": "x" * 2001})
    check("remember rejects a 2001-char fact", bool(err) and "2000" in err, err or "accepted")
    s, out, err = m.call("remember", {"topic": "../etc", "fact": "nope"})
    check("remember rejects a bad topic", bool(err), err or "accepted")
    s, out, err = m.call("get_document", {"name": "../.env"})
    check("get_document rejects path traversal", bool(err), err or "accepted")
    s, out, err = m.call("get_document", {"name": "definitely-missing.pdf"})
    check("get_document unknown name errors with a list", bool(err) and "available" in err, err or "")
    s, out, err = m.call("leave_note", {"for": "chatgpt", "text": "smoke note for chatgpt"})
    check("leave_note for chatgpt", not err and out and out.get("id"), err or "")
    s, out, err = m.call("leave_note", {"for": "claude-code", "text": "smoke note for claude-code"})
    check("leave_note for claude-code", not err and out and out.get("for") == "claude-code", err or "")
    s, out, err = m.call("inbox", {"peek": True, "assistant": "claude-code"})
    check("claude reads the claude-code inbox", not err and out and out["assistant"] == "claude-code" and out["count"] >= 1, err or str(out))
    s, out, err = m.call("inbox", {"assistant": "claude-code"})
    check("claude-code inbox read and cleared", not err and out and out["count"] >= 1, err or str(out))
    s, out, err = m.call("leave_note", {"for": "gemini", "text": "nope"})
    check("leave_note rejects unknown assistant", bool(err), err or "accepted")
    s, out, err = m.call("inbox", {"peek": True})
    check("inbox peek works", not err and out and "notes" in out, err or f"as {out and out['assistant']}: {out and out['count']} notes")

    # phase 2: lists
    s, out, err = m.call("remove_item", {"list": "shopping", "item": "Smoke Cola"})
    s, out, err = m.call("add_item", {"list": "shopping", "item": "  Smoke   Cola ", "qty": 2})
    row = next((i for i in (out or {}).get("items", []) if i["item"] == "Smoke Cola"), None)
    check("add_item normalises and stores qty", not err and row and row["qty"] == 2 and "/" in row["added_by"], err or str(out))
    s, out, err = m.call("add_item", {"list": "shopping", "item": "smoke cola", "qty": 11})
    row = next((i for i in (out or {}).get("items", []) if i["item"] == "Smoke Cola"), None)
    n = sum(1 for i in (out or {}).get("items", []) if i["item"].lower() == "smoke cola")
    check("add_item merges case-insensitively and caps at 12", not err and row and row["qty"] == 12 and n == 1, err or f"qty={row and row['qty']} rows={n}")
    s, out, err = m.call("add_item", {"list": "shopping", "item": "Smoke Water", "qty": 99})
    row = next((i for i in (out or {}).get("items", []) if i["item"] == "Smoke Water"), None)
    check("add_item caps a new qty of 99 at 12", not err and row and row["qty"] == 12 and "capped" in (out.get("note") or ""), err or str(out))
    s, out, err = m.call("add_item", {"list": "shopping", "item": "x", "qty": 0})
    check("add_item rejects qty 0", bool(err), err or "accepted")
    s, out, err = m.call("add_item", {"list": "chores", "item": "x"})
    check("add_item rejects an unknown list", bool(err) and "shopping" in err, err or "accepted")
    s, out, err = m.call("get_list", {"list": "shopping"})
    check("get_list returns the canonical list", not err and out and out["count"] >= 2, err or "")
    s, out, err = m.call("remove_item", {"list": "shopping", "item": "SMOKE COLA"})
    check("remove_item matches case-insensitively", not err and out and not any(i["item"] == "Smoke Cola" for i in out["items"]), err or "")
    s, out, err = m.call("remove_item", {"list": "shopping", "item": "Smoke Cola"})
    check("remove_item of a missing item returns the list with a note", not err and out and "not on the list" in (out.get("note") or ""), err or str(out))
    s, out, err = m.call("remove_item", {"list": "shopping", "item": "Smoke Water"})

    if other_token:
        o = Mcp(base, other_token)
        s, out, err = o.call("inbox", {"peek": True, "assistant": "claude-code"})
        check("chatgpt may not read a claude inbox", bool(err) and "not yours" in err, err or "allowed")
        s, out, err = o.call("inbox", {"peek": True})
        check("other assistant peeks its inbox", not err and out and out["count"] >= 1, err or f"{out}")
        s, out, err = o.call("inbox", {})
        check("other assistant reads and clears", not err and out and out["count"] >= 1, err or "")
        s, out, err = o.call("inbox", {})
        check("inbox is empty after reading", not err and out and out["count"] == 0, err or "")


def forbidden(base, bad_token):
    m = Mcp(base, bad_token)
    s, r = m.rpc("tools/list")
    check("allow-list: unknown email gets 403", s == 403, f"status {s}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--token", help="bearer token for the authenticated checks")
    ap.add_argument("--dev-emails", help="comma-separated ALLOWED_EMAILS, dev mode only: runs the chatgpt side and the 403 check")
    a = ap.parse_args()
    base = a.url.rstrip("/")
    unauthenticated(base)
    if a.token:
        other = bad = None
        if a.dev_emails:
            emails = [e.strip() for e in a.dev_emails.split(",")]
            other = f"{a.token}@{emails[1]}" if len(emails) > 1 else None
            bad = f"{a.token}@stranger@example.org"
        authenticated(base, a.token, other)
        if bad:
            forbidden(base, bad)
    print(f"\n{'ALL PASSED' if not FAILS else str(FAILS) + ' FAILED'}")
    sys.exit(1 if FAILS else 0)
