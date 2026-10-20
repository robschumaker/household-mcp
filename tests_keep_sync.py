"""Unit test for keep_sync with a fake Keep list. Run: venv/bin/python tests_keep_sync.py"""
import sys, tempfile
from pathlib import Path
import keep_sync
from store import Store


class FakeItem:
    def __init__(self, text, checked=False):
        self.id, self.text, self.checked, self.deleted = f"id-{text}-{id(self)}", text, checked, False
    def delete(self): self.deleted = True


class FakeList:
    def __init__(self, items):
        self.id, self._items = "list-1", [FakeItem(t, c) for t, c in items]
    @property
    def items(self): return [i for i in self._items if not i.deleted]
    @property
    def unchecked(self): return [i for i in self.items if not i.checked]
    def add(self, text, checked=False):
        it = FakeItem(text, checked); self._items.append(it); return it
    def state(self): return sorted((i.text, i.checked) for i in self.items)
    def n_checked(self): return sum(1 for i in self.items if i.checked)


fails = 0
def check(name, ok, detail=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    fails += not ok


def fresh_store():
    tmp = Path(tempfile.mkdtemp())
    keep_sync.DATA = tmp / "data"; keep_sync.DATA.mkdir()
    s = Store(tmp / "store")
    s.add_item("shopping", "Milk", 2, "owner/claude")
    s.add_item("shopping", "Eggs", 1, "partner/chatgpt")
    s.add_item("shopping", "Bread", 1, "owner/claude")
    d = s.read_list("shopping"); d["keep"]["unchecked_keys"] = ["milk", "eggs", "bread"]; s.write_list(d)
    return s

def review_exists(s): return (s.lists / "shopping.review.json").exists()
def items(s): return {i["item"] for i in s.get_list("shopping")["items"]}

NOOP = {"adds": 0, "removes": 0, "keep_check": 0, "keep_delete": 0, "keep_retext": 0, "keep_add": 0}

# --- basic reconciliation -------------------------------------------------------------
store = fresh_store()
# Keep: Milk plain (needs rename to 'Milk x2'), Eggs checked off at a speaker, 'bananas' added at a
# speaker, a stray duplicate 'milk', and an old checked 'Old thing'.
gl = FakeList([("Milk", False), ("Eggs", True), ("bananas", False), ("milk", False), ("Old thing", True)])
res = keep_sync.sync_shopping(store, gl)
lst = {i["item"]: i for i in store.get_list("shopping")["items"]}
check("speaker add lands with stamp speaker", "bananas" in lst and lst["bananas"]["added_by"] == "speaker")
check("Eggs (unchecked last run, checked now) removed from canonical", "Eggs" not in lst)
check("Bread untouched", "Bread" in lst)
check("Keep unchecked items mirror the Pi; checked items untouched",
      gl.state() == [("Bread", False), ("Eggs", True), ("Milk x2", False), ("Old thing", True), ("bananas", False)], str(gl.state()))
check("review file absent", not review_exists(store))
check("second run is a no-op", keep_sync.sync_shopping(store, gl) == NOOP)
check("unchecked keys recorded", store.read_list("shopping")["keep"]["unchecked_keys"] == ["bananas", "bread", "milk"])

# --- hundreds of checked-off items (years of history) must survive untouched ----------
store = fresh_store()
hist = FakeList([(f"bought thing {i}", True) for i in range(600)] + [("Bread", False), ("Milk x2", False), ("Eggs", False)])
r = keep_sync.sync_shopping(store, hist)
check("600 checked items: not held, nothing deleted, nothing changed", r == NOOP, str(r))
check("600 checked items all still present and checked", hist.n_checked() == 600 and len(hist.items) == 603)
check("canonical unchanged by history", items(store) == {"Milk", "Eggs", "Bread"})

# old checked history must not veto a fresh add on the Pi
store.add_item("shopping", "Bought thing 7", 1, "owner/claude")   # same key as a checked history item
r = keep_sync.sync_shopping(store, hist)
check("fresh Pi add with a checked twin in history is pushed as a new unchecked item",
      r["removes"] == 0 and r["keep_add"] == 1 and ("Bought thing 7", False) in hist.state() and ("bought thing 7", True) in hist.state(), str(r))
check("it stays on the canonical list", "Bought thing 7" in items(store))
next(i for i in hist.items if i.text == "Bought thing 7").checked = True   # bought it
r = keep_sync.sync_shopping(store, hist)
check("checking off the new item removes it from the Pi", r["removes"] == 1 and "Bought thing 7" not in items(store), str(r))
check("now 601 checked items, none ever deleted", hist.n_checked() == 601 and len(hist.items) == 604)

# an item checked in Keep that was NOT unchecked last run is ignored (history), not a removal
store = fresh_store()
d = store.read_list("shopping"); d["keep"]["unchecked_keys"] = []; store.write_list(d)
r = keep_sync.sync_shopping(store, FakeList([("Milk", True), ("Eggs", True), ("Bread", True)]))
check("checked items with no unchecked history do not remove Pi items", r["removes"] == 0 and items(store) == {"Milk", "Eggs", "Bread"}, str(r))

# --- remove_item on the Pi checks the item off in Keep -------------------------------
store = fresh_store()
gl = FakeList([("Milk x2", False), ("Eggs", False), ("Bread", False), ("Old thing", True)])
check("in sync to start", keep_sync.sync_shopping(store, gl) == NOOP)
store.remove_item("shopping", "bread", "owner/claude")
r = keep_sync.sync_shopping(store, gl)
check("Pi removal is checked off in Keep, not re-added", r["keep_check"] == 1 and r["adds"] == 0 and r["keep_delete"] == 0, str(r))
check("Bread is checked in Keep and stays off the Pi", ("Bread", True) in gl.state() and "Bread" not in items(store), str(gl.state()))
check("next run is a no-op", keep_sync.sync_shopping(store, gl) == NOOP)
check("Bread not resurrected", "Bread" not in items(store))
# a duplicate unchecked twin of a removed item is still deleted as a stray
store = fresh_store()
gl = FakeList([("Milk x2", False), ("Eggs", False), ("Bread", False), ("bread", False)])
store.remove_item("shopping", "Bread", "owner/claude")
r = keep_sync.sync_shopping(store, gl)
check("removed item: first twin checked, stray twin deleted", r["keep_check"] == 1 and r["keep_delete"] == 1 and gl.state() == [("Bread", True), ("Eggs", False), ("Milk x2", False)], str(gl.state()))
# speaker re-adds the same item later: it is a fresh add, not a removal echo
r = keep_sync.sync_shopping(store, gl)
gl.add("Bread")
r = keep_sync.sync_shopping(store, gl)
check("speaker re-add after a Pi removal lands as a new item", r["adds"] == 1 and "Bread" in items(store), str(r))

# --- addition hold ------------------------------------------------------------------
store = fresh_store()
flood = FakeList([(f"diet coke {i}", False) for i in range(21)] + [("Bread", False), ("Milk x2", False), ("Eggs", False)])
r = keep_sync.sync_shopping(store, flood)
check("21 speaker adds are held", "held" in r and "21 adds" in r["held"], str(r))
check("canonical unchanged by the hold", items(store) == {"Milk", "Eggs", "Bread"})
check("review file written", review_exists(store))
check("nothing pushed or deleted in Keep", len(flood.items) == 24)
check("one hold note for claude-code", store.inbox("claude-code", peek=True, user="t")["count"] == 1)
keep_sync.sync_shopping(store, flood)
check("hold does not re-notify", store.inbox("claude-code", peek=True, user="t")["count"] == 1)
twenty = FakeList([(f"item {i}", False) for i in range(20)] + [("Bread", False), ("Milk x2", False), ("Eggs", False)])
r = keep_sync.sync_shopping(store, twenty)
check("exactly 20 adds go through and clear the hold", r.get("adds") == 20 and not review_exists(store), str(r))

# --- deletion hold ------------------------------------------------------------------
store = fresh_store()
# 22 unchecked copies of Bread: 21 duplicates would be deleted -> held
dupes = FakeList([("Bread", False)] * 22 + [("Milk x2", False), ("Eggs", False)] + [(f"old {i}", True) for i in range(300)])
r = keep_sync.sync_shopping(store, dupes)
check("21 Keep deletions are held", "held" in r and "21 keep deletes" in r["held"], str(r))
check("nothing deleted in Keep during the hold", len(dupes.items) == 324 and dupes.n_checked() == 300)
check("review file names the pending deletes", "pending_keep_deletes" in (store.lists / "shopping.review.json").read_text())
twenty_one = FakeList([("Bread", False)] * 21 + [("Milk x2", False), ("Eggs", False)] + [(f"old {i}", True) for i in range(300)])
r = keep_sync.sync_shopping(store, twenty_one)
check("exactly 20 deletions go through", r.get("keep_delete") == 20 and len(twenty_one.items) == 303 and twenty_one.n_checked() == 300, str(r))
check("deletion hold cleared", not review_exists(store))

# --- House notes --------------------------------------------------------------------
store = fresh_store()
nl = FakeList([("buy stamps", False), ("already done", True), ("   ", False)])
n = keep_sync.ingest_notes(store, nl)
inbox = store.inbox("claude-code", peek=True, user="t")
check("House notes ingested into claude-code inbox and checked off",
      n == 1 and inbox["count"] == 1 and inbox["notes"][0]["from"] == "speaker/keep"
      and nl.state() == [("   ", False), ("already done", True), ("buy stamps", True)], str(nl.state()))

print("\n" + ("ALL PASSED" if not fails else f"{fails} FAILED")); sys.exit(1 if fails else 0)
