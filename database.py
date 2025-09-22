from __future__ import annotations

import sqlite3
import threading
from typing import Optional


class Database:
    """Simple SQLite wrapper for storing user profiles and reactions."""

    def __init__(self, path: str = "bot.db") -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    display_name TEXT,
                    age INTEGER,
                    bio TEXT,
                    profile_completed INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS likes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_user_id INTEGER NOT NULL,
                    to_user_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(from_user_id, to_user_id)
                )
                """
            )
            self._conn.commit()

    def upsert_user(self, telegram_id: int, username: Optional[str]) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO users (telegram_id, username, profile_completed)
                VALUES (?, ?, 0)
                ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username
                """,
                (telegram_id, username),
            )
            self._conn.commit()

    def set_profile(self, telegram_id: int, display_name: str, age: int, bio: str, username: Optional[str]) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                UPDATE users
                SET display_name = ?,
                    age = ?,
                    bio = ?,
                    username = ?,
                    profile_completed = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE telegram_id = ?
                """,
                (display_name, age, bio, username, telegram_id),
            )
            self._conn.commit()

    def reset_profile(self, telegram_id: int) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                UPDATE users
                   SET display_name = NULL,
                       age = NULL,
                       bio = NULL,
                       profile_completed = 0,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE telegram_id = ?
                """,
                (telegram_id,),
            )
            cur.execute("DELETE FROM likes WHERE from_user_id = ?", (telegram_id,))
            self._conn.commit()

    def get_user(self, telegram_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            return cur.fetchone()

    def get_next_profile(self, telegram_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT u.*
                  FROM users u
                 WHERE u.telegram_id != ?
                   AND u.profile_completed = 1
                   AND u.telegram_id NOT IN (
                        SELECT to_user_id FROM likes WHERE from_user_id = ?
                   )
                 ORDER BY u.updated_at DESC
                 LIMIT 1
                """,
                (telegram_id, telegram_id),
            )
            return cur.fetchone()

    def record_reaction(self, from_user_id: int, to_user_id: int, status: str) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO likes (from_user_id, to_user_id, status)
                VALUES (?, ?, ?)
                ON CONFLICT(from_user_id, to_user_id) DO UPDATE SET status=excluded.status,
                    created_at = CURRENT_TIMESTAMP
                """,
                (from_user_id, to_user_id, status),
            )
            self._conn.commit()

    def has_mutual_like(self, user_a: int, user_b: int) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT 1
                  FROM likes l1
                  JOIN likes l2
                    ON l1.from_user_id = ? AND l1.to_user_id = ?
                   AND l2.from_user_id = ? AND l2.to_user_id = ?
                 WHERE l1.status = 'like' AND l2.status = 'like'
                """,
                (user_a, user_b, user_b, user_a),
            )
            return cur.fetchone() is not None

    def get_profile_text(self, telegram_id: int) -> Optional[str]:
        user = self.get_user(telegram_id)
        if not user or not user["profile_completed"]:
            return None
        username = user["username"]
        contact_link = f"https://t.me/{username}" if username else f"tg://user?id={telegram_id}"
        return (
            f"{user['display_name']}, {user['age']}\n"
            f"Описание: {user['bio']}\n"
            f"Контакт: {contact_link}"
        )

