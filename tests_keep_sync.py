"""Unit test for keep_sync with a fake Keep list. Run: venv/bin/python tests_keep_sync.py"""
import json, sys, tempfile
from pathlib import Path
import keep_sync
from store import Store


class FakeItem:
    def __init__(self, text, checked=False):
        self.id, self.text, self.checked, self.deleted = f"id-{text}", text, checked, False
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


fails = 0
def check(name, ok, detail=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    fails += not ok


tmp = Path(tempfile.mkdtemp())
keep_sync.DATA = tmp / "data"; keep_sync.DATA.mkdir()
store = Store(tmp / "store")
store.add_item("shopping", "Milk", 2, "owner/claude")
store.add_item("shopping", "Eggs", 1, "partner/chatgpt")
store.add_item("shopping", "Bread", 1, "owner/claude")

# Keep: Milk still shown as plain 'Milk' (needs rename to 'Milk x2'), Eggs checked off at a speaker,
# 'bananas' added at a speaker, a stray duplicate 'milk', and a checked leftover 'Old thing'.
gl = FakeList([("Milk", False), ("Eggs", True), ("bananas", False), ("milk", False), ("Old thing", True)])
res = keep_sync.sync_shopping(store, gl)
lst = store.get_list("shopping")
names = {i["item"]: i for i in lst["items"]}
check("speaker add lands with stamp speaker", "bananas" in names and names["bananas"]["added_by"] == "speaker")
check("checked-off Eggs removed from canonical", "Eggs" not in names)
check("Bread untouched", "Bread" in names)
check("Keep mirrors the Pi exactly", gl.state() == [("Bread", False), ("Milk x2", False), ("bananas", False)], str(gl.state()))
check("review file absent", not (store.lists / "shopping.review.json").exists())

# idempotent second run: nothing changes
before = (lst, gl.state())
res2 = keep_sync.sync_shopping(store, gl)
check("second run is a no-op", res2 == {"adds": 0, "removes": 0, "keep_delete": 0, "keep_retext": 0, "keep_add": 0}, str(res2))

# hold: 21 new items at Keep
flood = FakeList([(f"diet coke {i}", False) for i in range(21)] + [("Bread", False), ("Milk x2", False), ("bananas", False)])
res3 = keep_sync.sync_shopping(store, flood)
check("flood is held", res3 == {"held": 21}, str(res3))
check("canonical unchanged by the hold", store.get_list("shopping")["count"] == 3)
check("review file written", (store.lists / "shopping.review.json").exists())
check("flood not pushed or deleted in Keep", len(flood.items) == 24)
note = store.inbox("claude", peek=True, user="t")
check("one note for claude about the hold", note["count"] == 1 and "held" in note["notes"][0]["text"])
keep_sync.sync_shopping(store, flood)
check("hold does not re-notify", store.inbox("claude", peek=True, user="t")["count"] == 1)

# exactly 20 is allowed
twenty = FakeList([(f"item {i}", False) for i in range(20)] + [("Bread", False), ("Milk x2", False), ("bananas", False)])
res4 = keep_sync.sync_shopping(store, twenty)
check("20 adds go through and clear the hold", res4.get("adds") == 20 and not (store.lists / "shopping.review.json").exists(), str(res4))

# House notes
store.inbox("claude", peek=False, user="t")  # clear
nl = FakeList([("buy stamps", False), ("already done", True), ("   ", False)])
n = keep_sync.ingest_notes(store, nl)
inbox = store.inbox("claude", peek=True, user="t")
check("House notes ingested into claude inbox and checked off", n == 1 and inbox["count"] == 1 and inbox["notes"][0]["from"] == "speaker/keep" and nl.state() == [("   ", False), ("already done", True), ("buy stamps", True)], str(nl.state()))

print("\n" + ("ALL PASSED" if not fails else f"{fails} FAILED")); sys.exit(1 if fails else 0)
