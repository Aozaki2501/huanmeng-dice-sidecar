"""团账本：全局一份，所有群共用，以 QQ 号识别人。"""
import calendar
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .db import log_op

BEIJING = timezone(timedelta(hours=8))
SOURCES = {"rr": "轮盘", "repeat": "复读", "manual": "手动", "import": "导入"}


def today() -> date:
    return datetime.now(BEIJING).date()


def add_months(d: date, months: int) -> date:
    """加 N 个月；目标月份没有这一天时取该月最后一天（12/31 + 2 个月 = 2/28 或 2/29）。"""
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def md(d: date | str) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return f"{d.month}/{d.day}"


def nearest_year(month: int, day: int, ref: date) -> date:
    """只有月/日时，取离 ref 最近的那一年。"""
    candidates = []
    for y in (ref.year - 1, ref.year, ref.year + 1):
        try:
            candidates.append(date(y, month, day))
        except ValueError:  # 2/29
            pass
    if not candidates:
        raise ValueError(f"{month}/{day} 不是有效日期")
    return min(candidates, key=lambda d: abs((d - ref).days))


@dataclass
class Person:
    qq: int
    name: str
    deadlines: list[str]  # 仍欠着的死线，从早到晚

    @property
    def entry(self) -> str:
        return f"{self.name}（{'，'.join(md(d) for d in self.deadlines)}）" if self.deadlines else self.name


