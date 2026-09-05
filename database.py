import os
import sqlite3
from pathlib import Path
from typing import Optional, List, Tuple


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        # Ensure parent directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    vk_post_id INTEGER PRIMARY KEY,
                    tg_post_msg_id INTEGER NOT NULL,
                    post_date INTEGER,
                    comments_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Inspect columns for backward compatibility / migrations
            cursor = conn.execute("PRAGMA table_info(posts);")
            columns = [row["name"] for row in cursor.fetchall()]

            if "tg_post_msg_id" not in columns:
                conn.execute("ALTER TABLE posts ADD COLUMN tg_post_msg_id INTEGER;")

            if "comments_count" not in columns:
                conn.execute("ALTER TABLE posts ADD COLUMN comments_count INTEGER DEFAULT 0;")

            # Migrate legacy tg_channel_msg_id / tg_discussion_msg_id if present
            if "tg_channel_msg_id" in columns:
                conn.execute("""
                    UPDATE posts 
                    SET tg_post_msg_id = COALESCE(tg_post_msg_id, tg_channel_msg_id, tg_discussion_msg_id)
                    WHERE tg_post_msg_id IS NULL;
                """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS comments (
                    vk_comment_id INTEGER PRIMARY KEY,
                    vk_post_id INTEGER NOT NULL,
                    tg_comment_msg_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (vk_post_id) REFERENCES posts (vk_post_id)
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_posts_tg_msg 
                ON posts (tg_post_msg_id);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_comments_post 
                ON comments (vk_post_id);
            """)
            conn.commit()

    def is_empty(self) -> bool:
        """Проверяет, пустая ли база постов (первый запуск)."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) as cnt FROM posts;")
            row = cur.fetchone()
            return row["cnt"] == 0

    def post_exists(self, vk_post_id: int) -> bool:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT 1 FROM posts WHERE vk_post_id = ?;", (vk_post_id,))
            return cur.fetchone() is not None

    def save_post(self, vk_post_id: int, tg_post_msg_id: int, post_date: int = 0, comments_count: int = 0) -> None:
        with self._get_connection() as conn:
            cursor = conn.execute("PRAGMA table_info(posts);")
            columns = [row["name"] for row in cursor.fetchall()]
            if "tg_channel_msg_id" in columns:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO posts (vk_post_id, tg_post_msg_id, tg_channel_msg_id, post_date, comments_count)
                    VALUES (?, ?, ?, ?, ?);
                    """,
                    (vk_post_id, tg_post_msg_id, tg_post_msg_id, post_date, comments_count)
                )
            else:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO posts (vk_post_id, tg_post_msg_id, post_date, comments_count)
                    VALUES (?, ?, ?, ?);
                    """,
                    (vk_post_id, tg_post_msg_id, post_date, comments_count)
                )
            conn.commit()

    def update_comments_count(self, vk_post_id: int, comments_count: int) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE posts SET comments_count = ? WHERE vk_post_id = ?;",
                (comments_count, vk_post_id)
            )
            conn.commit()

    def get_stored_comments_count(self, vk_post_id: int) -> int:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT comments_count FROM posts WHERE vk_post_id = ?;", (vk_post_id,))
            row = cur.fetchone()
            return row["comments_count"] if row and row["comments_count"] is not None else 0

    def get_synced_comments_count(self, vk_post_id: int) -> int:
        """Возвращает количество реально пересланных в Telegram комментариев к посту."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) as cnt FROM comments WHERE vk_post_id = ?;", (vk_post_id,))
            row = cur.fetchone()
            return row["cnt"] if row else 0

    def clean_deleted_comments(self, vk_post_id: int, active_comment_ids: List[int]) -> None:
        """Удаляет из базы комментарии, которые были удалены в ВК."""
        with self._get_connection() as conn:
            if active_comment_ids:
                placeholders = ",".join("?" for _ in active_comment_ids)
                conn.execute(
                    f"DELETE FROM comments WHERE vk_post_id = ? AND vk_comment_id NOT IN ({placeholders});",
                    [vk_post_id, *active_comment_ids]
                )
            else:
                conn.execute("DELETE FROM comments WHERE vk_post_id = ?;", (vk_post_id,))
            conn.commit()

    def get_post(self, vk_post_id: int) -> Optional[dict]:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM posts WHERE vk_post_id = ?;", 
                (vk_post_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            data = dict(row)
            if not data.get("tg_post_msg_id"):
                data["tg_post_msg_id"] = data.get("tg_channel_msg_id") or data.get("tg_discussion_msg_id")
            return data

    def get_recent_posts(self, limit: int = 20) -> List[dict]:
        """Возвращает недавние посты для проверки комментариев."""
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT * FROM posts 
                ORDER BY vk_post_id DESC 
                LIMIT ?;
                """,
                (limit,)
            )
            posts = []
            for row in cur.fetchall():
                data = dict(row)
                if not data.get("tg_post_msg_id"):
                    data["tg_post_msg_id"] = data.get("tg_channel_msg_id") or data.get("tg_discussion_msg_id")
                posts.append(data)
            return posts

    def comment_exists(self, vk_comment_id: int) -> bool:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT 1 FROM comments WHERE vk_comment_id = ?;", (vk_comment_id,))
            return cur.fetchone() is not None

    def save_comment(self, vk_comment_id: int, vk_post_id: int, tg_comment_msg_id: Optional[int] = None) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO comments (vk_comment_id, vk_post_id, tg_comment_msg_id)
                VALUES (?, ?, ?);
                """,
                (vk_comment_id, vk_post_id, tg_comment_msg_id)
            )
            conn.commit()
