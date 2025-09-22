from __future__ import annotations

import json
import sqlite3
import threading
from typing import Optional


GENDER_LABELS = {
    "male": "Парень",
    "female": "Девушка",
}

PREFERRED_LABELS = {
    "male": "друга",
    "female": "подругу",
}


def _gender_to_text(code: Optional[str]) -> Optional[str]:
    return GENDER_LABELS.get(code)


def _preferred_to_text(code: Optional[str]) -> Optional[str]:
    label = PREFERRED_LABELS.get(code)
    if not label:
        return None
    return label


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
                    phone TEXT,
                    display_name TEXT,
                    age INTEGER,
                    gender TEXT,
                    preferred_gender TEXT,
                    bio TEXT,
                    photo_urls TEXT,
                    photo_file_id TEXT,
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

            # Backfill optional columns for databases created before these features shipped.
            for column in [
                "photo_file_id TEXT",
                "phone TEXT",
                "gender TEXT",
                "preferred_gender TEXT",
                "photo_urls TEXT",
            ]:
                try:
                    cur.execute(f"ALTER TABLE users ADD COLUMN {column}")
                    self._conn.commit()
                except sqlite3.OperationalError:
                    pass

    def upsert_user(
        self, telegram_id: int, username: Optional[str], phone: Optional[str] = None
    ) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO users (telegram_id, username, phone, profile_completed)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = excluded.username,
                    phone = COALESCE(excluded.phone, users.phone)
                """,
                (telegram_id, username, phone),
            )
            self._conn.commit()

    def set_profile(
        self,
        telegram_id: int,
        display_name: str,
        age: int,
        bio: str,
        gender: Optional[str],
        preferred_gender: Optional[str],
        photo_refs: Optional[list[dict]],
        username: Optional[str],
    ) -> None:
        normalized_refs = [self._normalize_photo_ref(ref) for ref in (photo_refs or [])]

        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                UPDATE users
                SET display_name = ?,
                    age = ?,
                    bio = ?,
                    gender = ?,
                    preferred_gender = ?,
                    photo_urls = ?,
                    photo_file_id = NULL,
                    username = ?,
                    profile_completed = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE telegram_id = ?
                """,
                (
                    display_name,
                    age,
                    bio,
                    gender,
                    preferred_gender,
                    self._serialize_photo_urls(normalized_refs),
                    username,
                    telegram_id,
                ),
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
                       gender = NULL,
                       preferred_gender = NULL,
                       photo_file_id = NULL,
                       photo_urls = NULL,
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

    def get_next_profile(
        self,
        telegram_id: int,
        viewer_gender: Optional[str],
        viewer_preference: Optional[str],
    ) -> Optional[sqlite3.Row]:
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
                """,
                (telegram_id, telegram_id),
            )
            for row in cur.fetchall():
                if viewer_preference and row["gender"] and row["gender"] != viewer_preference:
                    continue
                if row["preferred_gender"] and viewer_gender and row["preferred_gender"] != viewer_gender:
                    continue
                return row
            return None

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
        bio = user["bio"] or "Описание отсутствует"
        phone = user["phone"]
        phone_line = f"Телефон: {phone}\n" if phone else ""
        gender = user["gender"]
        preferred = user["preferred_gender"]
        gender_text = _gender_to_text(gender)
        preferred_text = _preferred_to_text(preferred)
        gender_line = f"Пол: {gender_text}\n" if gender_text else ""
        preferred_line = f"Ищет: {preferred_text}\n" if preferred_text else ""
        photo_refs = self._deserialize_photo_urls(user["photo_urls"])
        if not photo_refs and user["photo_file_id"]:
            photo_refs = [{"file_id": user["photo_file_id"], "url": None, "type": "photo"}]
        photo_line = f"Фото: {len(photo_refs)} шт.\n" if photo_refs else ""
        return (
            f"{user['display_name']}, {user['age']}\n"
            f"{gender_line}"
            f"{preferred_line}"
            f"{photo_line}"
            f"Описание: {bio}\n"
            f"{phone_line}"
        ).rstrip()

    def build_contact_line(self, user: sqlite3.Row) -> str:
        display = user["display_name"] or "Собеседник"
        username = user["username"]
        if username:
            return f"[{display}](https://t.me/{username})"
        return f"[{display}](tg://user?id={user['telegram_id']})"

    def list_user_ids(self) -> list[int]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT telegram_id FROM users WHERE profile_completed = 1")
            return [row[0] for row in cur.fetchall()]

    def update_photo_refs(
        self, telegram_id: int, photo_refs: list[dict[str, Optional[str]]]
    ) -> None:
        normalized = [self._normalize_photo_ref(ref if isinstance(ref, dict) else {"file_id": ref, "url": None}) for ref in photo_refs]
        serialized = self._serialize_photo_urls(normalized)
        primary_file_id = None
        for ref in normalized:
            if ref.get("file_id"):
                primary_file_id = ref["file_id"]
                break
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                UPDATE users
                   SET photo_urls = ?,
                       photo_file_id = COALESCE(?, photo_file_id)
                 WHERE telegram_id = ?
                """,
                (serialized, primary_file_id, telegram_id),
            )
            self._conn.commit()

    def delete_user(self, telegram_id: int) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM likes WHERE from_user_id = ? OR to_user_id = ?", (telegram_id, telegram_id))
            cur.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
            self._conn.commit()

    @staticmethod
    def _normalize_photo_ref(self, ref: dict) -> dict:
        if not isinstance(ref, dict):
            return {"file_id": ref, "url": None, "type": "photo"}
        return {
            "file_id": ref.get("file_id"),
            "url": ref.get("url"),
            "type": ref.get("type") or "photo",
        }

    def _serialize_photo_urls(self, photo_refs: Optional[list[dict]]) -> Optional[str]:
        if not photo_refs:
            return None
        normalized = [self._normalize_photo_ref(ref) for ref in photo_refs]
        return json.dumps(normalized, ensure_ascii=False)

    @staticmethod
    def _deserialize_photo_urls(value: Optional[str]) -> list[dict]:
        if not value:
            return []
        try:
            parsed = json.loads(value)
            result: list[dict] = []
            for item in parsed:
                if isinstance(item, str):
                    result.append({"file_id": None, "url": item, "type": "photo"})
                elif isinstance(item, dict):
                    result.append(
                        {
                            "file_id": item.get("file_id"),
                            "url": item.get("url"),
                            "type": item.get("type") or "photo",
                        }
                    )
            return result
        except (json.JSONDecodeError, TypeError):
            return []

    def extract_photo_refs(self, row: Optional[sqlite3.Row]) -> list[dict]:
        if not row:
            return []
        refs = []
        try:
            refs = self._deserialize_photo_urls(row["photo_urls"])
        except KeyError:
            refs = []
        if refs:
            return refs
        try:
            legacy = row["photo_file_id"]
        except (KeyError, IndexError):
            legacy = None
        return ([{"file_id": legacy, "url": None, "type": "photo"}] if legacy else [])
