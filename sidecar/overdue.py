"""逾期名单：没还清团就退群的人。他们再进大群时，骰娘在大群提醒。"""
import re
import sqlite3
import time
from dataclasses import dataclass

from .db import log_op

_QQ = re.compile(r"[1-9]\d{4,11}")
_YEAR = re.compile(r"(?:19|20)\d\d")


@dataclass
class Row:
    name: str
    qq: int
    deadlines: str
    note: str = ""


def parse_paste(text: str) -> tuple[list[Row], list[str]]:
    """解析从腾讯文档表格复制来的内容：每行「昵称 ⇥ 死线 ⇥ QQ号 ⇥ 备注」，用 Tab 分隔。
    只有年份的行（如「2021」）是分组标题，下面各行的死线前面会加上这个年份。
    表头、空行跳过；QQ 号不是数字的行（比如填了邮箱）列为未导入，返回给后台提示。"""
    rows, skipped, year = [], [], ""
    for line in text.splitlines():
        cells = [c.strip() for c in line.split("\t")]
        filled = [c for c in cells if c]
        if not filled:
            continue
        if len(filled) == 1:  # 年份分组行或表格标题
            if _YEAR.fullmatch(filled[0]):
                year = filled[0]
            continue
        if len(cells) < 3 or cells[0] in ("昵称", "名字"):
            continue  # 表头、标题
        name, deadlines, qq = cells[0], cells[1].strip("()（） "), cells[2]
        note = cells[3] if len(cells) > 3 else ""
        if not name:
            continue
        if not _QQ.fullmatch(qq):
            skipped.append(f"{name}（QQ 号一栏是「{qq}」）")
            continue
        if year and deadlines:
            deadlines = f"{year} 年 {deadlines}"
        rows.append(Row(name, int(qq), deadlines, note))
    return rows, skipped


class Overdue:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def all(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM overdue ORDER BY added_at, rowid").fetchall()

    def get(self, qq: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM overdue WHERE qq = ?", (qq,)).fetchone()

    def save(self, rows: list[Row], operator: str) -> None:
        """新增或覆盖（同一个 QQ 号以这次为准）。"""
        now = int(time.time())
        self.conn.execute("BEGIN")
        for r in rows:
            self.conn.execute(
                "INSERT INTO overdue (qq, name, deadlines, note, added_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(qq) DO UPDATE SET name = excluded.name, deadlines = excluded.deadlines, note = excluded.note",
                (r.qq, r.name, r.deadlines, r.note, now))
            log_op(self.conn, operator, "overdue.save", None, r.__dict__)
        self.conn.execute("COMMIT")

    def delete(self, qq: int, operator: str) -> None:
        row = self.get(qq)
        if row is None:
            return
        self.conn.execute("BEGIN")
        self.conn.execute("DELETE FROM overdue WHERE qq = ?", (qq,))
        log_op(self.conn, operator, "overdue.delete", dict(row), None)
        self.conn.execute("COMMIT")
