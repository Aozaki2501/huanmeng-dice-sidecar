"""俄罗斯轮盘，行为与《记团.js》一致：
每群一局，满 6 人开局；开局后各自 `.轮盘 roll` 掷 d100，点大者先开枪，点数相同者重掷；
普通轮盘装 1 颗子弹，中弹后结束；高加索轮盘装 5 颗子弹，六人各开一枪，中弹者各记一团。
开局后超过设定分钟无人操作则作废（报名阶段不计时）。对局只存在内存里。

这里只管对局状态，返回要发送的文案键和变量，记团和发送由 bot 负责。"""
import random
import time
from dataclasses import dataclass, field

MAX_PLAYERS = 6


@dataclass
class Player:
    id: int
    name: str
    roll: int = 0


@dataclass
class Game:
    players: list[Player] = field(default_factory=list)
    started: bool = False
    order: list[Player] | None = None
    caucasus: bool = False
    bullets: set[int] = field(default_factory=set)
    turn: int = 0
    updated: float = field(default_factory=time.monotonic)

    def find(self, uid: int) -> Player | None:
        return next((p for p in self.players if p.id == uid), None)

    def pending(self) -> str:
        return "、".join(p.name for p in self.players if not p.roll)

    def order_text(self) -> str:
        return "\n".join(f"{i}. {p.name}（d100={p.roll}）" for i, p in enumerate(self.order, 1))


@dataclass
class Outcome:
    key: str
    values: dict = field(default_factory=dict)
    shot: Player | None = None  # 中弹者，由 bot 记团并发公告


class Roulette:
    def __init__(self, rng: random.Random | None = None):
        self.games: dict[int, Game] = {}
        self.rng = rng or random.Random()

    def get(self, group_id: int, timeout_minutes: int) -> Game | None:
        game = self.games.get(group_id)
        if game and game.started and time.monotonic() - game.updated > timeout_minutes * 60:
            del self.games[group_id]
            return None
        return game

    def _touch(self, group_id: int, game: Game) -> None:
        game.updated = time.monotonic()
        self.games[group_id] = game

    def status(self, group_id: int, timeout: int) -> Outcome:
        game = self.get(group_id, timeout)
        if not game:
            return Outcome("rr.status_none")
        if not game.started:
            return Outcome("rr.status_signup", {"count": len(game.players), "max": MAX_PLAYERS,
                                                "players": "、".join(p.name for p in game.players)})
        if not game.order:
            rolled = "、".join(f"{p.name}({p.roll})" for p in game.players if p.roll) or "无"
            return Outcome("rr.status_rolling", {"rolled": rolled, "pending": game.pending()})
        return Outcome("rr.status_playing", {"shots": game.turn, "left": MAX_PLAYERS - game.turn,
                                             "order": game.order_text(), "current": game.order[game.turn].name})

    def join(self, group_id: int, uid: int, name: str, timeout: int, *, caucasus: bool = False) -> Outcome:
        game = self.get(group_id, timeout)
        if game and game.caucasus != caucasus:
            return Outcome("rr.mode_conflict", {"mode": "高加索轮盘" if game.caucasus else "俄罗斯轮盘"})
        if game and game.started:
            return Outcome("rr.join_started")
        game = game or Game(caucasus=caucasus)
        if game.find(uid):
            return Outcome("rr.join_dup", {"name": name, "count": len(game.players), "max": MAX_PLAYERS})
        game.players.append(Player(uid, name))
        if len(game.players) < MAX_PLAYERS:
            self._touch(group_id, game)
            return Outcome("rr.join", {"name": name, "count": len(game.players), "max": MAX_PLAYERS})
        game.started = True
        self._touch(group_id, game)
        return Outcome("rr.join_full", {"name": name, "max": MAX_PLAYERS, "pending": game.pending()})

    def quit(self, group_id: int, uid: int, name: str, timeout: int) -> Outcome:
        game = self.get(group_id, timeout)
        if not game or not game.find(uid):
            return Outcome("rr.quit_none")
        if game.started:
            return Outcome("rr.quit_started")
        game.players = [p for p in game.players if p.id != uid]
        if game.players:
            self._touch(group_id, game)
        else:
            del self.games[group_id]
        return Outcome("rr.quit", {"name": name, "count": len(game.players), "max": MAX_PLAYERS})

    def roll(self, group_id: int, uid: int, timeout: int) -> Outcome:
        game = self.get(group_id, timeout)
        if not game or not game.started:
            return Outcome("rr.roll_not_started")
        if game.order:
            return Outcome("rr.roll_ordered")
        player = game.find(uid)
        if not player:
            return Outcome("rr.roll_not_player")
        if player.roll:
            return Outcome("rr.roll_dup", {"name": player.name, "roll": player.roll, "pending": game.pending()})

        player.roll = self.rng.randint(1, 100)
        self._touch(group_id, game)
        base = {"name": player.name, "roll": player.roll}
        if game.pending():
            return Outcome("rr.rolled", {**base, "pending": game.pending()})

        # 全员掷完，点数相同的人清空重掷
        counts: dict[int, int] = {}
        for p in game.players:
            counts[p.roll] = counts.get(p.roll, 0) + 1
        tied = [p for p in game.players if counts[p.roll] > 1]
        if tied:
            text = "、".join(f"{p.name}({p.roll})" for p in tied)
            for p in tied:
                p.roll = 0
            return Outcome("rr.tie", {**base, "tied": text})

        game.order = sorted(game.players, key=lambda p: p.roll, reverse=True)
        chamber = self.rng.randrange(MAX_PLAYERS)
        game.bullets = set(range(MAX_PLAYERS)) - {chamber} if game.caucasus else {chamber}
        game.turn = 0
        return Outcome("rr.order_ready", {**base, "order": game.order_text(), "first": game.order[0].name})

    def shoot(self, group_id: int, uid: int, timeout: int) -> Outcome:
        game = self.get(group_id, timeout)
        if not game or not game.started:
            return Outcome("rr.shoot_none")
        if not game.order:
            return Outcome("rr.shoot_need_roll", {"pending": game.pending()})
        current = game.order[game.turn]
        if current.id != uid:
            return Outcome("rr.shoot_not_turn", {"current": current.name})
        hit = game.turn in game.bullets
        if hit and not game.caucasus:
            del self.games[group_id]
            return Outcome("rr.bang", {"name": current.name}, shot=current)
        game.turn += 1
        if game.caucasus:
            values = {"name": current.name}
            if game.turn == MAX_PLAYERS:
                del self.games[group_id]
                values["survivor"] = next(p.name for i, p in enumerate(game.order) if i not in game.bullets)
                key = "cr.bang_last" if hit else "cr.click_last"
            else:
                self._touch(group_id, game)
                values.update(left=MAX_PLAYERS - game.turn, next=game.order[game.turn].name)
                key = "cr.bang" if hit else "cr.click"
            return Outcome(key, values, shot=current if hit else None)
        self._touch(group_id, game)
        return Outcome("rr.click", {"name": current.name, "left": MAX_PLAYERS - game.turn,
                                    "next": game.order[game.turn].name})

    def off(self, group_id: int, is_admin: bool, timeout: int) -> Outcome:
        if not self.get(group_id, timeout):
            return Outcome("rr.off_none")
        if not is_admin:
            return Outcome("rr.off_denied")
        del self.games[group_id]
        return Outcome("rr.off")
