# coding=utf-8
"""Durable SQLite outbox for exactly-once-oriented WSJ delivery."""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .models import AlreadyRunning, FeedArticle, FetchedArticle


AUTOMATIC_STATUSES = ("discovered", "fetch_pending", "fetched", "doc_created")
ALL_STATUSES = AUTOMATIC_STATUSES + (
    "shared",
    "notified",
    "unknown",
    "manual",
    "baseline",
)
PUBLISHER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class ProcessLock:
    """Non-blocking host lock used in addition to SQLite transactions."""

    def __init__(self, db_path: Path) -> None:
        self.path = Path(f"{db_path}.lock")
        self._fd: Optional[int] = None

    def __enter__(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise AlreadyRunning("已有 WSJ delivery 进程正在运行") from exc
        self._fd = fd
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


class Outbox:
    """A synchronous outbox whose every remote stage is committed immediately."""

    def __init__(self, path: Path, publisher: str = "wsj") -> None:
        publisher = str(publisher or "").strip().lower()
        if not PUBLISHER_PATTERN.fullmatch(publisher):
            raise ValueError("publisher must be a lowercase identifier")
        self.publisher = publisher
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self._conn = sqlite3.connect(self.path, timeout=5)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create_schema()
        os.chmod(self.path, 0o600)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Outbox":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS articles (
                article_key TEXT PRIMARY KEY,
                publisher TEXT NOT NULL DEFAULT 'wsj',
                article_id TEXT NOT NULL DEFAULT '',
                normalized_url TEXT NOT NULL UNIQUE,
                source_url TEXT NOT NULL,
                feed_title TEXT NOT NULL,
                feed_published_at TEXT NOT NULL DEFAULT '',
                feed_author TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error_code TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                bpc_request_id TEXT NOT NULL DEFAULT '',
                canonical_url TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                paragraphs_json TEXT NOT NULL DEFAULT '[]',
                body_items_json TEXT NOT NULL DEFAULT '[]',
                body_sha256 TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL DEFAULT '',
                document_id TEXT NOT NULL DEFAULT '',
                document_url TEXT NOT NULL DEFAULT '',
                block_cursor INTEGER NOT NULL DEFAULT 0,
                document_block_index INTEGER NOT NULL DEFAULT 0,
                image_states_json TEXT NOT NULL DEFAULT '{}',
                render_plan_json TEXT NOT NULL DEFAULT '',
                document_create_started_at REAL,
                document_retention_state TEXT NOT NULL DEFAULT 'active',
                document_delete_started_at REAL,
                document_delete_next_attempt_at REAL NOT NULL DEFAULT 0,
                document_delete_retry_count INTEGER NOT NULL DEFAULT 0,
                document_delete_error_code TEXT NOT NULL DEFAULT '',
                document_delete_error TEXT NOT NULL DEFAULT '',
                document_deleted_at REAL,
                message_uuid TEXT NOT NULL DEFAULT '',
                notification_started_at REAL,
                discovered_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                notified_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_wsj_articles_work
                ON articles(status, next_attempt_at, discovered_at);
            CREATE INDEX IF NOT EXISTS idx_wsj_articles_message
                ON articles(message_uuid, status);

            CREATE TABLE IF NOT EXISTS image_blobs (
                article_key TEXT NOT NULL,
                cursor INTEGER NOT NULL,
                source_url TEXT NOT NULL,
                final_url TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                extension TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                width INTEGER NOT NULL DEFAULT 0,
                height INTEGER NOT NULL DEFAULT 0,
                data BLOB NOT NULL,
                PRIMARY KEY(article_key, cursor),
                FOREIGN KEY(article_key) REFERENCES articles(article_key)
            );

            CREATE TABLE IF NOT EXISTS alerts (
                kind TEXT PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 1,
                started_at REAL NOT NULL,
                last_sent_at REAL,
                recovered_at REAL
            );

            CREATE TABLE IF NOT EXISTS circuit (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                kind TEXT NOT NULL,
                until_at REAL NOT NULL,
                activated_at REAL NOT NULL
            );
            """
        )
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(articles)").fetchall()
        }
        if "publisher" not in columns:
            self._conn.execute(
                "ALTER TABLE articles ADD COLUMN publisher TEXT NOT NULL DEFAULT 'wsj'"
            )
        if "document_create_started_at" not in columns:
            self._conn.execute(
                "ALTER TABLE articles ADD COLUMN document_create_started_at REAL"
            )
        if "notification_started_at" not in columns:
            self._conn.execute(
                "ALTER TABLE articles ADD COLUMN notification_started_at REAL"
            )
        if "body_items_json" not in columns:
            self._conn.execute(
                "ALTER TABLE articles ADD COLUMN body_items_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "document_block_index" not in columns:
            self._conn.execute(
                "ALTER TABLE articles ADD COLUMN document_block_index INTEGER NOT NULL DEFAULT 0"
            )
            self._conn.execute(
                "UPDATE articles SET document_block_index=block_cursor"
            )
        if "image_states_json" not in columns:
            self._conn.execute(
                "ALTER TABLE articles ADD COLUMN image_states_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "render_plan_json" not in columns:
            self._conn.execute(
                "ALTER TABLE articles ADD COLUMN render_plan_json TEXT NOT NULL DEFAULT ''"
            )
        retention_columns = {
            "document_retention_state": "TEXT NOT NULL DEFAULT 'active'",
            "document_delete_started_at": "REAL",
            "document_delete_next_attempt_at": "REAL NOT NULL DEFAULT 0",
            "document_delete_retry_count": "INTEGER NOT NULL DEFAULT 0",
            "document_delete_error_code": "TEXT NOT NULL DEFAULT ''",
            "document_delete_error": "TEXT NOT NULL DEFAULT ''",
            "document_deleted_at": "REAL",
        }
        for name, definition in retention_columns.items():
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE articles ADD COLUMN {name} {definition}"
                )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wsj_articles_retention "
            "ON articles(document_retention_state, document_delete_next_attempt_at, notified_at)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_publisher_work "
            "ON articles(publisher, status, next_attempt_at, discovered_at)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_publisher_message "
            "ON articles(publisher, message_uuid, status)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_publisher_canonical "
            "ON articles(publisher, canonical_url) WHERE canonical_url!=''"
        )

        image_columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(image_blobs)").fetchall()
        }
        if "width" not in image_columns:
            self._conn.execute(
                "ALTER TABLE image_blobs ADD COLUMN width INTEGER NOT NULL DEFAULT 0"
            )
        if "height" not in image_columns:
            self._conn.execute(
                "ALTER TABLE image_blobs ADD COLUMN height INTEGER NOT NULL DEFAULT 0"
            )

        # Old databases used process-global feed state. Existing rows and state
        # belong to the original WSJ runner; keep them intact while making new
        # publishers initialize independently.
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) "
            "SELECT 'feed_initialized:wsj', value FROM meta "
            "WHERE key='feed_initialized'"
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) "
            "SELECT 'initialized_at:wsj', value FROM meta "
            "WHERE key='initialized_at'"
        )

        # Alert keys are namespaced in-place because the legacy table's primary
        # key is only ``kind``. Legacy unscoped entries can only be WSJ entries.
        self._conn.execute(
            "UPDATE alerts SET kind='wsj:' || kind WHERE instr(kind, ':')=0"
        )

        # The legacy circuit table has a singleton constraint and therefore
        # cannot isolate two runners. Migrate its only possible row to scoped
        # meta state; new reads/writes use meta exclusively.
        legacy_circuit = self._conn.execute(
            "SELECT kind, until_at, activated_at FROM circuit WHERE singleton=1"
        ).fetchone()
        if legacy_circuit is not None:
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                (
                    "circuit:wsj",
                    json.dumps(
                        {
                            "kind": str(legacy_circuit["kind"]),
                            "until_at": float(legacy_circuit["until_at"]),
                            "activated_at": float(legacy_circuit["activated_at"]),
                        },
                        separators=(",", ":"),
                    ),
                ),
            )
            self._conn.execute("DELETE FROM circuit WHERE singleton=1")
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '8')"
        )
        self._conn.execute(
            "UPDATE meta SET value='8' WHERE key='schema_version'"
        )
        self._conn.commit()

    def is_initialized(self) -> bool:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            (self._meta_key("feed_initialized"),),
        ).fetchone()
        return bool(row and row["value"] == "1")

    def initialize_with_items(self, items: Sequence[FeedArticle], now: float) -> int:
        """Atomically seed state from the current feed and allow future normal runs."""
        with self._conn:
            inserted = self._insert_items(items, now)
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, '1') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self._meta_key("feed_initialized"),),
            )
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self._meta_key("initialized_at"), str(now)),
            )
        return inserted

    def initialize_current_only(
        self, items: Sequence[FeedArticle], now: float
    ) -> int:
        """Record the current snapshot as a terminal baseline without remote writes."""
        with self._conn:
            inserted = self._insert_items(items, now, initial_status="baseline")
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, '1') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self._meta_key("feed_initialized"),),
            )
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self._meta_key("initialized_at"), str(now)),
            )
        return inserted

    def discover(self, items: Sequence[FeedArticle], now: float) -> int:
        with self._conn:
            return self._insert_items(items, now)

    def _insert_items(
        self,
        items: Sequence[FeedArticle],
        now: float,
        *,
        initial_status: str = "discovered",
    ) -> int:
        if initial_status not in {"discovered", "baseline"}:
            raise ValueError("invalid initial article status")
        before = self._conn.total_changes
        for item in items:
            if item.publisher != self.publisher:
                raise ValueError("feed item publisher does not match outbox scope")
            self._conn.execute(
                """
                INSERT OR IGNORE INTO articles (
                    article_key, publisher, article_id, normalized_url, source_url,
                    feed_title, feed_published_at, feed_author,
                    status, discovered_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.article_key,
                    self.publisher,
                    item.article_id,
                    item.normalized_url,
                    item.source_url,
                    item.title,
                    item.published_at,
                    item.author,
                    initial_status,
                    now,
                    now,
                ),
            )
        return self._conn.total_changes - before

    def get(self, article_key: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM articles WHERE article_key = ?", (article_key,)
        ).fetchone()
        return dict(row) if row else None

    def get_work(self, now: float, limit: int) -> list[dict]:
        placeholders = ",".join("?" for _ in AUTOMATIC_STATUSES)
        rows = self._conn.execute(
            f"""
            SELECT * FROM articles
            WHERE publisher=? AND status IN ({placeholders}) AND next_attempt_at <= ?
            ORDER BY CASE status
                WHEN 'doc_created' THEN 0
                WHEN 'fetched' THEN 1
                WHEN 'fetch_pending' THEN 2
                ELSE 3
            END, discovered_at, article_key
            LIMIT ?
            """,
            (self.publisher, *AUTOMATIC_STATUSES, now, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def has_eligible_work(self, now: float) -> bool:
        placeholders = ",".join("?" for _ in AUTOMATIC_STATUSES)
        row = self._conn.execute(
            f"SELECT 1 FROM articles WHERE publisher=? AND status IN ({placeholders}) "
            "AND next_attempt_at <= ? LIMIT 1",
            (self.publisher, *AUTOMATIC_STATUSES, now),
        ).fetchone()
        return row is not None

    def mark_fetch_pending(self, article_key: str, request_id: str, now: float) -> None:
        self._update(
            """
            UPDATE articles SET status='fetch_pending', bpc_request_id=?,
                next_attempt_at=0, last_error_code='', last_error='', updated_at=?
            WHERE article_key=? AND status='discovered'
            """,
            (request_id, now, article_key),
        )

    def mark_fetched(
        self, article_key: str, article: FetchedArticle, now: float
    ) -> bool:
        """Persist fetched content, refusing a second owner of one canonical URL."""
        with self._conn:
            owner = self._conn.execute(
                """
                SELECT article_key FROM articles
                WHERE publisher=? AND canonical_url=? AND article_key!=?
                ORDER BY discovered_at, article_key LIMIT 1
                """,
                (self.publisher, article.canonical_url, article_key),
            ).fetchone()
            if owner is not None:
                cursor = self._conn.execute(
                    """
                    UPDATE articles SET status='manual', canonical_url=?, title=?,
                        author=?, published_at=?, body_sha256=?, fetched_at=?,
                        bpc_request_id=?, retry_count=retry_count+1,
                        consecutive_failures=consecutive_failures+1,
                        next_attempt_at=0, last_error_code='DUPLICATE_CANONICAL',
                        last_error='规范地址已由另一条 outbox 记录持有，未创建重复文档',
                        updated_at=?
                    WHERE article_key=? AND publisher=? AND status='fetch_pending'
                    """,
                    (
                        article.canonical_url,
                        article.title,
                        article.author,
                        article.published_at,
                        article.sha256,
                        article.fetched_at,
                        article.request_id,
                        now,
                        article_key,
                        self.publisher,
                    ),
                )
                return False
            cursor = self._conn.execute(
                """
                UPDATE articles SET status='fetched', canonical_url=?, title=?, author=?,
                    published_at=?, paragraphs_json=?, body_items_json=?, body_sha256=?, fetched_at=?,
                    bpc_request_id=?, consecutive_failures=0, next_attempt_at=0,
                    last_error_code='', last_error='', updated_at=?
                WHERE article_key=? AND publisher=? AND status='fetch_pending'
                """,
                (
                    article.canonical_url,
                    article.title,
                    article.author,
                    article.published_at,
                    json.dumps(article.paragraphs, ensure_ascii=False),
                    json.dumps(article.body_items, ensure_ascii=False),
                    article.sha256,
                    article.fetched_at,
                    article.request_id,
                    now,
                    article_key,
                    self.publisher,
                ),
            )
            return cursor.rowcount == 1

    def mark_doc_create_started(self, article_key: str, now: float) -> None:
        self._update(
            """
            UPDATE articles SET document_create_started_at=?, updated_at=?
            WHERE article_key=? AND status='fetched'
                AND document_create_started_at IS NULL
            """,
            (now, now, article_key),
        )

    def freeze_render_plan(
        self, article_key: str, plan: Sequence[dict], now: float
    ) -> None:
        """Persist the exact plan before remote creation; never rewrite it."""
        encoded = json.dumps(list(plan), ensure_ascii=False, separators=(",", ":"))
        self._update(
            """
            UPDATE articles SET render_plan_json=?, updated_at=?
            WHERE article_key=? AND status IN ('fetched', 'doc_created')
                AND render_plan_json=''
            """,
            (encoded, now, article_key),
        )

    def clear_doc_create_started(self, article_key: str, now: float) -> None:
        self._update(
            """
            UPDATE articles SET document_create_started_at=NULL, updated_at=?
            WHERE article_key=? AND status='fetched'
            """,
            (now, article_key),
        )

    def mark_doc_created(
        self,
        article_key: str,
        document_id: str,
        document_url: str,
        now: float,
    ) -> None:
        self._update(
            """
            UPDATE articles SET status='doc_created', document_id=?, document_url=?,
                block_cursor=0, document_block_index=0, image_states_json='{}',
                document_retention_state='active', document_delete_started_at=NULL,
                document_delete_next_attempt_at=0, document_delete_retry_count=0,
                document_delete_error_code='', document_delete_error='',
                document_deleted_at=NULL,
                consecutive_failures=0, next_attempt_at=0,
                last_error_code='', last_error='', updated_at=?
            WHERE article_key=? AND status='fetched'
            """,
            (document_id, document_url, now, article_key),
        )

    def advance_blocks(
        self,
        article_key: str,
        cursor: int,
        now: float,
        document_block_index: Optional[int] = None,
    ) -> None:
        if document_block_index is None:
            document_block_index = cursor
        self._update(
            """
            UPDATE articles SET block_cursor=?, document_block_index=?, consecutive_failures=0,
                next_attempt_at=0, last_error_code='', last_error='', updated_at=?
            WHERE article_key=? AND status='doc_created'
            """,
            (cursor, document_block_index, now, article_key),
        )

    def mark_image_skipped(
        self,
        article_key: str,
        cursor: int,
        source_url: str,
        code: str,
        now: float,
        cursor_advance: int = 1,
    ) -> None:
        """Skip a rejected image before an Image Block exists."""
        with self._conn:
            row = self._conn.execute(
                "SELECT image_states_json, document_block_index FROM articles "
                "WHERE article_key=? AND status='doc_created' AND block_cursor=?",
                (article_key, cursor),
            ).fetchone()
            if row is None:
                return
            states = _decode_image_states(row["image_states_json"])
            states[str(cursor)] = {
                "state": "skipped",
                "source_url": source_url,
                "error_code": code[:100],
            }
            self._conn.execute(
                "DELETE FROM image_blobs WHERE article_key=? AND cursor=?",
                (article_key, cursor),
            )
            self._conn.execute(
                "UPDATE articles SET block_cursor=?, image_states_json=?, updated_at=? "
                "WHERE article_key=? AND status='doc_created' AND block_cursor=?",
                (
                    cursor + max(1, cursor_advance),
                    json.dumps(states, ensure_ascii=False, separators=(",", ":")),
                    now,
                    article_key,
                    cursor,
                ),
            )

    def prepare_image(
        self,
        article_key: str,
        cursor: int,
        *,
        source_url: str,
        final_url: str,
        mime_type: str,
        extension: str,
        sha256: str,
        data: bytes,
        now: float,
        width: int = 0,
        height: int = 0,
    ) -> None:
        """Atomically spool validated bytes before any remote image write."""
        with self._conn:
            row = self._conn.execute(
                "SELECT image_states_json FROM articles WHERE article_key=? "
                "AND status='doc_created' AND block_cursor=?",
                (article_key, cursor),
            ).fetchone()
            if row is None:
                return
            self._conn.execute(
                """
                INSERT INTO image_blobs(
                    article_key, cursor, source_url, final_url, mime_type,
                    extension, sha256, size, width, height, data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(article_key, cursor) DO NOTHING
                """,
                (
                    article_key,
                    cursor,
                    source_url,
                    final_url,
                    mime_type,
                    extension,
                    sha256,
                    len(data),
                    max(0, int(width)),
                    max(0, int(height)),
                    sqlite3.Binary(data),
                ),
            )
            states = _decode_image_states(row["image_states_json"])
            states[str(cursor)] = {
                "state": "prepared",
                "source_url": source_url,
                "sha256": sha256,
                "mime_type": mime_type,
                "size": len(data),
                "width": max(0, int(width)),
                "height": max(0, int(height)),
            }
            self._conn.execute(
                "UPDATE articles SET image_states_json=?, updated_at=? "
                "WHERE article_key=? AND status='doc_created' AND block_cursor=?",
                (
                    json.dumps(states, ensure_ascii=False, separators=(",", ":")),
                    now,
                    article_key,
                    cursor,
                ),
            )

    def get_prepared_image(self, article_key: str, cursor: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM image_blobs WHERE article_key=? AND cursor=?",
            (article_key, cursor),
        ).fetchone()
        return dict(row) if row else None

    def backfill_prepared_image_dimensions(
        self,
        article_key: str,
        cursor: int,
        width: int,
        height: int,
        now: float,
    ) -> None:
        """Repair a pre-schema-v8 image spool after reading its trusted bytes."""
        if width <= 0 or height <= 0:
            return
        with self._conn:
            self._conn.execute(
                "UPDATE image_blobs SET width=?, height=? "
                "WHERE article_key=? AND cursor=? AND (width<=0 OR height<=0)",
                (int(width), int(height), article_key, cursor),
            )
            row = self._conn.execute(
                "SELECT image_states_json FROM articles WHERE article_key=?",
                (article_key,),
            ).fetchone()
            if row is None:
                return
            states = _decode_image_states(row["image_states_json"])
            state = dict(states.get(str(cursor)) or {})
            if state:
                state["width"] = int(width)
                state["height"] = int(height)
                states[str(cursor)] = state
                self._conn.execute(
                    "UPDATE articles SET image_states_json=?, updated_at=? "
                    "WHERE article_key=?",
                    (
                        json.dumps(
                            states,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        now,
                        article_key,
                    ),
                )

    def mark_image_block_created(
        self,
        article_key: str,
        cursor: int,
        source_url: str,
        block_id: str,
        sha256: str,
        mime_type: str,
        size: int,
        width: int,
        height: int,
        now: float,
    ) -> None:
        self._set_image_state(
            article_key,
            cursor,
            {
                "state": "block_created",
                "source_url": source_url,
                "block_id": block_id,
                "sha256": sha256,
                "mime_type": mime_type,
                "size": int(size),
                "width": int(width),
                "height": int(height),
                "upload_started_at": None,
                "file_token": "",
            },
            now,
        )

    def mark_image_upload_started(
        self, article_key: str, cursor: int, now: float
    ) -> None:
        self._update_image_fields(
            article_key, cursor, {"upload_started_at": now}, now
        )

    def clear_image_upload_started(
        self, article_key: str, cursor: int, now: float
    ) -> None:
        self._update_image_fields(
            article_key, cursor, {"upload_started_at": None}, now
        )

    def mark_image_uploaded(
        self, article_key: str, cursor: int, file_token: str, now: float
    ) -> None:
        self._update_image_fields(
            article_key,
            cursor,
            {
                "state": "uploaded",
                "file_token": file_token,
                "upload_started_at": None,
            },
            now,
        )

    def mark_image_bound(self, article_key: str, cursor: int, now: float) -> None:
        """Atomically record binding and move both logical/remote cursors."""
        with self._conn:
            row = self._conn.execute(
                "SELECT image_states_json, document_block_index FROM articles "
                "WHERE article_key=? AND status='doc_created' AND block_cursor=?",
                (article_key, cursor),
            ).fetchone()
            if row is None:
                return
            states = _decode_image_states(row["image_states_json"])
            state = dict(states.get(str(cursor)) or {})
            state["state"] = "bound"
            states[str(cursor)] = state
            self._conn.execute(
                "DELETE FROM image_blobs WHERE article_key=? AND cursor=?",
                (article_key, cursor),
            )
            self._conn.execute(
                "UPDATE articles SET block_cursor=?, document_block_index=?, "
                "image_states_json=?, consecutive_failures=0, next_attempt_at=0, "
                "last_error_code='', last_error='', updated_at=? "
                "WHERE article_key=? AND status='doc_created' AND block_cursor=?",
                (
                    cursor + 1,
                    int(row["document_block_index"]) + 1,
                    json.dumps(states, ensure_ascii=False, separators=(",", ":")),
                    now,
                    article_key,
                    cursor,
                ),
            )

    def _set_image_state(
        self, article_key: str, cursor: int, value: dict, now: float
    ) -> None:
        with self._conn:
            row = self._conn.execute(
                "SELECT image_states_json FROM articles WHERE article_key=? "
                "AND status='doc_created' AND block_cursor=?",
                (article_key, cursor),
            ).fetchone()
            if row is None:
                return
            states = _decode_image_states(row["image_states_json"])
            states[str(cursor)] = value
            self._conn.execute(
                "UPDATE articles SET image_states_json=?, updated_at=? "
                "WHERE article_key=? AND status='doc_created' AND block_cursor=?",
                (
                    json.dumps(states, ensure_ascii=False, separators=(",", ":")),
                    now,
                    article_key,
                    cursor,
                ),
            )

    def _update_image_fields(
        self, article_key: str, cursor: int, fields: dict, now: float
    ) -> None:
        with self._conn:
            row = self._conn.execute(
                "SELECT image_states_json FROM articles WHERE article_key=? "
                "AND status='doc_created' AND block_cursor=?",
                (article_key, cursor),
            ).fetchone()
            if row is None:
                return
            states = _decode_image_states(row["image_states_json"])
            state = dict(states.get(str(cursor)) or {})
            state.update(fields)
            states[str(cursor)] = state
            self._conn.execute(
                "UPDATE articles SET image_states_json=?, updated_at=? "
                "WHERE article_key=? AND status='doc_created' AND block_cursor=?",
                (
                    json.dumps(states, ensure_ascii=False, separators=(",", ":")),
                    now,
                    article_key,
                    cursor,
                ),
            )

    def mark_shared(self, article_key: str, now: float) -> None:
        self._update(
            """
            UPDATE articles SET status='shared', consecutive_failures=0,
                next_attempt_at=0, last_error_code='', last_error='', updated_at=?
            WHERE article_key=? AND status='doc_created'
            """,
            (now, article_key),
        )

    def schedule_retry(
        self,
        article_key: str,
        code: str,
        message: str,
        now: float,
        delay: float,
    ) -> None:
        self._update(
            """
            UPDATE articles SET retry_count=retry_count+1,
                consecutive_failures=consecutive_failures+1,
                next_attempt_at=?, last_error_code=?, last_error=?, updated_at=?
            WHERE article_key=? AND status NOT IN ('notified', 'unknown', 'manual')
            """,
            (now + delay, code[:100], message[:500], now, article_key),
        )

    def mark_terminal(
        self,
        article_key: str,
        status: str,
        code: str,
        message: str,
        now: float,
    ) -> None:
        if status not in {"unknown", "manual"}:
            raise ValueError("terminal status must be unknown or manual")
        self._update(
            """
            UPDATE articles SET status=?, retry_count=retry_count+1,
                consecutive_failures=consecutive_failures+1,
                next_attempt_at=0, last_error_code=?, last_error=?, updated_at=?
            WHERE article_key=? AND status != 'notified'
            """,
            (status, code[:100], message[:500], now, article_key),
        )

    def retry_delay(self, article_key: str, base: int, maximum: int) -> int:
        row = self._conn.execute(
            "SELECT consecutive_failures FROM articles WHERE article_key=?",
            (article_key,),
        ).fetchone()
        failures = int(row[0]) if row else 0
        return min(maximum, base * (2 ** min(failures, 8)))

    def pending_shared(self, now: float) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT * FROM articles AS current
            WHERE current.publisher=? AND status='shared' AND next_attempt_at <= ?
                AND (
                    message_uuid=''
                    OR NOT EXISTS (
                        SELECT 1 FROM articles AS peer
                        WHERE peer.message_uuid=current.message_uuid
                            AND peer.publisher=current.publisher
                            AND (
                                peer.status!='shared'
                                OR peer.next_attempt_at > ?
                            )
                    )
                )
            ORDER BY CASE WHEN message_uuid='' THEN 1 ELSE 0 END,
                message_uuid, discovered_at, article_key
            """,
            (self.publisher, now, now),
        ).fetchall()
        return [dict(row) for row in rows]

    def reconcile_partial_message_groups(self, now: float) -> None:
        """Never resend a deterministic UUID with only a subset of its links."""
        self._update(
            """
            UPDATE articles SET status='unknown', retry_count=retry_count+1,
                consecutive_failures=consecutive_failures+1, next_attempt_at=0,
                last_error_code='FEISHU_MESSAGE_GROUP_PARTIAL',
                last_error='同一消息 UUID 的文章状态不完整，需人工核对', updated_at=?
            WHERE publisher=? AND status='shared' AND message_uuid!=''
                AND EXISTS (
                    SELECT 1 FROM articles AS peer
                    WHERE peer.message_uuid=articles.message_uuid
                        AND peer.publisher=articles.publisher
                        AND peer.status!='shared'
                )
            """,
            (now, self.publisher),
        )

    def schedule_message_retry(
        self,
        article_keys: Sequence[str],
        value: str,
        code: str,
        message: str,
        now: float,
        base: int,
        maximum: int,
    ) -> None:
        if not article_keys:
            return
        placeholders = ",".join("?" for _ in article_keys)
        with self._conn:
            row = self._conn.execute(
                f"SELECT MAX(consecutive_failures) FROM articles "
                f"WHERE article_key IN ({placeholders}) AND status='shared' "
                "AND message_uuid=? AND publisher=?",
                (*article_keys, value, self.publisher),
            ).fetchone()
            failures = int(row[0] or 0) if row else 0
            delay = min(maximum, base * (2 ** min(failures, 8)))
            self._conn.execute(
                f"""
                UPDATE articles SET retry_count=retry_count+1,
                    consecutive_failures=consecutive_failures+1,
                    next_attempt_at=?, last_error_code=?, last_error=?,
                    notification_started_at=NULL, updated_at=?
                WHERE article_key IN ({placeholders}) AND status='shared'
                    AND message_uuid=? AND publisher=?
                """,
                (
                    now + delay,
                    code[:100],
                    message[:500],
                    now,
                    *article_keys,
                    value,
                    self.publisher,
                ),
            )

    def mark_message_terminal(
        self,
        article_keys: Sequence[str],
        value: str,
        status: str,
        code: str,
        message: str,
        now: float,
    ) -> None:
        if status not in {"unknown", "manual"}:
            raise ValueError("message terminal status must be unknown or manual")
        if not article_keys:
            return
        placeholders = ",".join("?" for _ in article_keys)
        self._update(
            f"""
            UPDATE articles SET status=?, retry_count=retry_count+1,
                consecutive_failures=consecutive_failures+1, next_attempt_at=0,
                last_error_code=?, last_error=?, updated_at=?
            WHERE article_key IN ({placeholders}) AND status='shared'
                AND message_uuid=? AND publisher=?
            """,
            (
                status,
                code[:100],
                message[:500],
                now,
                *article_keys,
                value,
                self.publisher,
            ),
        )

    def assign_message_uuid(self, article_keys: Sequence[str], value: str, now: float) -> None:
        if not article_keys:
            return
        placeholders = ",".join("?" for _ in article_keys)
        self._update(
            f"""
            UPDATE articles SET message_uuid=?, updated_at=?
            WHERE article_key IN ({placeholders}) AND status='shared' AND message_uuid=''
                AND publisher=?
            """,
            (value, now, *article_keys, self.publisher),
        )

    def mark_notification_started(
        self, article_keys: Sequence[str], value: str, now: float
    ) -> None:
        if not article_keys:
            return
        placeholders = ",".join("?" for _ in article_keys)
        self._update(
            f"""
            UPDATE articles SET notification_started_at=?, updated_at=?
            WHERE article_key IN ({placeholders}) AND status='shared'
                AND message_uuid=? AND notification_started_at IS NULL
                AND publisher=?
            """,
            (now, now, *article_keys, value, self.publisher),
        )

    def clear_notification_started(
        self, article_keys: Sequence[str], value: str, now: float
    ) -> None:
        if not article_keys:
            return
        placeholders = ",".join("?" for _ in article_keys)
        self._update(
            f"""
            UPDATE articles SET notification_started_at=NULL, updated_at=?
            WHERE article_key IN ({placeholders}) AND status='shared'
                AND message_uuid=? AND publisher=?
            """,
            (now, *article_keys, value, self.publisher),
        )

    def mark_notified(self, article_keys: Sequence[str], value: str, now: float) -> None:
        if not article_keys:
            return
        placeholders = ",".join("?" for _ in article_keys)
        self._update(
            f"""
            UPDATE articles SET status='notified', notified_at=?, updated_at=?,
                consecutive_failures=0, next_attempt_at=0,
                last_error_code='', last_error=''
            WHERE article_key IN ({placeholders}) AND status='shared' AND message_uuid=?
                AND publisher=?
            """,
            (now, now, *article_keys, value, self.publisher),
        )

    def occupied_document_count(self) -> int:
        """Count confirmed Docx tokens plus uncertain creation slots.

        Every row with a document ID counts regardless of article status;
        manual/unknown/in-progress rows are protected from automatic deletion
        but still consume the configured cloud-document allowance.  An
        interrupted/uncertain create without a returned token reserves one
        conservative slot because the document may exist remotely.
        """
        row = self._conn.execute(
            """
            SELECT
                COUNT(DISTINCT CASE
                    WHEN document_id!='' AND document_retention_state!='deleted'
                    THEN document_id END
                ) +
                SUM(CASE
                    WHEN document_id='' AND status='unknown'
                        AND document_create_started_at IS NOT NULL
                    THEN 1 ELSE 0 END)
            FROM articles
            """
        ).fetchone()
        return int(row[0] or 0) if row else 0

    def retention_excess(self, limit: int) -> int:
        return max(0, self.occupied_document_count() - int(limit))

    def retention_candidates(self, limit: int) -> list[dict]:
        """Return pending intents plus only the oldest notified overflow rows."""
        excess = self.retention_excess(limit)
        if excess <= 0:
            return []
        pending = self._conn.execute(
            """
            SELECT * FROM articles
            WHERE status='notified' AND document_id!=''
                AND document_deleted_at IS NULL
                AND document_retention_state='delete_pending'
            ORDER BY COALESCE(document_create_started_at, notified_at,
                    updated_at, discovered_at), article_key
            """
        ).fetchall()
        blocked = self._conn.execute(
            """
            SELECT COUNT(DISTINCT document_id) FROM articles
            WHERE status='notified' AND document_id!=''
                AND document_deleted_at IS NULL
                AND document_retention_state='blocked'
            """
        ).fetchone()
        reserved = len({str(row["document_id"]) for row in pending})
        reserved += int(blocked[0] or 0) if blocked else 0
        needed = max(0, excess - reserved)
        planned: list[sqlite3.Row] = []
        if needed:
            planned = self._conn.execute(
                """
                SELECT * FROM articles AS current
                WHERE status='notified' AND document_id!=''
                    AND document_deleted_at IS NULL
                    AND document_retention_state='active'
                    AND NOT EXISTS (
                        SELECT 1 FROM articles AS duplicate
                        WHERE duplicate.article_key!=current.article_key
                            AND duplicate.document_id=current.document_id
                            AND duplicate.document_retention_state!='deleted'
                    )
                ORDER BY COALESCE(document_create_started_at, notified_at,
                        updated_at, discovered_at), article_key
                LIMIT ?
                """,
                (needed,),
            ).fetchall()
        return [dict(row) for row in [*pending, *planned]]

    def begin_document_delete(self, article_key: str, now: float) -> bool:
        """Persist DELETE intent before the remote idempotent request."""
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE articles SET document_retention_state='delete_pending',
                    document_delete_started_at=COALESCE(document_delete_started_at, ?),
                    document_delete_next_attempt_at=0,
                    document_delete_error_code='', document_delete_error='', updated_at=?
                WHERE article_key=? AND status='notified' AND document_id!=''
                    AND document_deleted_at IS NULL
                    AND document_retention_state='active'
                """,
                (now, now, article_key),
            )
            return cursor.rowcount == 1

    def mark_document_deleted(self, article_key: str, now: float) -> None:
        self._update(
            """
            UPDATE articles SET document_retention_state='deleted',
                document_deleted_at=?, document_delete_next_attempt_at=0,
                document_delete_error_code='', document_delete_error='', updated_at=?
            WHERE article_key=? AND status='notified'
                AND document_retention_state='delete_pending'
            """,
            (now, now, article_key),
        )

    def document_delete_retry_delay(
        self, article_key: str, base: int, maximum: int
    ) -> int:
        row = self._conn.execute(
            "SELECT document_delete_retry_count FROM articles WHERE article_key=?",
            (article_key,),
        ).fetchone()
        failures = int(row[0] or 0) if row else 0
        return min(maximum, base * (2 ** min(failures, 8)))

    def schedule_document_delete_retry(
        self,
        article_key: str,
        code: str,
        message: str,
        now: float,
        delay: float,
    ) -> None:
        """Keep the durable intent; replaying DELETE is state-idempotent."""
        self._update(
            """
            UPDATE articles SET document_delete_retry_count=document_delete_retry_count+1,
                document_delete_next_attempt_at=?, document_delete_error_code=?,
                document_delete_error=?, updated_at=?
            WHERE article_key=? AND status='notified'
                AND document_retention_state='delete_pending'
                AND document_deleted_at IS NULL
            """,
            (now + delay, code[:100], message[:500], now, article_key),
        )

    def block_document_delete(
        self, article_key: str, code: str, message: str, now: float
    ) -> None:
        """Fail closed on local identity/integrity violations without an API call."""
        self._update(
            """
            UPDATE articles SET document_retention_state='blocked',
                document_delete_error_code=?, document_delete_error=?, updated_at=?
            WHERE article_key=? AND status='notified'
                AND document_retention_state IN ('active', 'delete_pending')
                AND document_deleted_at IS NULL
            """,
            (code[:100], message[:500], now, article_key),
        )

    def status_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS count FROM articles "
            "WHERE publisher=? GROUP BY status",
            (self.publisher,),
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def activate_circuit(self, kind: str, now: float, duration: float) -> None:
        key = self._meta_key("circuit")
        current = self.circuit()
        until_at = max(
            float(current["until_at"]) if current else 0.0,
            now + duration,
        )
        value = json.dumps(
            {
                "kind": kind[:100],
                "until_at": until_at,
                "activated_at": now,
            },
            separators=(",", ":"),
        )
        self._update(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def circuit(self) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (self._meta_key("circuit"),)
        ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(str(row["value"]))
            if not isinstance(value, dict):
                return None
            return {
                "kind": str(value.get("kind") or ""),
                "until_at": float(value["until_at"]),
                "activated_at": float(value["activated_at"]),
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def circuit_blocked(self, now: float) -> bool:
        state = self.circuit()
        return bool(state and float(state["until_at"]) > now)

    def clear_circuit(self) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM meta WHERE key=?", (self._meta_key("circuit"),)
            )

    def activate_alert(self, kind: str, now: float, cooldown: float) -> bool:
        scoped_kind = self._alert_key(kind)
        row = self._conn.execute(
            "SELECT * FROM alerts WHERE kind=?", (scoped_kind,)
        ).fetchone()
        with self._conn:
            if row is None:
                self._conn.execute(
                    "INSERT INTO alerts(kind, active, started_at) VALUES (?, 1, ?)",
                    (scoped_kind, now),
                )
                return True
            if not row["active"]:
                self._conn.execute(
                    "UPDATE alerts SET active=1, started_at=?, recovered_at=NULL WHERE kind=?",
                    (now, scoped_kind),
                )
            last_sent = row["last_sent_at"]
            return last_sent is None or float(last_sent) + cooldown <= now

    def mark_alert_sent(self, kind: str, now: float) -> None:
        self._update(
            "UPDATE alerts SET last_sent_at=? WHERE kind=? AND active=1",
            (now, self._alert_key(kind)),
        )

    def active_alerts(self) -> list[dict]:
        prefix = f"{self.publisher}:"
        rows = self._conn.execute(
            "SELECT * FROM alerts WHERE active=1 AND substr(kind, 1, ?)=? "
            "ORDER BY started_at",
            (len(prefix), prefix),
        ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["kind"] = str(value["kind"])[len(prefix) :]
            values.append(value)
        return values

    def mark_alert_recovered(self, kind: str, now: float) -> None:
        self._update(
            "UPDATE alerts SET active=0, recovered_at=? WHERE kind=? AND active=1",
            (now, self._alert_key(kind)),
        )

    def _meta_key(self, kind: str) -> str:
        return f"{kind}:{self.publisher}"

    def _alert_key(self, kind: str) -> str:
        prefix = f"{self.publisher}:"
        return prefix + str(kind)[: 100 - len(prefix)]

    def _update(self, sql: str, params: tuple) -> None:
        with self._conn:
            self._conn.execute(sql, params)


def _decode_image_states(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
