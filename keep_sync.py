#!/usr/bin/env python3
"""Mirror the canonical shopping list to Google Keep and ingest House notes.

Runs from household-keep.timer every 5 minutes. Keep is a mirror, never the source
of truth: if it breaks you lose speaker sync for a while, never the list.

Per run:
  1. pull the Keep list; any unchecked item not on the canonical list was added at a
     speaker, so it is added with the stamp "speaker"
  2. any canonical item that was unchecked in Keep last run and is checked now was bought,
     so it is removed from the canonical list
  3. push the canonical list back so Keep's UNCHECKED items are exactly what the Pi has.
     Checked-off items in Keep are history: they are never deleted, renamed or unchecked
  4. log every difference; if step 1 would add, or step 3 would delete, more than HOLD_LIMIT
     items the run is held for review (lists/shopping.review.json, a note for Claude) and
     nothing changes on either side
  5. unchecked entries in the House notes list become notes in Claude's inbox, then are
     checked off in Keep
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from auth import load_env
from store import Store, StoreError, keep_text, normalize_item, parse_keep_text

HOLD_LIMIT = 20
LIST_NAME = "shopping"
SPEAKER = "speaker"
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
log = logging.getLogger("keep_sync")


# ---------- pure reconciliation, testable without Keep ----------

def reconcile(canonical: list[dict], keep_items: list[dict], last_unchecked: set[str] | None = None) -> dict:
    """canonical: rows from lists/shopping.json. keep_items: [{"id","text","checked"}].
    last_unchecked: keys that were unchecked in Keep at the end of the previous run; only
    those count as "checked off since", so old checked history never vetoes a new Pi add.
    Returns the changes to make on both sides. Checked Keep items are never touched."""
    last_unchecked = set(last_unchecked or ())
    canon_by_key = {r["key"]: r for r in canonical}
    unchecked_by_key: dict[str, dict] = {}
    checked_keys: set[str] = set()
    for it in keep_items:
        name, _qty = parse_keep_text(it["text"])
        try:
            _d, key = normalize_item(name)
        except StoreError:
            continue
        it["key"] = key
        if it["checked"]:
            checked_keys.add(key)
        elif key not in unchecked_by_key:
            unchecked_by_key[key] = it

    adds = [it for key, it in unchecked_by_key.items() if key not in canon_by_key]
    removes = [canon_by_key[k] for k in sorted(checked_keys)
               if k in canon_by_key and k not in unchecked_by_key and k in last_unchecked]

    desired = {r["key"]: r for r in canonical if r["key"] not in {x["key"] for x in removes}}
    for it in adds:
        name, qty = parse_keep_text(it["text"])
        display, key = normalize_item(name)
        desired[key] = {"item": display, "key": key, "qty": min(qty, 12), "added_by": SPEAKER}

    keep_delete, keep_retext = [], []
    seen: set[str] = set()
    for it in keep_items:
        if it["checked"]:
            continue  # history stays exactly as it is
        key = it.get("key")
        if key is None or key not in desired or key in seen:
            keep_delete.append(it)
            continue
        seen.add(key)
        want = keep_text(desired[key])
        if it["text"].strip() != want:
            keep_retext.append((it, want))
    keep_add = [desired[k] for k in desired if k not in seen]
    return {"adds": adds, "removes": removes, "keep_delete": keep_delete,
            "keep_retext": keep_retext, "keep_add": keep_add,
            "unchecked_keys": sorted(desired)}


# ---------- Keep access ----------

def connect(username: str, token: str):
    import gkeepapi

    keep = gkeepapi.Keep()
    state_path = DATA / "keep_state.json"
    state = None
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            state = None
    keep.authenticate(username, token, state=state, sync=True)
    return keep


def save_state(keep) -> None:
    DATA.mkdir(exist_ok=True)
    p = DATA / "keep_state.json"
    p.write_text(json.dumps(keep.dump()))
    os.chmod(p, 0o600)


def find_list(keep, title: str):
    import gkeepapi

    for node in keep.find(func=lambda n: n.type == gkeepapi.node.NodeType.List and n.title == title):
        return node
    return None


def sync_shopping(store: Store, glist) -> dict:
    with store.list_lock(LIST_NAME):
        data = store.read_list(LIST_NAME)
        items = [{"id": it.id, "text": it.text, "checked": it.checked, "node": it} for it in glist.items]
        plan = reconcile(data["items"], items, set(data["keep"].get("unchecked_keys") or ()))
        review = store.lists / f"{LIST_NAME}.review.json"

        too_many = {}
        if len(plan["adds"]) > HOLD_LIMIT:
            too_many["pending_adds"] = [it["text"] for it in plan["adds"]]
        if len(plan["keep_delete"]) > HOLD_LIMIT:
            too_many["pending_keep_deletes"] = [it["text"] for it in plan["keep_delete"]]
        if too_many:
            summary = ", ".join(f"{len(v)} {k.replace('pending_', '').replace('_', ' ')}" for k, v in too_many.items())
            log.warning("HELD: this run would make %s (limit %d per run); nothing changed on either side. %s",
                        summary, HOLD_LIMIT, json.dumps(too_many))
            first_time = not review.exists()
            review.write_text(json.dumps({"held_at": _now(), "reason": f"more than {HOLD_LIMIT} changes of one kind: {summary}",
                                          **too_many}, indent=1) + "\n")
            store.log_write("keep_sync", SPEAKER, "keep", held=summary)
            if first_time:
                store.leave_note("claude-code", f"Keep sync held: the Grocery List mirror would make {summary} in one run "
                                 f"(limit {HOLD_LIMIT}). See lists/shopping.review.json. Fix up the Keep list or the "
                                 f"canonical list; the mirror resumes on its own.", SPEAKER, "keep")
            return {"held": summary}
        if review.exists():
            review.unlink()
            log.info("hold cleared")

        changed = False
        for it in plan["adds"]:
            name, qty = parse_keep_text(it["text"])
            display, key = normalize_item(name)
            data["items"].append({"item": display, "key": key, "qty": min(qty, 12), "added_by": SPEAKER, "added": _today()})
            store.log_write("add_item", SPEAKER, "keep", list=LIST_NAME, item=display, qty=min(qty, 12))
            log.info("speaker added: %s", it["text"])
            changed = True
        for row in plan["removes"]:
            data["items"] = [r for r in data["items"] if r["key"] != row["key"]]
            store.log_write("remove_item", SPEAKER, "keep", list=LIST_NAME, item=row["item"], via="checked in Keep")
            log.info("checked off in Keep, removed: %s", row["item"])
            changed = True
        for it in plan["keep_delete"]:
            log.info("keep: delete %r%s", it["text"], " (checked)" if it["checked"] else "")
            it["node"].delete()
        for it, want in plan["keep_retext"]:
            log.info("keep: rename %r -> %r", it["text"], want)
            it["node"].text = want
        for row in plan["keep_add"]:
            log.info("keep: add %r", keep_text(row))
            glist.add(keep_text(row))
        unchecked_keys = plan["unchecked_keys"]
        if changed or data["keep"].get("list_id") != glist.id or data["keep"].get("unchecked_keys") != unchecked_keys:
            data["keep"] = {"list_id": glist.id, "unchecked_keys": unchecked_keys,
                            "last_change": _now() if changed else data["keep"].get("last_change")}
            store.write_list(data)
    return {k: len(v) for k, v in plan.items() if k != "unchecked_keys"}


def ingest_notes(store: Store, nlist) -> int:
    n = 0
    for it in list(nlist.unchecked):
        text = (it.text or "").strip()
        if not text:
            continue
        try:
            note = store.leave_note("claude", text, SPEAKER, "keep")
        except StoreError as e:
            log.warning("skipped House notes entry: %s", e)
            continue
        it.checked = True
        log.info("note for claude %s: %s", note["id"], text[:80])
        n += 1
    return n


def _now() -> str:
    from store import _now_iso
    return _now_iso()


def _today() -> str:
    from store import _today as t
    return t()


def main() -> int:
    load_env(os.environ.get("ENV_FILE", "/home/pi/household/.env"))
    DATA.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(DATA / "keep_sync.log")])
    username = os.environ.get("KEEP_USERNAME", "")
    token = os.environ.get("KEEP_MASTER_TOKEN", "")
    shopping_title = os.environ.get("KEEP_SHOPPING_LIST", "Grocery List")
    notes_title = os.environ.get("KEEP_NOTES_LIST", "House notes")
    if not (username and token):
        log.info("Keep mirror not configured (KEEP_USERNAME / KEEP_MASTER_TOKEN empty); nothing to do")
        return 0
    store = Store(os.environ.get("STORE_DIR", "/home/pi/household"))
    try:
        keep = connect(username, token)
    except Exception as e:  # gkeepapi.exception.LoginException and friends
        log.error("Keep login failed: %s", type(e).__name__)
        return 1
    glist = find_list(keep, shopping_title)
    if glist is None:
        log.error("no Keep list titled %r; create it in Keep", shopping_title)
        save_state(keep)
        return 1
    result = sync_shopping(store, glist)
    nlist = find_list(keep, notes_title)
    notes = ingest_notes(store, nlist) if nlist is not None else 0
    if nlist is None:
        log.warning("no Keep list titled %r; dictation inbox skipped", notes_title)
    keep.sync()
    save_state(keep)
    log.info("done: %s, notes ingested: %d", result, notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