class Ledger:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------- 查询 ----------
    def person(self, qq: int) -> Person | None:
        row = self.conn.execute("SELECT name FROM members WHERE qq = ?", (qq,)).fetchone()
        if row is None:
            return None
        rows = self.conn.execute(
            "SELECT deadline FROM ledger WHERE qq = ? AND removed_at IS NULL ORDER BY deadline", (qq,)
        )
        return Person(qq, row["name"], [r["deadline"] for r in rows])

    def people(self) -> list[Person]:
        """所有还欠团的人，按最早的死线排序。"""
        rows = self.conn.execute(
            "SELECT m.qq, m.name, l.deadline FROM ledger l JOIN members m ON m.qq = l.qq "
            "WHERE l.removed_at IS NULL ORDER BY l.deadline, l.id"
        )
        people: dict[int, Person] = {}
        for r in rows:
            people.setdefault(r["qq"], Person(r["qq"], r["name"], [])).deadlines.append(r["deadline"])
        return list(people.values())

    def records(self, include_removed: bool = False) -> list[sqlite3.Row]:
        where = "" if include_removed else "WHERE l.removed_at IS NULL"
        return self.conn.execute(
            f"SELECT l.*, m.name FROM ledger l JOIN members m ON m.qq = l.qq {where} ORDER BY l.deadline, l.id"
        ).fetchall()

    def find(self, query: str) -> list[Person]:
        """按 QQ 号或名字找还欠团的人。名字可能本身就是数字，所以两种都试。"""
        query = query.strip()
        found = {p.qq: p for p in self.people() if p.name == query or (query.isdigit() and p.qq == int(query))}
        return list(found.values())

    # ---------- 修改 ----------
    def ensure_member(self, qq: int, name: str) -> None:
        """第一次出现的人用当时的群名片登记名字；已有的名字不覆盖，以后台修改为准。"""
        self.conn.execute("INSERT OR IGNORE INTO members (qq, name) VALUES (?, ?)", (qq, name or str(qq)))

    def add(self, qq: int, name: str, source: str, operator: str, months: int,
            recorded_on: date | None = None, deadline: date | None = None) -> tuple[Person, str]:
        """记一团，返回 (记团后的此人, 新死线)。直接指定死线时（手动补录），记团日期可以为空。"""
        if deadline is None:
            recorded_on = recorded_on or today()
            deadline = add_months(recorded_on, months)
        recorded = recorded_on.isoformat() if recorded_on else None
        self.conn.execute("BEGIN")
        self.ensure_member(qq, name)
        self.conn.execute(
            "INSERT INTO ledger (qq, recorded_on, deadline, source) VALUES (?, ?, ?, ?)",
            (qq, recorded, deadline.isoformat(), source),
        )
        log_op(self.conn, operator, "ledger.add", None,
               {"qq": qq, "recorded_on": recorded, "deadline": deadline.isoformat(), "source": source})
        self.conn.execute("COMMIT")
        return self.person(qq), deadline.isoformat()

    def remove_earliest(self, qq: int, operator: str) -> tuple[Person, str] | None:
        """消去此人死线最早的一团，返回 (消团后的此人, 被消去的死线)。"""
        before = self.person(qq)
        if before is None or not before.deadlines:
            return None
        row = self.conn.execute(
            "SELECT id, deadline FROM ledger WHERE qq = ? AND removed_at IS NULL ORDER BY deadline, id LIMIT 1", (qq,)
        ).fetchone()
        self._remove(row["id"], operator, "ledger.clear", before)
        return self.person(qq), row["deadline"]

    def delete_record(self, record_id: int, operator: str) -> None:
        row = self.conn.execute("SELECT qq FROM ledger WHERE id = ? AND removed_at IS NULL", (record_id,)).fetchone()
        if row:
            self._remove(record_id, operator, "ledger.delete", self.person(row["qq"]))

    def _remove(self, record_id: int, operator: str, action: str, before: Person) -> None:
        self.conn.execute("BEGIN")
        self.conn.execute("UPDATE ledger SET removed_at = ?, removed_by = ? WHERE id = ?",
                          (int(time.time()), operator, record_id))
        after = self.person(before.qq)
        log_op(self.conn, operator, action, {"record": record_id, "entry": before.entry}, {"entry": after.entry})
        self.conn.execute("COMMIT")

    def set_deadline(self, record_id: int, deadline: date, operator: str) -> None:
        row = self.conn.execute("SELECT qq, deadline FROM ledger WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            return
        self.conn.execute("BEGIN")
        self.conn.execute("UPDATE ledger SET deadline = ? WHERE id = ?", (deadline.isoformat(), record_id))
        log_op(self.conn, operator, "ledger.deadline", {"record": record_id, "deadline": row["deadline"]},
               {"record": record_id, "deadline": deadline.isoformat()})
        self.conn.execute("COMMIT")

    def member_name(self, qq: int) -> str | None:
        row = self.conn.execute("SELECT name FROM members WHERE qq = ?", (qq,)).fetchone()
        return row["name"] if row else None

    def rename(self, qq: int, name: str, operator: str) -> None:
        """改名单上显示的名字（圈名）。还没被记过团的人也可以先登记，以后记团就用这个名字。"""
        old = self.member_name(qq)
        if old == name:
            return
        self.conn.execute("BEGIN")
        self.conn.execute("INSERT INTO members (qq, name) VALUES (?, ?) ON CONFLICT(qq) DO UPDATE SET name = excluded.name",
                          (qq, name))
        log_op(self.conn, operator, "member.rename", None if old is None else {"qq": qq, "name": old}, {"qq": qq, "name": name})
        self.conn.execute("COMMIT")

    def import_people(self, items: list[tuple[int, str, list[date]]], operator: str) -> int:
        """批量导入现有名单：(QQ, 名字, 死线列表)。名字以导入的为准。"""
        n = 0
        self.conn.execute("BEGIN")
        for qq, name, deadlines in items:
            self.conn.execute(
                "INSERT INTO members (qq, name) VALUES (?, ?) ON CONFLICT(qq) DO UPDATE SET name = excluded.name",
                (qq, name),
            )
            for d in deadlines:
                self.conn.execute("INSERT INTO ledger (qq, recorded_on, deadline, source) VALUES (?, NULL, ?, 'import')",
                                  (qq, d.isoformat()))
                n += 1
        log_op(self.conn, operator, "ledger.import", None,
               [{"qq": qq, "name": name, "deadlines": [d.isoformat() for d in ds]} for qq, name, ds in items])
        self.conn.execute("COMMIT")
        return n


def parse_list_line(line: str, ref: date) -> tuple[str, list[date]]:
    """解析现行名单的一行，如 `Flame（11/3，11/30）`。"""
    line = line.strip()
    for left, right in (("（", "）"), ("(", ")")):
        if line.endswith(right) and left in line:
            name, _, inner = line[:-1].rpartition(left)
            break
    else:
        raise ValueError(f"看不懂这一行：{line}")
    name = name.strip()
    if not name:
        raise ValueError(f"这一行没有名字：{line}")
    deadlines = []
    for part in inner.replace(",", "，").replace("、", "，").split("，"):
        part = part.strip()
        if not part:
            continue
        try:
            m, d = (int(x) for x in part.split("/"))
            deadlines.append(nearest_year(m, d, ref))
        except ValueError:
            raise ValueError(f"「{line}」里的日期「{part}」看不懂，应写成 月/日") from None
    if not deadlines:
        raise ValueError(f"这一行没有死线：{line}")
    return name, sorted(deadlines)
