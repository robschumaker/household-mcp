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
        "Shared household memory and lists for the owner's Claude, the partner's ChatGPT and Claude Code on the owner's laptop. "
        "Start with inbox (pass topic when the chat is about one thing, e.g. europe-december-2026-trip) and recall; "
        "record decisions and new facts with remember; use leave_note, with a topic tag, to tell another assistant "
        "about changes (for=claude: the owner's claude.ai Claude, trips and household plans; for=claude-code: Claude Code, "
        "the Pi and laptop work; for=chatgpt: the partner's assistant). inbox never clears notes: when a note's task is done, "
        "call finish_note with its id. Topics are short hyphenated names such as europe-december-2026-trip, household, people; "
        "merge_topics and delete_topic tidy them. "
        "The shopping list: add_item, remove_item, get_list; always report the full list a write returns."
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
    for_: Annotated[Literal["claude", "chatgpt", "claude-code"], Field(
        alias="for",
        description="which assistant the note is for: claude (the owner's claude.ai Claude: trips, itinerary, "
                    "household plans), chatgpt (the partner's assistant), claude-code (Claude Code on the owner's laptop: "
                    "the Pi, this server, laptop work)")],
    text: Annotated[str, Field(description="the note, at most 2000 characters")],
    topic: Annotated[str | None, Field(
        description="what the note is about, as a memory topic name such as europe-december-2026-trip or "
                    "household, so the reader can filter its inbox; omit for anything general")] = None,
) -> dict:
    """Leave a note for another assistant (for = claude, chatgpt or claude-code), tagged with a
    topic when it concerns one. It stays in that assistant's inbox until they finish_note it."""
    c = await _caller()
    return _wrap(store.leave_note, for_, text, c.user, c.assistant, topic)


@mcp.tool
async def inbox(
    topic: Annotated[str | None, Field(
        description="only notes about this topic, plus untagged ones; omit to see every open note")] = None,
    assistant: Annotated[Literal["claude", "chatgpt", "claude-code"] | None, Field(
        description="which inbox to read; defaults to the calling assistant's. Claude Code must pass "
                    "claude-code, because it reaches this server through the same connector as claude.ai")] = None,
    peek: Annotated[bool, Field(description="ignored; reading never clears notes any more")] = True,
) -> dict:
    """Open notes left for the calling assistant, oldest first, each with an id. Reading never
    removes a note: call finish_note(id) once its task is done. Claude Code passes
    assistant=claude-code; notes addressed to claude belong to the owner's claude.ai Claude."""
    c = await _caller()
    which = assistant or c.assistant
    if which not in ASSISTANTS:
        raise ToolError("could not tell which assistant is calling; pass assistant=claude, chatgpt or claude-code")
    if not c.may_read_inbox(which):
        raise ToolError(f"the {which} inbox is not yours to read (you are {c.assistant})")
    return _wrap(store.inbox, which, True, c.user, topic)


@mcp.tool
async def finish_note(
    id: Annotated[str, Field(description="the note id as inbox showed it")],
    assistant: Annotated[Literal["claude", "chatgpt", "claude-code"] | None, Field(
        description="whose inbox the note is in; defaults to the calling assistant's (Claude Code passes claude-code)")] = None,
) -> dict:
    """Remove one note from your inbox because its task is done. The note is kept in the read log."""
    c = await _caller()
    which = assistant or c.assistant
    if which not in ASSISTANTS:
        raise ToolError("could not tell which assistant is calling; pass assistant=claude, chatgpt or claude-code")
    if not c.may_read_inbox(which):
        raise ToolError(f"the {which} inbox is not yours to finish notes in (you are {c.assistant})")
    return _wrap(store.finish_note, which, id, c.user, c.assistant)


@mcp.tool
async def merge_topics(
    sources: Annotated[list[str], Field(description="topics to fold into the target; they are retired afterwards")],
    into: Annotated[str, Field(description="the topic that keeps everything (created if new)")],
) -> dict:
    """Merge memory topics: every line of each source is appended to the target, exact duplicate
    facts skipped, and the sources are moved out of memory into log/merged-topics. Nothing is erased."""
    c = await _caller()
    return _wrap(store.merge_topics, sources, into, c.user, c.assistant)


@mcp.tool
async def delete_topic(
    topic: Annotated[str, Field(description="the topic to retire")],
) -> dict:
    """Retire a whole memory topic. Its file moves to log/deleted-topics, so it can be restored by hand."""
    c = await _caller()
    return _wrap(store.delete_topic, topic, c.user, c.assistant)


@mcp.tool
async def add_item(list: str, item: str, qty: int = 1) -> dict:
    """Add an item to a household list (lists: shopping). qty defaults to 1 and is capped
    at 12; an item already on the list is merged and its qty bumped rather than duplicated.
    Returns the full list after the write; report that list back."""
    c = await _caller()
    return _wrap(store.add_item, list, item, qty, f"{c.user}/{c.assistant}")


@mcp.tool
async def remove_item(list: str, item: str) -> dict:
    """Remove an item from a household list, matched case-insensitively. No match returns the
    list unchanged with a note. Returns the full list after the write."""
    c = await _caller()
    return _wrap(store.remove_item, list, item, f"{c.user}/{c.assistant}")


@mcp.tool
async def get_list(list: str) -> dict:
    """Read a household list (lists: shopping) with quantities and who added what.
    This is the canonical copy on the Pi, not the Google Keep mirror."""
    await _caller()
    return _wrap(store.get_list, list)


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(_: Request):
    return JSONResponse({"ok": True, "name": "household-mcp", "phase": 2})


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
