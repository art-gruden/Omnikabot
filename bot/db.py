"""SQLite: налаштування користувачів, доступ, стан діалогу, зв'язок «картинка → текст»."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL DEFAULT '',
    username   TEXT    NOT NULL DEFAULT '',
    settings   TEXT    NOT NULL DEFAULT '{}',
    state      TEXT,
    state_data TEXT    NOT NULL DEFAULT '{}',
    allowed    INTEGER NOT NULL DEFAULT 0,   -- 1 дозволено, -1 відхилено
    admin      INTEGER NOT NULL DEFAULT 0,
    requested  INTEGER NOT NULL DEFAULT 0,
    created    REAL    NOT NULL,
    seen       REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS renders (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    title      TEXT,             -- NULL: з налаштувань; '' — без заголовка; інакше разовий заголовок
    created    REAL    NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS renders_user ON renders(user_id, created);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS samples (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT    NOT NULL,       -- рубрика: birthday | morning | omnika | unity
    text    TEXT    NOT NULL,
    user_id INTEGER NOT NULL,
    created REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_kind ON samples(kind, created DESC);
CREATE TABLE IF NOT EXISTS drafts (
    id      TEXT    PRIMARY KEY,    -- короткий токен для callback_data
    user_id INTEGER NOT NULL,
    kind    TEXT    NOT NULL,
    source  TEXT    NOT NULL,       -- що надіслав користувач
    text    TEXT    NOT NULL,       -- оформлений текст
    prompt  TEXT,                   -- промт для зображення
    extra   TEXT    NOT NULL DEFAULT '{}',
    created REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS drafts_user ON drafts(user_id, created DESC);
CREATE TABLE IF NOT EXISTS usage (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    what    TEXT    NOT NULL,       -- image | text
    created REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_user ON usage(user_id, created DESC);
"""

SAMPLES_PER_KIND = 60
DRAFTS_PER_USER = 50

