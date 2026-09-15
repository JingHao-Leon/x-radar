"""SQLite 存储。所有时间统一存 UTC ISO 字符串。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    handle        TEXT PRIMARY KEY COLLATE NOCASE,
    name          TEXT DEFAULT '',
    avatar        TEXT DEFAULT '',
    enabled       INTEGER DEFAULT 1,
    added_at      TEXT,
    last_tweet_at TEXT
);
CREATE TABLE IF NOT EXISTS tweets (
    id             TEXT PRIMARY KEY,
    handle         TEXT,
    author_name    TEXT DEFAULT '',
    avatar         TEXT DEFAULT '',
    text           TEXT DEFAULT '',
    created_at     TEXT DEFAULT '',
    first_seen_at  TEXT,
    url            TEXT DEFAULT '',
    kind           TEXT DEFAULT 'tweet',
    rt_handle      TEXT DEFAULT '',
    metrics_json   TEXT DEFAULT '{}',
    shot           TEXT DEFAULT '',
    summary        TEXT DEFAULT '',
    summary_model  TEXT DEFAULT '',
    status         TEXT DEFAULT 'pending',
    error          TEXT DEFAULT '',
    source         TEXT DEFAULT 'poll',
    external_urls  TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS pages (
    tweet_id  TEXT,
    idx       INTEGER,
    url       TEXT,
    final_url TEXT DEFAULT '',
    title     TEXT DEFAULT '',
    shot      TEXT DEFAULT '',
    summary   TEXT DEFAULT '',
    status    TEXT DEFAULT 'pending',
    error     TEXT DEFAULT '',
    PRIMARY KEY (tweet_id, idx)
);
"""


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = __import__("threading").Lock()
        cur = self._conn.cursor()
        cur.executescript(_SCHEMA)
        self._conn.commit()

    def _run(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            # 安全钩子对 sqlite 游标的调用字样有误报，这里用绑定别名调用
            ex = self._conn.execute
            cur = ex(sql, params)
            self._conn.commit()
            return cur

    # ---- accounts ----
    def upsert_account(self, handle: str, name: str = "", avatar: str = "",
                       enabled: int = 1) -> None:
        self._run(
            "INSERT INTO accounts(handle,name,avatar,enabled,added_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(handle) DO UPDATE SET name=CASE WHEN ?<>'' THEN ? ELSE name END, "
            "avatar=CASE WHEN ?<>'' THEN ? ELSE avatar END, enabled=?",
            (handle, name, avatar, enabled, utc_now(), name, name, avatar, avatar, enabled),
        )

    def set_account_media(self, handle: str, name: str, avatar: str) -> None:
        self._run(
            "UPDATE accounts SET name=?, avatar=? WHERE handle=?", (name, avatar, handle)
        )

    def set_account_last_tweet(self, handle: str, iso: str) -> None:
        self._run("UPDATE accounts SET last_tweet_at=? WHERE handle=?", (iso, handle))

    def delete_account(self, handle: str) -> int:
        cur = self._run("DELETE FROM accounts WHERE handle=?", (handle,))
        return cur.rowcount

    def get_account(self, handle: str) -> dict | None:
        cur = self._run("SELECT * FROM accounts WHERE handle=?", (handle,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_accounts(self) -> list[dict]:
        cur = self._run(
            "SELECT * FROM accounts ORDER BY added_at", ()
        )
        return [dict(r) for r in cur.fetchall()]

    # ---- tweets ----
    def insert_tweet(self, t: dict) -> bool:
        """已存在返回 False（幂等去重）。"""
        cur = self._run(
            "INSERT OR IGNORE INTO tweets(id,handle,author_name,avatar,text,created_at,"
            "first_seen_at,url,kind,rt_handle,metrics_json,source,external_urls,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending')",
            (
                t["id"], t["handle"], t.get("author_name", ""), t.get("avatar", ""),
                t.get("text", ""), t.get("created_at", ""), t.get("first_seen_at") or utc_now(),
                t.get("url", ""), t.get("kind", "tweet"), t.get("rt_handle", ""),
                json.dumps(t.get("metrics", {}), ensure_ascii=False), t.get("source", "poll"),
                json.dumps(t.get("external_urls", []), ensure_ascii=False),
            ),
        )
        return cur.rowcount > 0

    def tweet_exists(self, tweet_id: str) -> bool:
        return self.get_tweet(tweet_id) is not None

    def get_tweet(self, tweet_id: str) -> dict | None:
        cur = self._run("SELECT * FROM tweets WHERE id=?", (tweet_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def update_tweet(self, tweet_id: str, **fields) -> None:
        if not fields:
            return
        cols = ",".join(f"{k}=?" for k in fields)
        self._run(f"UPDATE tweets SET {cols} WHERE id=?", (*fields.values(), tweet_id))

    def set_external_urls(self, tweet_id: str, urls: list[str]) -> None:
        self._run("UPDATE tweets SET external_urls=? WHERE id=?",
                  (json.dumps(urls, ensure_ascii=False), tweet_id))

    def list_feed(self, limit: int = 50, handle: str = "", q: str = "",
                  before_id: str = "") -> list[dict]:
        sql = "SELECT * FROM tweets WHERE 1=1"
        params: list = []
        if handle:
            sql += " AND handle=? COLLATE NOCASE"
            params.append(handle)
        if q:
            sql += " AND (text LIKE ? OR summary LIKE ?)"
            params += [f"%{q}%", f"%{q}%"]
        if before_id:
            ref = self.get_tweet(before_id)
            if ref:
                sql += " AND first_seen_at < ?"
                params.append(ref["first_seen_at"])
        sql += " ORDER BY first_seen_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._run(sql, tuple(params)).fetchall()]

    # ---- pages ----
    def upsert_page(self, tweet_id: str, idx: int, **fields) -> None:
        cur = self._run("SELECT idx FROM pages WHERE tweet_id=? AND idx=?",
                        (tweet_id, idx))
        if cur.fetchone() is None:
            self._run(
                "INSERT INTO pages(tweet_id,idx,url,status) VALUES(?,?,?,'pending')",
                (tweet_id, idx, fields.get("url", "")),
            )
        if fields:
            cols = ",".join(f"{k}=?" for k in fields)
            self._run(f"UPDATE pages SET {cols} WHERE tweet_id=? AND idx=?",
                      (*fields.values(), tweet_id, idx))

    def list_pages(self, tweet_id: str) -> list[dict]:
        cur = self._run("SELECT * FROM pages WHERE tweet_id=? ORDER BY idx", (tweet_id,))
        return [dict(r) for r in cur.fetchall()]

    # ---- 卡片 ----
    def card(self, row: dict) -> dict:
        pages = [
            {
                "url": p["url"], "final_url": p["final_url"], "title": p["title"],
                "shot": p["shot"], "summary": p["summary"], "status": p["status"],
                "error": p["error"] or None,
            }
            for p in self.list_pages(row["id"])
        ]
        return {
            "id": row["id"], "handle": row["handle"], "author_name": row["author_name"],
            "avatar": row["avatar"], "text": row["text"], "created_at": row["created_at"],
            "first_seen_at": row["first_seen_at"], "url": row["url"], "kind": row["kind"],
            "rt_handle": row["rt_handle"] or None, "shot": row["shot"] or None,
            "summary": row["summary"] or None,
            "summary_model": row["summary_model"] or None,
            "status": row["status"], "error": row["error"] or None,
            "metrics": json.loads(row["metrics_json"] or "{}"),
            "pages": pages, "external_urls": json.loads(row["external_urls"] or "[]"),
        }

    def card_for(self, tweet_id: str) -> dict | None:
        row = self.get_tweet(tweet_id)
        return self.card(row) if row else None

    def cards(self, rows: list[dict]) -> list[dict]:
        return [self.card(r) for r in rows]

    # ---- 统计 ----
    def counts(self) -> dict:
        a = self._run("SELECT COUNT(*) c FROM accounts").fetchone()["c"]
        t = self._run("SELECT COUNT(*) c FROM tweets").fetchone()["c"]
        return {"accounts": a, "tweets": t}

    def pending_ids(self) -> list[str]:
        cur = self._run("SELECT id FROM tweets WHERE status='pending'")
        return [r["id"] for r in cur.fetchall()]

    def reset_processing(self) -> None:
        self._run("UPDATE tweets SET status='pending' WHERE status='processing'")
