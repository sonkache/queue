from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from time import time


MAX_ACTIVE = 10


@dataclass(frozen=True)
class Queue:
    id: int
    chat_id: int
    thread_id: int | None
    message_id: int | None
    title: str
    opens_at: int
    closes_at: int
    closed_early: bool
    revision: int
    reply_to_message_id: int | None = None

    def status(self, now: float) -> str:
        if self.closed_early or now >= self.closes_at:
            return "closed"
        return "waiting" if now < self.opens_at else "open"

    def next_transition(self, now: float) -> int | None:
        status = self.status(now)
        if status == "waiting":
            return self.opens_at
        return self.closes_at if status == "open" else None


class QueueStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS queues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    message_id INTEGER,
                    title TEXT NOT NULL,
                    opens_at INTEGER NOT NULL,
                    closes_at INTEGER NOT NULL CHECK (closes_at > opens_at),
                    closed_early INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 0,
                    sync_at REAL,
                    reply_to_message_id INTEGER
                );
                CREATE INDEX IF NOT EXISTS active_queues
                    ON queues(chat_id, closed_early, closes_at);
                CREATE INDEX IF NOT EXISTS due_cards ON queues(sync_at);
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_id INTEGER NOT NULL REFERENCES queues(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    UNIQUE (queue_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS entry_order ON entries(queue_id, id);
            """)
        with self.transaction() as db:
            if "reply_to_message_id" not in {row["name"] for row in db.execute("PRAGMA table_info(queues)")}:
                db.execute("ALTER TABLE queues ADD COLUMN reply_to_message_id INTEGER")
            if db.execute("PRAGMA user_version").fetchone()[0] < 3:
                # Refresh published cards to apply the current layout.
                db.execute("UPDATE queues SET sync_at = ? WHERE message_id IS NOT NULL", (time(),))
                db.execute("PRAGMA user_version = 3")

    def connect(self):
        db = sqlite3.connect(self.path, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    @contextmanager
    def transaction(self):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def queue_from(row: sqlite3.Row) -> Queue:
        return Queue(**{key: row[key] for key in Queue.__dataclass_fields__})

    def create(self, chat_id: int, thread_id: int | None, title: str,
               opens_at: int, closes_at: int, *, reply_to_message_id: int | None = None) -> int | None:
        with self.transaction() as db:
            now = time()
            active = db.execute("""
                SELECT count(*) FROM queues
                WHERE chat_id = ? AND closed_early = 0 AND closes_at > ?
            """, (chat_id, now)).fetchone()[0]
            if active >= MAX_ACTIVE:
                return None
            result = db.execute("""
                INSERT INTO queues (chat_id, thread_id, title, opens_at, closes_at, sync_at, reply_to_message_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (chat_id, thread_id, title, opens_at, closes_at, now, reply_to_message_id))
            return result.lastrowid

    def active(self, chat_id: int) -> list[Queue]:
        with closing(self.connect()) as db:
            rows = db.execute("""
                SELECT * FROM queues
                WHERE chat_id = ? AND closed_early = 0 AND closes_at > ?
                ORDER BY opens_at, id
            """, (chat_id, time()))
            return [self.queue_from(row) for row in rows]

    def close(self, queue_id: int, chat_id: int) -> bool:
        with self.transaction() as db:
            now = time()
            result = db.execute("""
                UPDATE queues SET closed_early = 1, sync_at = ?, revision = revision + 1
                WHERE id = ? AND chat_id = ? AND closed_early = 0 AND closes_at > ?
            """, (now, queue_id, chat_id, now))
            return result.rowcount == 1

    def repost(self, chat_id: int, message_id: int) -> bool:
        with self.transaction() as db:
            result = db.execute("""
                UPDATE queues SET reply_to_message_id = message_id, message_id = NULL,
                    sync_at = ?, revision = revision + 1
                WHERE chat_id = ? AND message_id = ?
            """, (time(), chat_id, message_id))
            return result.rowcount == 1

    def card_message_id(self, queue_id: int, chat_id: int) -> int | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT message_id FROM queues WHERE id = ? AND chat_id = ?",
                             (queue_id, chat_id)).fetchone()
            return row["message_id"] if row is not None else None

    def change_entry(self, queue_id: int, chat_id: int, user_id: int,
                     name: str, action: str) -> tuple[str, int | None]:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM queues WHERE id = ? AND chat_id = ?",
                             (queue_id, chat_id)).fetchone()
            if row is None:
                return "missing", None
            now = time()
            status = self.queue_from(row).status(now)
            if action == "cancel":
                result = db.execute("DELETE FROM entries WHERE queue_id = ? AND user_id = ?",
                                    (queue_id, user_id))
                if result.rowcount == 0:
                    return "absent", None
                outcome, position = "cancelled", None
            else:
                entry = db.execute("SELECT id FROM entries WHERE queue_id = ? AND user_id = ?",
                                   (queue_id, user_id)).fetchone()
                if entry is not None:
                    return "already", self.position(db, queue_id, entry["id"])
                if status != "open":
                    return status, None
                result = db.execute("INSERT INTO entries (queue_id, user_id, name) VALUES (?, ?, ?)",
                                    (queue_id, user_id, name))
                outcome, position = "joined", self.position(db, queue_id, result.lastrowid)
            db.execute("UPDATE queues SET sync_at = ?, revision = revision + 1 WHERE id = ?",
                       (now, queue_id))
            return outcome, position

    @staticmethod
    def position(db: sqlite3.Connection, queue_id: int, entry_id: int) -> int:
        return db.execute("SELECT count(*) FROM entries WHERE queue_id = ? AND id <= ?",
                          (queue_id, entry_id)).fetchone()[0]

    def roster(self, queue_id: int, chat_id: int) -> tuple[Queue, list[str]] | None:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM queues WHERE id = ? AND chat_id = ?",
                             (queue_id, chat_id)).fetchone()
            if row is None:
                return None
            entries = db.execute("SELECT name FROM entries WHERE queue_id = ? ORDER BY id", (queue_id,))
            return self.queue_from(row), [entry["name"] for entry in entries]

    def due(self) -> list[Queue]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM queues WHERE sync_at <= ? ORDER BY sync_at LIMIT 50",
                              (time(),))
            return [self.queue_from(row) for row in rows]

    def finish_sync(self, queue: Queue, next_at: float | None, message_id: int | None = None):
        with self.transaction() as db:
            # A join or cancellation may arrive while the Telegram request is in flight.
            db.execute("""
                UPDATE queues SET message_id = COALESCE(?, message_id),
                    sync_at = CASE WHEN revision = ? THEN ? ELSE sync_at END
                WHERE id = ?
            """, (message_id, queue.revision, next_at, queue.id))
