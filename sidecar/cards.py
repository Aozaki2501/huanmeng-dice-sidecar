"""人物卡：图片存在 data/cards/<QQ号>/ 下，索引在 SQLite 的 cards 表。
同一个人存同名卡时，新图是当前版本，旧图保留；删卡是软删除，文件移到 data/cards/_deleted/。"""
import re
import sqlite3
import time
from pathlib import Path

from .db import log_op

_UNSAFE = re.compile(r'[\\/:*?"<>|\s]+')


def image_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.startswith(b"GIF8"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpg"


class Cards:
    def __init__(self, conn: sqlite3.Connection, root: Path):
        self.conn = conn
        self.root = root

    def file(self, row) -> Path:
        return self.root / row["path"]

    def save(self, qq: int, name: str, data: bytes) -> int:
        ts = int(time.time())
        stem = f"{time.strftime('%Y%m%d-%H%M%S')}_{_UNSAFE.sub('_', name)[:40]}"
        rel = Path(str(qq)) / f"{stem}.{image_ext(data)}"
        n = 1
        while (self.root / rel).exists() or (self.root / "_deleted" / rel).exists():  # 同一秒存了同名卡
            n += 1
            rel = Path(str(qq)) / f"{stem}_{n}.{image_ext(data)}"
        (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.root / rel).write_bytes(data)
        self.conn.execute("BEGIN")
        cur = self.conn.execute("INSERT INTO cards (qq, name, path, created_at) VALUES (?, ?, ?, ?)",
                                (qq, name, rel.as_posix(), ts))
        log_op(self.conn, str(qq), "card.save", None, {"qq": qq, "name": name, "path": rel.as_posix()})
        self.conn.execute("COMMIT")
        return cur.lastrowid

    def current(self, qq: int) -> list[sqlite3.Row]:
        """每个卡名的最新一版，按卡名第一次存的先后排序，序号就是列表里的位置。"""
        return self.conn.execute(
            """SELECT c.* FROM cards c
               JOIN (SELECT name, MIN(id) AS first, MAX(id) AS last FROM cards
                     WHERE qq = ? AND deleted_at IS NULL GROUP BY name) g ON c.id = g.last
               ORDER BY g.first""", (qq,)).fetchall()

    def find(self, qq: int, query: str) -> sqlite3.Row | None:
        cards = self.current(qq)
        for c in cards:
            if c["name"] == query:
                return c
        if query.isdigit() and 1 <= int(query) <= len(cards):
            return cards[int(query) - 1]
        return None

    def delete(self, qq: int, name: str, operator: str) -> None:
        """删掉这个卡名的所有版本，否则旧版本会顶上来。"""
        rows = self.conn.execute("SELECT * FROM cards WHERE qq = ? AND name = ? AND deleted_at IS NULL", (qq, name)).fetchall()
        self._move(rows, operator, deleted=True)

    def delete_id(self, card_id: int, operator: str) -> None:
        self._move(self.conn.execute("SELECT * FROM cards WHERE id = ? AND deleted_at IS NULL", (card_id,)).fetchall(),
                   operator, deleted=True)

    def restore_id(self, card_id: int, operator: str) -> None:
        self._move(self.conn.execute("SELECT * FROM cards WHERE id = ? AND deleted_at IS NOT NULL", (card_id,)).fetchall(),
                   operator, deleted=False)

    def _move(self, rows, operator: str, deleted: bool) -> None:
        if not rows:
            return
        self.conn.execute("BEGIN")
        for r in rows:
            old = Path(r["path"])
            new = Path("_deleted") / old if deleted else Path(*old.parts[1:])
            if (self.root / old).exists():
                (self.root / new).parent.mkdir(parents=True, exist_ok=True)
                (self.root / old).replace(self.root / new)
            self.conn.execute("UPDATE cards SET path = ?, deleted_at = ?, deleted_by = ? WHERE id = ?",
                              (new.as_posix(), int(time.time()) if deleted else None, operator if deleted else None, r["id"]))
            log_op(self.conn, operator, "card.delete" if deleted else "card.restore",
                   {"id": r["id"], "path": old.as_posix()}, {"id": r["id"], "path": new.as_posix()})
        self.conn.execute("COMMIT")

    def all(self) -> list[sqlite3.Row]:
        """后台用：所有卡（含已删除），按 QQ 号、卡名、时间排列。"""
        return self.conn.execute(
            "SELECT c.*, m.name AS member FROM cards c LEFT JOIN members m ON m.qq = c.qq "
            "ORDER BY c.qq, c.name, c.id DESC").fetchall()

    def get(self, card_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
