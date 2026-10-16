"""复读链。规则以群规「赌局：复读列车」为准，细节经苍崎确认（见计划 4.6）：
- 同一句话累计 4 条（原句 + 3 次复读）时链生效；生效前被打断不处理。
- 生效后被打断：打断者参与过复读则记打断者，否则记倒数第三位复读者。
  发原句的人不算参与复读，除非他之后也跟着复读过。
- 生效后有人第二次复读：立即记他一团，链中止。原句发送者的第一次复读不算重复。
- 只有纯文字能开链、续链；图片、表情、指令等只会打断。撤回不处理。
状态只在内存里，重启后清空。"""
from dataclasses import dataclass, field

EFFECTIVE = 4  # 原句 + 复读的总条数达到这个数时生效


@dataclass
class Chain:
    text: str
    origin: int
    repeaters: list[int] = field(default_factory=list)  # 原句之后每次复读的人，按顺序

    @property
    def total(self) -> int:
        return 1 + len(self.repeaters)

    @property
    def effective(self) -> bool:
        return self.total >= EFFECTIVE


@dataclass
class Settlement:
    text: str
    count: int    # 结算时链上的总条数
    target: int   # 被记团的人
    reason: str   # 'breaker' 参与者亲手打断 / 'third' 倒数第三位 / 'dup' 重复复读


class RepeatTracker:
    def __init__(self):
        self.chains: dict[int, Chain] = {}

    def feed(self, group_id: int, user_id: int, text: str | None) -> Settlement | None:
        """喂一条消息。text 为 None 表示非纯文字或指令：不能开链续链，但会打断。"""
        chain = self.chains.get(group_id)
        if chain is not None and text is not None and text == chain.text:
            if chain.effective and user_id in chain.repeaters:
                # 重复复读：记他，链中止；这条消息作为新链的原句
                self.chains[group_id] = Chain(text, user_id)
                return Settlement(chain.text, chain.total + 1, user_id, "dup")
            chain.repeaters.append(user_id)
            return None

        result = None
        if chain is not None and chain.effective:
            if user_id in chain.repeaters:
                result = Settlement(chain.text, chain.total, user_id, "breaker")
            else:
                result = Settlement(chain.text, chain.total, chain.repeaters[-3], "third")
        if text is None:
            self.chains.pop(group_id, None)
        else:
            self.chains[group_id] = Chain(text, user_id)
        return result