RENDERS_PER_USER = 200


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.executescript(SCHEMA)

    def _x(self, sql: str, args=()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, args)

    # ---------- kv
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self._x("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self._x("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)))

    # ---------- users
    def touch_user(self, uid: int, name: str, username: str) -> sqlite3.Row:
        now = time.time()
        with self.lock:
            self._x("INSERT INTO users(id, name, username, created, seen) VALUES(?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username, seen=excluded.seen",
                    (uid, name[:100], username[:64], now, now))
            return self.user(uid)

    def user(self, uid: int) -> sqlite3.Row | None:
        return self._x("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    def users(self, where: str = "1", args=()) -> list[sqlite3.Row]:
        return self._x(f"SELECT * FROM users WHERE {where} ORDER BY seen DESC", args).fetchall()

    def set_user(self, uid: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE users SET {cols} WHERE id=?", (*fields.values(), uid))

    def ensure_user(self, uid: int) -> None:
        now = time.time()
        self._x("INSERT OR IGNORE INTO users(id, created, seen) VALUES(?, ?, ?)", (uid, now, now))

    def settings(self, uid: int, defaults: dict) -> dict:
        row = self.user(uid)
        stored = json.loads(row["settings"]) if row else {}
        return {**defaults, **{k: v for k, v in stored.items() if k in defaults}}

    def save_settings(self, uid: int, settings: dict) -> None:
        self._x("UPDATE users SET settings=? WHERE id=?", (json.dumps(settings, ensure_ascii=False), uid))

    def set_state(self, uid: int, state: str | None, data: dict | None = None) -> None:
        self._x("UPDATE users SET state=?, state_data=? WHERE id=?",
                (state, json.dumps(data or {}, ensure_ascii=False), uid))

    def state(self, uid: int) -> tuple[str | None, dict]:
        row = self.user(uid)
        if not row:
            return None, {}
        return row["state"], json.loads(row["state_data"] or "{}")

    # ---------- renders
    def add_render(self, chat_id: int, message_id: int, user_id: int, text: str, title: str | None) -> None:
        with self.lock:
            self._x("INSERT OR REPLACE INTO renders VALUES(?, ?, ?, ?, ?, ?)",
                    (chat_id, message_id, user_id, text, title, time.time()))
            self._x("DELETE FROM renders WHERE user_id=? AND rowid NOT IN "
                    "(SELECT rowid FROM renders WHERE user_id=? ORDER BY created DESC LIMIT ?)",
                    (user_id, user_id, RENDERS_PER_USER))

    def get_render(self, chat_id: int, message_id: int) -> sqlite3.Row | None:
        return self._x("SELECT * FROM renders WHERE chat_id=? AND message_id=?", (chat_id, message_id)).fetchone()

    def last_render(self, user_id: int) -> sqlite3.Row | None:
        return self._x("SELECT * FROM renders WHERE user_id=? ORDER BY created DESC LIMIT 1", (user_id,)).fetchone()

    # ---------- зразки постів (для аналізу стилю рубрики)
    def add_sample(self, kind: str, text: str, user_id: int) -> bool:
        text = (text or "").strip()
        if len(text) < 20:
            return False
        with self.lock:
            dup = self._x("SELECT 1 FROM samples WHERE kind=? AND text=?", (kind, text)).fetchone()
            if dup:
                return False
            self._x("INSERT INTO samples(kind, text, user_id, created) VALUES(?, ?, ?, ?)",
                    (kind, text, user_id, time.time()))
            self._x("DELETE FROM samples WHERE kind=? AND id NOT IN "
                    "(SELECT id FROM samples WHERE kind=? ORDER BY created DESC LIMIT ?)",
                    (kind, kind, SAMPLES_PER_KIND))
        return True

    def samples(self, kind: str, limit: int = 12) -> list[sqlite3.Row]:
        return self._x("SELECT * FROM samples WHERE kind=? ORDER BY created DESC LIMIT ?", (kind, limit)).fetchall()

    def samples_count(self, kind: str) -> int:
        return self._x("SELECT COUNT(*) c FROM samples WHERE kind=?", (kind,)).fetchone()["c"]

    def delete_sample(self, sid: int) -> None:
        self._x("DELETE FROM samples WHERE id=?", (sid,))

    def clear_samples(self, kind: str) -> int:
        with self.lock:
            n = self.samples_count(kind)
            self._x("DELETE FROM samples WHERE kind=?", (kind,))
        return n

    # ---------- чернетки постів (переживають перезапуск, тому кнопки працюють далі)
    def add_draft(self, user_id: int, kind: str, source: str, text: str,
                  prompt: str | None = None, extra: dict | None = None) -> str:
        did = secrets.token_hex(4)
        with self.lock:
            self._x("INSERT INTO drafts(id, user_id, kind, source, text, prompt, extra, created) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (did, user_id, kind, source, text, prompt, json.dumps(extra or {}, ensure_ascii=False), time.time()))
            self._x("DELETE FROM drafts WHERE user_id=? AND id NOT IN "
                    "(SELECT id FROM drafts WHERE user_id=? ORDER BY created DESC LIMIT ?)",
                    (user_id, user_id, DRAFTS_PER_USER))
        return did

    def draft(self, did: str) -> sqlite3.Row | None:
        return self._x("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()

    def update_draft(self, did: str, **fields) -> None:
        if not fields:
            return
        if "extra" in fields and not isinstance(fields["extra"], str):
            fields["extra"] = json.dumps(fields["extra"], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE drafts SET {cols} WHERE id=?", (*fields.values(), did))

    # ---------- облік генерацій (ліміти)
    def add_usage(self, user_id: int, what: str) -> None:
        with self.lock:
            self._x("INSERT INTO usage(user_id, what, created) VALUES(?, ?, ?)", (user_id, what, time.time()))
            self._x("DELETE FROM usage WHERE created < ?", (time.time() - 7 * 86400,))

    def usage_count(self, user_id: int, what: str, hours: float = 24) -> int:
        row = self._x("SELECT COUNT(*) c FROM usage WHERE user_id=? AND what=? AND created > ?",
                      (user_id, what, time.time() - hours * 3600)).fetchone()
        return row["c"]
