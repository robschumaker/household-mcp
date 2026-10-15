"""Household MCP server, phase 1: memory and coordination tools over Streamable HTTP."""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
import time
from typing import Annotated, Literal
from urllib.parse import quote, unquote

from pydantic import Field
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from auth import AllowListMiddleware, Config, build_auth, current_caller, load_env
from store import ASSISTANTS, Store, StoreError

load_env(os.environ.get("ENV_FILE", "/home/pi/household/.env"))
cfg = Config()
cfg.validate()
store = Store(cfg.store_dir)
auth = build_auth(cfg)

mcp = FastMCP(
    name="Household",
    instructions=(
        "Shared household memory for the owner's Claude and the partner's ChatGPT. Start with inbox and recall; "
        "record decisions and new facts with remember; use leave_note to tell the other assistant "
        "about changes. Topics are short hyphenated names such as europe-trip, household, people."
    ),
    auth=auth,
)

LINK_SECRET = hashlib.sha256((cfg.client_secret or cfg.dev_bypass or "household").encode()).digest()
LINK_TTL = 24 * 3600


def _sign(name: str, exp: int) -> str:
    return hmac.new(LINK_SECRET, f"{name}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def _download_link(name: str) -> str:
    exp = int(time.time()) + LINK_TTL
    return f"{cfg.base_url}/documents/{quote(name)}?exp={exp}&sig={_sign(name, exp)}"


async def _caller():
    try:
        return await current_caller(cfg, auth)
    except PermissionError as e:
        raise ToolError(str(e)) from e


def _wrap(fn, *args):
    try:
        return fn(*args)
    except StoreError as e:
        raise ToolError(str(e)) from e


@mcp.tool
async def recall(query: str, topic: str | None = None) -> dict:
    """Search the shared household memory. Full-text match over every stored fact,
    newest first, up to 20 lines, each with its topic and attribution stamp.
    Optional topic narrows to one topic (see list_topics)."""
    await _caller()
    return _wrap(store.recall, query, topic)


@mcp.tool
async def remember(topic: str, fact: str) -> dict:
    """Store one fact or decision in the shared memory under a topic (created if new).
    Returns the exact line as written, including its [date user/assistant] stamp.
    Fact is a single line of at most 2000 characters."""
    c = await _caller()
    return _wrap(store.remember, topic, fact, c.user, c.assistant)


@mcp.tool
async def list_topics() -> list[dict]:
    """List memory topics with their line counts and last-updated dates. Cheap orientation call."""
    await _caller()
    return store.list_topics()


@mcp.tool
async def get_document(name: str) -> dict:
    """Fetch a document from the household documents folder by file name.
    Text files (md, txt, csv, json) come back as text; PDFs and other binaries
    come back with a download link valid for 24 hours. An unknown name lists what exists."""
    await _caller()
    info = _wrap(store.get_document, name)
    if "text" not in info:
        info["download_url"] = _download_link(info["name"])
        info["note"] = "binary document; the link above downloads it for 24 hours"
    return info


@mcp.tool
async def leave_note(
    for_: Annotated[Literal["claude", "chatgpt"], Field(alias="for", description="which assistant the note is for")],
    text: Annotated[str, Field(description="the note, at most 2000 characters")],
) -> dict:
    """Leave a note for the other assistant (for = claude or chatgpt). It lands in that
    assistant's inbox. Returns the stored note with its id and attribution."""
    c = await _caller()
    return _wrap(store.leave_note, for_, text, c.user, c.assistant)


@mcp.tool
async def inbox(peek: bool = False) -> dict:
    """Read unread notes left for the calling assistant, oldest first. Notes are cleared
    once read; pass peek=true to look without clearing."""
    c = await _caller()
    if c.assistant not in ASSISTANTS:
        raise ToolError("could not tell which assistant is calling, so there is no inbox to read")
    return _wrap(store.inbox, c.assistant, peek, c.user)


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(_: Request):
    return JSONResponse({"ok": True, "name": "household-mcp", "phase": 1})


@mcp.custom_route("/documents/{name:path}", methods=["GET"], include_in_schema=False)
async def download(request: Request):
    name = unquote(request.path_params["name"])
    try:
        exp = int(request.query_params.get("exp", "0"))
    except ValueError:
        exp = 0
    sig = request.query_params.get("sig", "")
    if exp < time.time() or not hmac.compare_digest(sig, _sign(name, exp)):
        return PlainTextResponse("link expired or invalid", status_code=403)
    try:
        path = store.resolve_document(name)
    except StoreError:
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(path, filename=path.name)


if __name__ == "__main__":
    print(f"household-mcp: store={cfg.store_dir} bind={cfg.bind_host}:{cfg.bind_port} base_url={cfg.base_url}", file=sys.stderr)
    mcp.run(
        transport="http",
        host=cfg.bind_host,
        port=cfg.bind_port,
        path="/mcp",
        stateless_http=True,
        json_response=True,
        show_banner=False,
        middleware=[Middleware(AllowListMiddleware, cfg=cfg)],
    )
