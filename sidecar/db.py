"""SQLite 存储。所有数据（配置、文案、账本、人物卡索引、操作日志）都在一个文件里。
结构变更按 MIGRATIONS 顺序追加，用 PRAGMA user_version 记录已执行到第几条。"""
import json
import sqlite3
import time
from pathlib import Path

MIGRATIONS = [
    # 1: 骨架
    """
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE templates (
        key TEXT NOT NULL,
        scope TEXT NOT NULL DEFAULT '',   -- '' 为默认；按群覆盖时为群号
        variants TEXT NOT NULL,           -- JSON 字符串数组
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (key, scope)
    );
    CREATE TABLE op_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at INTEGER NOT NULL,
        operator TEXT NOT NULL,           -- 'web' 或 QQ 号
        action TEXT NOT NULL,
        before TEXT,
        after TEXT
    );
    """,
]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.execute("BEGIN")
        try:
            for stmt in sql.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            conn.execute(f"PRAGMA user_version={i}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def log_op(conn: sqlite3.Connection, operator: str, action: str, before=None, after=None) -> None:
    dump = lambda v: None if v is None else json.dumps(v, ensure_ascii=False)
    conn.execute(
        "INSERT INTO op_log (at, operator, action, before, after) VALUES (?, ?, ?, ?, ?)",
        (int(time.time()), operator, action, dump(before), dump(after)),
    )
