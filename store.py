"""File store for the household memory: markdown topics, documents, inboxes, write log.

Everything lives under STORE_DIR as plain files. Search is SQLite FTS5 over
memory/*.md, held in memory and rebuilt whenever a memory file changes on disk
(our own writes or the Drive mirror).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_TEXT = 2000
MAX_RECALL = 20
ASSISTANTS = ("claude", "chatgpt")
TEXT_DOC_SUFFIXES = {".md", ".txt", ".csv", ".json"}
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DOC_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,127}$")
STAMP_RE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2}) ([^\]/]+)/([^\]]+)\] (.*)$")


class StoreError(ValueError):
    """Raised for invalid input; the message is safe to show to the caller."""


def _today() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_topic(topic: str) -> str:
    t = re.sub(r"[\s_]+", "-", (topic or "").strip().lower())
    t = re.sub(r"-{2,}", "-", t).strip("-")
    if not TOPIC_RE.match(t):
        raise StoreError(
            "topic must be 1-64 characters of letters, digits and hyphens, e.g. europe-trip"
        )
    return t


def clean_text(text: str, what: str) -> str:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if not t:
        raise StoreError(f"{what} is empty")
    if len(t) > MAX_TEXT:
        raise StoreError(f"{what} is {len(t)} characters; the limit is {MAX_TEXT}")
    return t


def parse_line(line: str) -> dict | None:
    m = STAMP_RE.match(line.rstrip("\n"))
    if not m:
        return None
    date, user, assistant, fact = m.groups()
    return {"date": date, "user": user, "assistant": assistant, "fact": fact}


class Store:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.memory = self.root / "memory"
        self.documents = self.root / "documents"
        self.inbox_dir = self.root / "inbox"
        self.lists = self.root / "lists"
        self.log = self.root / "log"
        for d in (self.memory, self.documents, self.inbox_dir, self.lists, self.log):
            d.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        self._db_sig: tuple = ()

    # ---------- write log ----------

    def log_write(self, tool: str, user: str, assistant: str, **detail) -> None:
        rec = {"ts": _now_iso(), "tool": tool, "user": user, "assistant": assistant}
        rec.update(detail)
        with self._lock, open(self.log / "writes.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---------- memory ----------

    def _topic_files(self) -> list[Path]:
        return sorted(p for p in self.memory.glob("*.md") if p.is_file())

    def remember(self, topic: str, fact: str, user: str, assistant: str) -> dict:
        topic = normalize_topic(topic)
        fact = clean_text(fact, "fact")
        line = f"- [{_today()} {user}/{assistant}] {fact}"
        with self._lock:
            path = self.memory / f"{topic}.md"
            new = not path.exists()
            with open(path, "a", encoding="utf-8") as f:
                if new:
                    f.write(f"# {topic}\n\n")
                f.write(line + "\n")
        self.log_write("remember", user, assistant, topic=topic, line=line)
        return {"topic": topic, "line": line, "created_topic": new}

    def list_topics(self) -> list[dict]:
        out = []
        for p in self._topic_files():
            dates = []
            count = 0
            for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
                rec = parse_line(raw)
                if rec:
                    count += 1
                    dates.append(rec["date"])
            last = max(dates) if dates else datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
            out.append({"topic": p.stem, "lines": count, "last_updated": last})
        return out

    def _signature(self) -> tuple:
        return tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in self._topic_files())

    def _index(self) -> sqlite3.Connection:
        sig = self._signature()
        if self._db is not None and sig == self._db_sig:
            return self._db
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.execute(
            "CREATE VIRTUAL TABLE lines USING fts5(topic UNINDEXED, date UNINDEXED, "
            "user UNINDEXED, assistant UNINDEXED, lineno UNINDEXED, fact, stamp UNINDEXED, "
            "tokenize='unicode61')"
        )
        rows = []
        for p in self._topic_files():
            for n, raw in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                rec = parse_line(raw)
                if rec:
                    rows.append((p.stem, rec["date"], rec["user"], rec["assistant"], n, rec["fact"], raw))
        db.executemany("INSERT INTO lines VALUES (?,?,?,?,?,?,?)", rows)
        db.commit()
        if self._db is not None:
            self._db.close()
        self._db, self._db_sig = db, sig
        return db

    @staticmethod
    def _fts_terms(query: str) -> list[str]:
        return [t for t in re.findall(r"[^\W_]+", query, flags=re.UNICODE) if t]

    def recall(self, query: str, topic: str | None = None) -> dict:
        query = (query or "").strip()
        if not query:
            raise StoreError("query is empty")
        topic_n = normalize_topic(topic) if topic else None
        terms = self._fts_terms(query)
        with self._lock:
            db = self._index()
            where = " AND topic = ?" if topic_n else ""
            params: list = [topic_n] if topic_n else []
            hits: list = []
            if terms:
                for joiner in (" AND ", " OR "):
                    match = joiner.join(f'"{t}"*' for t in terms)
                    hits = db.execute(
                        f"SELECT topic, date, user, assistant, lineno, fact, stamp FROM lines "
                        f"WHERE lines MATCH ?{where} ORDER BY date DESC, lineno DESC LIMIT ?",
                        [match, *params, MAX_RECALL],
                    ).fetchall()
                    if hits:
                        break
            if not hits:
                hits = db.execute(
                    f"SELECT topic, date, user, assistant, lineno, fact, stamp FROM lines "
                    f"WHERE fact LIKE ? ESCAPE '\\'{where} ORDER BY date DESC, lineno DESC LIMIT ?",
                    ["%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", *params, MAX_RECALL],
                ).fetchall()
        return {
            "query": query,
            "topic": topic_n,
            "count": len(hits),
            "lines": [
                {"topic": t, "date": d, "by": f"{u}/{a}", "fact": f, "line": s}
                for (t, d, u, a, _n, f, s) in hits
            ],
        }

    # ---------- documents ----------

    def document_names(self) -> list[str]:
        return sorted(p.name for p in self.documents.iterdir() if p.is_file() and not p.name.startswith("."))

    def resolve_document(self, name: str) -> Path:
        name = (name or "").strip()
        if not DOC_NAME_RE.match(name) or ".." in name:
            raise StoreError("document name may only contain letters, digits, spaces, dots, dashes, parentheses")
        path = (self.documents / name).resolve()
        if path.parent != self.documents.resolve() or not path.is_file():
            names = self.document_names()
            hint = ", ".join(names) if names else "(documents/ is empty)"
            raise StoreError(f"no document named {name!r}; available: {hint}")
        return path

    def get_document(self, name: str) -> dict:
        path = self.resolve_document(name)
        info = {"name": path.name, "bytes": path.stat().st_size,
                "modified": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")}
        if path.suffix.lower() in TEXT_DOC_SUFFIXES:
            info["text"] = path.read_text(encoding="utf-8", errors="replace")
        return info

    # ---------- inbox ----------

    def _inbox_path(self, assistant: str, read: bool = False) -> Path:
        if assistant not in ASSISTANTS:
            raise StoreError(f"assistant must be one of {', '.join(ASSISTANTS)}")
        return self.inbox_dir / (f"{assistant}.read.jsonl" if read else f"{assistant}.jsonl")

    def leave_note(self, for_assistant: str, text: str, user: str, assistant: str) -> dict:
        for_assistant = (for_assistant or "").strip().lower()
        text = clean_text(text, "note")
        note_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.urandom(2).hex()}"
        note = {"id": note_id, "ts": _now_iso(), "for": for_assistant, "from": f"{user}/{assistant}", "text": text}
        with self._lock, open(self._inbox_path(for_assistant), "a", encoding="utf-8") as f:
            f.write(json.dumps(note, ensure_ascii=False) + "\n")
        self.log_write("leave_note", user, assistant, note_id=note_id, **{"for": for_assistant})
        return note

    def inbox(self, assistant: str, peek: bool, user: str) -> dict:
        path = self._inbox_path(assistant)
        with self._lock:
            notes = []
            if path.exists():
                for raw in path.read_text(encoding="utf-8").splitlines():
                    raw = raw.strip()
                    if raw:
                        try:
                            notes.append(json.loads(raw))
                        except json.JSONDecodeError:
                            notes.append({"id": "?", "text": raw, "from": "?", "ts": "?"})
            notes.sort(key=lambda n: n.get("ts", ""))
            if notes and not peek:
                with open(self._inbox_path(assistant, read=True), "a", encoding="utf-8") as f:
                    for n in notes:
                        n = dict(n, read_ts=_now_iso(), read_by=user)
                        f.write(json.dumps(n, ensure_ascii=False) + "\n")
                path.write_text("", encoding="utf-8")
        if notes and not peek:
            self.log_write("inbox", user, assistant, cleared=[n.get("id") for n in notes])
        return {"assistant": assistant, "count": len(notes), "peeked": bool(peek), "notes": notes}
