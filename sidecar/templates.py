"""文案模板：目录、校验、渲染和存储。
所有对群友可见的文字都从这里取，代码里不写死文案。新增文案时在 CATALOG 里加一条即可，
后台页面会自动列出来。"""
import json
import random
import sqlite3
import string
import time
from dataclasses import dataclass, field

from .db import log_op


@dataclass(frozen=True)
class TemplateDef:
    key: str
    group: str
    title: str
    description: str
    variables: dict[str, str]  # 变量名 -> 说明
    default: list[str]
    per_group: bool = False  # 是否允许按群单独设置
    samples: dict[str, str] = field(default_factory=dict)  # 预览用示例值


# 变量的预览示例值，模板里没单独指定时用这里的
SAMPLES = {
    "at": "@示例群友",
    "nickname": "示例群友",
    "group_name": "未可知的幻梦境",
    "name": "Flame",
    "left": "4",
    "next": "巧克力",
    "deadline": "12/17",
    "entry": "Flame（11/3，11/30）",
    "text": "草",
    "count": "4",
    "reason": "复读链倒数第三",
    "timeout": "15",
    "help": "（轮盘帮助）",
    "max": "6",
    "players": "Flame、巧克力、km",
    "rolled": "Flame(88)、巧克力(42)",
    "pending": "km、阿青",
    "shots": "2",
    "order": "1. Flame（d100=88）\n2. 巧克力（d100=42）",
    "current": "km",
    "first": "Flame",
    "roll": "88",
    "tied": "km(42)、阿青(42)",
    "query": "Flame",
    "candidates": "Flame（11/3）  QQ 12345678\nFlame（12/1）  QQ 87654321",
    "mode": "高加索轮盘",
    "survivor": "巧克力",
}

GROUPS = ["入群与拉群", "俄罗斯轮盘", "高加索轮盘", "复读链", "团账本", "人物卡"]

CATALOG = [
    TemplateDef(
        "welcome.default", "入群与拉群", "入群欢迎",
        "新成员进大群时发送。其他群默认不发；要发的话在下面为那个群单独设置。",
        {"at": "@ 新成员", "nickname": "新成员昵称", "group_name": "群名"},
        ["{at}\n欢迎新佬，做个自我介绍吧~\n"
         "1）称呼是什么呢？\n"
         "2）有跑团经验吗？喜欢什么风格的团？平时的跑团时间？\n"
         "3）如果是纯萌新，请先读完调查员手册与规则书的前四章。\n"
         "4）所有新入群的成员需要以截图，截图，截图（重要的事情说三遍）的方式将一张人物卡私聊发给任意管理，发过卡才能跑团。"
         "同时请避免人物卡剧透。这是为了看看你的相性与群理念是否相符，对规则书的理解程度，以及让开团的kp对你有一个初步印象。\n"
         "5）群内冲浪前请务必先阅读公告【本群群规】。复读有惩罚机制，请确认风险后再当复读机哟！\n"
         "6）群内骰娘在大群不开放，也仅可用于群内开团使用，并不是公骰而是群私骰。\n"
         "PS：群公告中有各种指南和信息，不懂的地方欢迎和群里小伙伴们一起交流ʕ •ᴥ•ʔ"], per_group=True,
    ),
    TemplateDef(
        "overdue.joined", "入群与拉群", "逾期名单提醒",
        "逾期名单上的人进大群时，在入群欢迎之后发送。留空则不提醒。",
        {"at": "@ 新成员", "name": "名单上的昵称", "deadlines": "名单上的死线", "note": "名单上的备注", "qq": "QQ 号"},
        ["⚠️ 管理请注意：{at}（QQ {qq}）在逾期名单上，名单昵称「{name}」，死线 {deadlines}，没还清团就退群了。"],
        samples={"name": "五", "deadlines": "2020 年 8/26，8/29，9/12", "note": "", "qq": "499806631"},
    ),
    TemplateDef(
        "invite.reject", "入群与拉群", "拉群拒绝", "骰娘被拉进没有大群成员的群时，退群前发送。",
        {"group_name": "群名"},
        ["这个群里没有找到幻梦境的群友，骰娘先告辞了。"],
    ),
    TemplateDef(
        "invite.hello", "入群与拉群", "进群问候", "骰娘被拉进群且校验通过时发送。留空则不发送。",
        {"group_name": "群名"},
        [""],
    ),
    # ---------- 俄罗斯轮盘（默认台词取自《记团.js》） ----------
    TemplateDef(
        "rr.help", "俄罗斯轮盘", "轮盘帮助", "`.轮盘 help`，以及没有进行中的对局时 `.轮盘` 的说明部分。",
        {"timeout": "超时分钟数"},
        ["俄罗斯轮盘：一把左轮6个弹仓，只装1颗子弹，中弹者记一团。\n"
         ".轮盘 join  报名（满6人开局）\n"
         ".轮盘 quit  取消报名（开局前）\n"
         ".轮盘 roll  开局后各自掷d100，大者先开枪\n"
         ".轮盘       查看当前状态\n"
         ".开枪 / .轮盘 shoot  轮到自己时扣扳机\n"
         ".轮盘 off   强制结束本局，不记团（本群管理）\n"
         "开局后{timeout}分钟无人操作，本局自动作废。记团名单见 .团\n"
         "另有五颗子弹的高加索轮盘：.高加索轮盘 help"],
    ),
    TemplateDef("rr.status_none", "俄罗斯轮盘", "状态：没有对局", "没有进行中的对局时发送 `.轮盘`。",
                {"help": "轮盘帮助的内容"}, ["当前没有进行中的轮盘。\n\n{help}"]),
    TemplateDef("rr.status_signup", "俄罗斯轮盘", "状态：报名中", "报名阶段发送 `.轮盘`。",
                {"count": "已报名人数", "max": "满员人数", "players": "已报名的人"}, ["报名中（{count}/{max}）：\n{players}"]),
    TemplateDef("rr.status_rolling", "俄罗斯轮盘", "状态：掷点中", "掷点阶段发送 `.轮盘`。",
                {"rolled": "已掷的人和点数", "pending": "还没掷的人"}, ["掷点中，请发送 .轮盘 roll\n已掷：{rolled}\n待掷：{pending}"]),
    TemplateDef("rr.status_playing", "俄罗斯轮盘", "状态：开枪中", "开枪阶段发送 `.轮盘`。",
                {"shots": "已开枪数", "left": "剩余弹仓格数", "order": "开枪顺序（多行）", "current": "当前轮到的人"},
                ["进行中，已开 {shots} 枪，剩余弹仓 {left} 格。\n{order}\n现在轮到：{current}"]),
    TemplateDef("rr.join", "俄罗斯轮盘", "报名成功", "报名后还没满员时发送。",
                {"name": "报名者", "count": "已报名人数", "max": "满员人数"}, ["{name} 报名参与俄罗斯轮盘（{count}/{max}）。"]),
    TemplateDef("rr.join_full", "俄罗斯轮盘", "满员开局", "最后一人报名、人齐开局时发送。",
                {"name": "报名者", "max": "满员人数", "pending": "待掷点的人"},
                ["{name} 报名参与俄罗斯轮盘（{max}/{max}）。\n人齐了！请每位参与者发送 .轮盘 roll 掷d100，点数大的先扣扳机。\n待掷：{pending}"]),
    TemplateDef("rr.join_dup", "俄罗斯轮盘", "重复报名", "已经报过名的人再次报名。",
                {"name": "报名者", "count": "已报名人数", "max": "满员人数"}, ["{name} 已经报过名了（{count}/{max}）。"]),
    TemplateDef("rr.join_started", "俄罗斯轮盘", "已开局时报名", "对局进行中有人报名。", {}, ["本局已经开始，请等这一局结束。"]),
    TemplateDef("rr.quit", "俄罗斯轮盘", "取消报名", "开局前取消报名。",
                {"name": "取消者", "count": "剩余报名人数", "max": "满员人数"}, ["{name} 取消了报名（{count}/{max}）。"]),
    TemplateDef("rr.quit_none", "俄罗斯轮盘", "没报名却取消", "没报名的人发送 `.轮盘 quit`。", {}, ["你没有报名。"]),
    TemplateDef("rr.quit_started", "俄罗斯轮盘", "开局后取消", "开局后想取消报名。", {}, ["已经开局了，枪在手里就别想跑。"]),
    TemplateDef("rr.roll_not_started", "俄罗斯轮盘", "未开局时掷点", "还没满员就发送 `.轮盘 roll`。", {}, ["还没满6人开局，现在不用掷点。"]),
    TemplateDef("rr.roll_ordered", "俄罗斯轮盘", "顺序已定时掷点", "开枪顺序已定后又发送 `.轮盘 roll`。", {}, ["顺序已经定好了，轮到你时发送 .开枪"]),
    TemplateDef("rr.roll_not_player", "俄罗斯轮盘", "非参与者掷点", "没参与这一局的人发送 `.轮盘 roll`。", {}, ["你没有参与这一局。"]),
    TemplateDef("rr.roll_dup", "俄罗斯轮盘", "重复掷点", "已经掷过的人再次掷点。",
                {"name": "掷点者", "roll": "已掷点数", "pending": "待掷点的人"}, ["{name} 已经掷过了（d100={roll}）。\n待掷：{pending}"]),
    TemplateDef("rr.rolled", "俄罗斯轮盘", "掷点结果", "掷点后还有人没掷时发送。",
                {"name": "掷点者", "roll": "点数", "pending": "待掷点的人"}, ["{name} 掷出 d100={roll}\n待掷：{pending}"]),
    TemplateDef("rr.tie", "俄罗斯轮盘", "点数相同", "全员掷完但有人点数相同、需要重掷时发送。",
                {"name": "最后掷点者", "roll": "其点数", "tied": "点数相同的人"},
                ["{name} 掷出 d100={roll}\n点数相同：{tied}\n请以上玩家发送 .轮盘 roll 重掷。"]),
    TemplateDef("rr.order_ready", "俄罗斯轮盘", "顺序确定", "全员掷完、装填子弹后发送。",
                {"name": "最后掷点者", "roll": "其点数", "order": "开枪顺序（多行）", "first": "第一个开枪的人"},
                ["{name} 掷出 d100={roll}\n全员掷点完毕！左轮已装填1颗子弹，转轮……\n按d100从大到小扣扳机：\n{order}\n请 {first} 发送 .开枪"]),
    TemplateDef("rr.shoot_none", "俄罗斯轮盘", "没有对局时开枪", "没有进行中的对局时发送 `.开枪`。", {},
                ["当前没有进行中的轮盘。发送 .轮盘 join 报名，满6人开局。"]),
    TemplateDef("rr.shoot_need_roll", "俄罗斯轮盘", "未掷完时开枪", "还有人没掷点时发送 `.开枪`。",
                {"pending": "待掷点的人"}, ["还有人没掷点，请发送 .轮盘 roll\n待掷：{pending}"]),
    TemplateDef("rr.shoot_not_turn", "俄罗斯轮盘", "没轮到时开枪", "不是当前开枪者发送 `.开枪`。",
                {"current": "当前轮到的人"}, ["还没轮到你，现在轮到 {current} 扣扳机。"]),
    TemplateDef(
        "rr.click", "俄罗斯轮盘", "空枪", "开枪没有中弹时发送。",
        {"name": "开枪者", "left": "剩余弹仓格数", "next": "下一位开枪者"},
        ["{name} 扣动扳机……\n咔哒。空弹，存活确认！\n剩余弹仓 {left} 格，下一位：{next}"],
    ),
    TemplateDef(
        "rr.bang", "俄罗斯轮盘", "中弹", "开枪中弹时在本群发送。",
        {"name": "中弹者", "deadline": "死线（月/日）", "entry": "此人在账本中的整行"},
        ["{name} 扣动扳机……\n砰！！{name} 中弹倒地！记一团，死线 {deadline}。\n{entry}"],
    ),
    TemplateDef(
        "rr.announce", "俄罗斯轮盘", "轮盘记团公告", "轮盘中弹后发到大群的公告。事发群就是大群时不发。",
        {"name": "中弹者", "deadline": "死线（月/日）", "entry": "此人在账本中的整行"},
        ["【记团公告】{name} 在俄罗斯轮盘中中弹，记一团，死线 {deadline}。\n{entry}"],
    ),
    TemplateDef("rr.off", "俄罗斯轮盘", "强制结束", "管理发送 `.轮盘 off` 结束对局。", {}, ["本局俄罗斯轮盘已被强制结束，不记团。"]),
    TemplateDef("rr.off_none", "俄罗斯轮盘", "没有对局时结束", "没有对局时发送 `.轮盘 off`。", {}, ["当前没有进行中的轮盘。"]),
    TemplateDef("rr.off_denied", "俄罗斯轮盘", "无权结束", "非管理发送 `.轮盘 off`。", {}, ["只有管理员可以强制结束。"]),
    TemplateDef("rr.mode_conflict", "俄罗斯轮盘", "轮盘模式冲突", "同一群不能同时报名或开两种轮盘。",
                {"mode": "当前对局的模式"},
                ["本群已有{mode}对局，请使用对应指令参与，或等本局结束后再报名另一种轮盘。"]),

    # ---------- 高加索轮盘 ----------
    TemplateDef(
        "cr.help", "高加索轮盘", "高加索轮盘帮助", "`.高加索轮盘 help`；也可用 `.高加索`。",
        {"timeout": "超时分钟数"},
        ["高加索轮盘：6个弹仓装5颗子弹，随机留下1个空弹位。六人各开一枪，五位中弹者各记一团，唯一空枪者免记团。\n"
         ".高加索轮盘 join  报名（满6人开局）\n"
         ".高加索轮盘 quit  取消报名（开局前）\n"
         ".高加索轮盘 roll  各自掷d100，大者先开枪，同点重掷\n"
         ".高加索轮盘       查看当前状态\n"
         ".开枪 / .高加索轮盘 shoot  轮到自己时扣扳机\n"
         ".高加索轮盘 off   管理强制结束，已记团保留\n"
         "中弹时立即记团并继续下一人，六枪全部打完本局结束。开局后{timeout}分钟无人操作则结束，已记团保留。\n"
         "可用中文 报名/退出/掷点/开枪/结束，.高加索 是同义指令。每群同时只开一局轮盘。"],
    ),
    TemplateDef("cr.status_signup", "高加索轮盘", "状态：报名中", "高加索轮盘报名状态。",
                {"count": "已报名人数", "max": "满员人数", "players": "已报名的人"},
                ["高加索轮盘报名中（{count}/{max}，5颗子弹）：\n{players}"]),
    TemplateDef("cr.status_rolling", "高加索轮盘", "状态：掷点中", "高加索轮盘掷点状态。",
                {"rolled": "已掷的人和点数", "pending": "还没掷的人"},
                ["高加索轮盘掷点中，请发送 .高加索轮盘 roll\n已掷：{rolled}\n待掷：{pending}"]),
    TemplateDef("cr.status_playing", "高加索轮盘", "状态：开枪中", "高加索轮盘开枪状态。",
                {"shots": "已开枪数", "left": "剩余弹仓格数", "order": "开枪顺序（多行）", "current": "当前轮到的人"},
                ["高加索轮盘进行中，已开 {shots} 枪，剩余 {left} 人未开枪。\n{order}\n现在轮到：{current}"]),
    TemplateDef("cr.join", "高加索轮盘", "报名成功", "高加索轮盘报名后还没满员。",
                {"name": "报名者", "count": "已报名人数", "max": "满员人数"},
                ["{name} 报名参与高加索轮盘（{count}/{max}）：5颗子弹，六人各开一枪，中弹各记一团。"]),
    TemplateDef("cr.join_full", "高加索轮盘", "满员开局", "高加索轮盘满员。",
                {"name": "报名者", "max": "满员人数", "pending": "待掷点的人"},
                ["{name} 报名参与高加索轮盘（{max}/{max}）。\n人齐了！请每位参与者发送 .高加索轮盘 roll 掷d100，点数大的先扣扳机。\n待掷：{pending}"]),
    TemplateDef("cr.order_ready", "高加索轮盘", "顺序确定", "高加索轮盘确定顺序并装填。",
                {"name": "最后掷点者", "roll": "其点数", "order": "开枪顺序（多行）", "first": "第一个开枪的人"},
                ["{name} 掷出 d100={roll}\n高加索轮盘已装填5颗子弹，留下1个随机空弹位，转轮……\n按d100从大到小，每人开一枪：\n{order}\n请 {first} 发送 .开枪"]),
    TemplateDef("cr.click", "高加索轮盘", "空枪", "高加索轮盘空枪后继续。",
                {"name": "开枪者", "left": "剩余未开枪人数", "next": "下一位开枪者"},
                ["{name} 扣动扳机……咔哒！唯一的空弹位，{name} 本局免记团。\n还剩 {left} 人未开枪，下一位：{next}，请发送 .开枪"]),
    TemplateDef("cr.bang", "高加索轮盘", "中弹后继续", "高加索轮盘中弹记团后继续下一人。",
                {"name": "中弹者", "deadline": "死线（月/日）", "entry": "此人在账本中的整行",
                 "left": "剩余未开枪人数", "next": "下一位开枪者"},
                ["{name} 扣动扳机……砰！高加索轮盘中弹，记一团，死线 {deadline}。\n{entry}\n还剩 {left} 人未开枪，下一位：{next}，请发送 .开枪"]),
    TemplateDef("cr.bang_last", "高加索轮盘", "最后一枪中弹", "最后一人中弹并结束高加索轮盘。",
                {"name": "中弹者", "deadline": "死线（月/日）", "entry": "此人在账本中的整行", "survivor": "唯一空枪者"},
                ["{name} 扣动扳机……砰！高加索轮盘中弹，记一团，死线 {deadline}。\n{entry}\n六枪全部打完，高加索轮盘结束：五人各记一团，唯一生还者 {survivor} 免记团。"]),
    TemplateDef("cr.click_last", "高加索轮盘", "最后一枪空枪", "最后一人空枪并结束高加索轮盘。",
                {"name": "开枪者", "survivor": "唯一空枪者"},
                ["{name} 扣动扳机……咔哒！唯一的空弹位。\n六枪全部打完，高加索轮盘结束：五人各记一团，唯一生还者 {survivor} 免记团。"]),
    TemplateDef("cr.announce", "高加索轮盘", "高加索轮盘记团公告", "每次中弹后发到大群，事发群就是大群时不重复发送。",
                {"name": "中弹者", "deadline": "死线（月/日）", "entry": "此人在账本中的整行"},
                ["【记团公告】{name} 在高加索轮盘中中弹，记一团，死线 {deadline}。\n{entry}"]),
    TemplateDef("cr.off", "高加索轮盘", "强制结束", "管理中途结束高加索轮盘，已记团保留。", {},
                ["本局高加索轮盘已被强制结束，未开枪者不再开枪，已经记下的欠团保留。"]),

    # ---------- 复读链 ----------
    TemplateDef(
        "repeat.announce", "复读链", "复读链记团公告", "复读链结算记团时在本群发送，同时发到大群（本群就是大群时只发一次）。",
        {"text": "复读的内容（超过 20 字会截断）", "count": "链上的总条数（含原句）", "name": "被记团的人", "reason": "记团原因",
         "deadline": "死线（月/日）", "entry": "此人在账本中的整行"},
        ["【记团公告】「{text}」复读 {count} 条后复读链中止。\n{name}：{reason}，记一团，死线 {deadline}。\n{entry}"],
    ),
    TemplateDef("repeat.reason_breaker", "复读链", "记团原因：亲手打断", "公告里 {reason} 的内容：打断者自己也在链里复读过。",
                {}, ["复读者亲手打断"]),
    TemplateDef("repeat.reason_third", "复读链", "记团原因：倒数第三", "公告里 {reason} 的内容：打断者没参与复读，记倒数第三位复读者。",
                {}, ["复读链倒数第三"]),
    TemplateDef("repeat.reason_dup", "复读链", "记团原因：重复复读", "公告里 {reason} 的内容：链生效后有人第二次复读。",
                {}, ["重复复读"]),

    # ---------- 团账本 ----------
    TemplateDef(
        "ledger.help", "团账本", "账本帮助", "`.团 help` 和用法不对时发送。", {},
        ["记团名单（轮盘、复读共用，跨群通用）。记团后两个月内须开团，括号内为死线。\n"
         ".团            查看记团名单\n"
         ".团 我         查看自己欠的团\n"
         ".团 消 <名字或QQ号>  消去此人死线最早的一团（管理）\n"
         ".团 加 <QQ号> <名字> <死线月/日>  手动录入一团（管理）\n"
         ".团 名 <圈名>  设置自己在名单上的名字\n"
         ".团 名 <QQ号或@某人> <圈名>  改别人的名字（管理）"],
    ),
    TemplateDef(
        "ledger.title", "团账本", "名单标题与说明", "`.团` 输出的开头部分，下面接分隔线、全部名单链接和名单。", {},
        ["【记团名单】\n榜上有名的须在死线日期（月/日）前开团，不然会被赠送飞机票哟。消团券获得者在全部名单链接中也有记录。"],
    ),
    TemplateDef("ledger.link", "团账本", "全部名单链接", "`.团` 输出中「全部名单」下面的链接。留空则不显示这一段。", {},
                ["https://docs.qq.com/sheet/DQVFWdGlzdnF5VXFK"]),
    TemplateDef("ledger.empty", "团账本", "名单为空", "没有人欠团时，名单位置显示的内容。", {}, ["暂无。"]),
    TemplateDef("ledger.mine", "团账本", "查看自己", "`.团 我` 且欠团时发送。",
                {"entry": "此人在账本中的整行", "count": "欠团数"}, ["{entry}，共 {count} 团。"]),
    TemplateDef("ledger.mine_none", "团账本", "自己不欠团", "`.团 我` 且不欠团时发送。", {}, ["你目前不欠团。"]),
    TemplateDef("ledger.removed", "团账本", "消团结果", "`.团 消` 成功且此人还欠团时发送。",
                {"name": "被消团的人", "deadline": "被消去那一团的死线", "entry": "此人在账本中的整行（消团后）"},
                ["{name} 消去一团（死线 {deadline}），剩余：{entry}"]),
    TemplateDef("ledger.removed_all", "团账本", "消团后还清", "`.团 消` 成功且此人已还清时发送。",
                {"name": "被消团的人", "deadline": "被消去那一团的死线"}, ["{name} 消去一团（死线 {deadline}），已还清。"]),
    TemplateDef("ledger.not_found", "团账本", "消团找不到人", "`.团 消` 的名字或 QQ 号不在名单里。",
                {"query": "输入的名字或QQ号"}, ["没有找到「{query}」。用法：.团 消 <名字或QQ号>"]),
    TemplateDef("ledger.ambiguous", "团账本", "消团重名", "`.团 消` 匹配到多个人时发送。",
                {"query": "输入的名字", "candidates": "匹配到的人和 QQ 号（多行）"}, ["名单里有多个「{query}」，请改用QQ号：\n{candidates}"]),
    TemplateDef("ledger.denied", "团账本", "无权消团", "没有权限的人使用 `.团 消`。", {}, ["只有大群管理员可以消团。"]),
    TemplateDef("ledger.added", "团账本", "手动记团结果", "`.团 加` 成功后发送。", {"entry": "此人在账本中的整行"}, ["已记团：{entry}"]),
    TemplateDef("ledger.add_usage", "团账本", "手动记团用法", "`.团 加` 参数不对时发送。", {},
                ["用法：.团 加 <QQ号> <名字> <死线月/日>\n例：.团 加 123456789 Flame 11/30"]),
    TemplateDef("ledger.add_denied", "团账本", "无权手动记团", "没有权限的人使用 `.团 加`。", {}, ["只有大群管理员可以手动记团。"]),
    TemplateDef("ledger.renamed", "团账本", "改名结果", "`.团 名` 成功后发送。",
                {"old": "原来的名字（没登记过时是 QQ 号）", "name": "新名字"}, ["名单上的「{old}」已改为「{name}」。"],
                samples={"old": "群昵称乱七八糟", "name": "Flame"}),
    TemplateDef("ledger.rename_usage", "团账本", "改名用法", "`.团 名` 参数不对时发送。", {},
                ["用法：.团 名 <圈名>  设置自己的名字\n管理可用：.团 名 <QQ号或@某人> <圈名>"]),
    TemplateDef("ledger.rename_denied", "团账本", "无权改别人名字", "没有权限的人改别人的名字。", {},
                ["只有大群管理员可以改别人的名字，改自己的请用 .团 名 <圈名>"]),

    # ---------- 人物卡 ----------
    TemplateDef(
        "card.saved", "人物卡", "存卡成功", "私聊存卡成功后发送。",
        {"name": "卡名"},
        ["已存档人物卡「{name}」。"],
    ),
    TemplateDef("card.save_usage", "人物卡", "存卡用法", "`.存卡` 没写卡名时发送。", {},
                ["用法：.存卡 <卡名>，同一条消息里带上人物卡截图；也可以先发指令，3 分钟内再发图。\n大群群主或管理员可代存：.存卡 <QQ号> <卡名>"]),
    TemplateDef("card.saved_other", "人物卡", "代存成功", "大群管理员为他人存卡成功后，回复上传者。",
                {"qq": "人物卡归属 QQ 号", "name": "卡名"}, ["已为 QQ {qq} 存档人物卡「{name}」。"]),
    TemplateDef("card.wait_image_other", "人物卡", "等待代存图片", "管理员先发代存指令后，提示补发图片。",
                {"qq": "人物卡归属 QQ 号", "name": "卡名"},
                ["请在 3 分钟内发送人物卡「{name}」的截图，将保存到 QQ {qq} 名下。"]),
    TemplateDef("card.save_other_denied", "人物卡", "无权代存", "非大群管理员尝试为他人存卡。", {},
                ["只有大群群主或管理员可以代存别人的人物卡。存自己的卡请用 .存卡 <卡名>。"]),
    TemplateDef("card.wait_image", "人物卡", "等待图片", "`.存卡 <卡名>` 没带图片时发送。", {"name": "卡名"},
                ["请在 3 分钟内发送人物卡「{name}」的截图。"]),
    TemplateDef("card.save_failed", "人物卡", "存卡失败", "图片下载失败时发送。", {"name": "卡名"},
                ["人物卡「{name}」的图片没能保存，请重新发送一次。"]),
    TemplateDef("card.list", "人物卡", "卡片列表", "`.取卡` 列出卡片时发送。",
                {"owner": "谁的卡", "cards": "编号和卡名，每行一张"}, ["{owner}的人物卡：\n{cards}\n发送 .取卡 <编号或卡名> 取回图片。"],
                samples={"owner": "你", "cards": "1. 调查员A\n2. 调查员B"}),
    TemplateDef("card.list_none", "人物卡", "没有卡片", "`.取卡` 但没有存过卡时发送。", {"owner": "谁的卡"},
                ["{owner}还没有存过人物卡。"], samples={"owner": "你"}),
    TemplateDef("card.not_found", "人物卡", "找不到卡片", "卡名或编号对不上时发送。", {"query": "输入的卡名或编号"},
                ["没有找到人物卡「{query}」，发送 .取卡 查看列表。"], samples={"query": "调查员C"}),
    TemplateDef("card.denied", "人物卡", "无权取卡", "取别人的卡但没有权限时发送。", {},
                ["只有大群成员可以取别人的人物卡。"]),
    TemplateDef("card.deleted", "人物卡", "删卡成功", "`.删卡` 成功后发送。", {"name": "卡名"},
                ["已删除人物卡「{name}」。"]),
    TemplateDef("card.delete_usage", "人物卡", "删卡用法", "`.删卡` 没写卡名时发送。", {},
                ["用法：.删卡 <编号或卡名>"]),
    TemplateDef(
        "card.private_only", "人物卡", "请私聊使用", "在群里使用人物卡指令时发送。",
        {},
        ["人物卡指令请私聊骰娘使用。"],
    ),
]
BY_KEY = {t.key: t for t in CATALOG}

_formatter = string.Formatter()


class TemplateError(ValueError):
    pass


def parse(text: str) -> list[tuple[str, str | None]]:
    """拆成 (字面文字, 变量名或 None) 的列表；花括号写错时抛 TemplateError。
    想输出花括号本身时写成 {{ 和 }}。"""
    try:
        parts = list(_formatter.parse(text))
    except ValueError:
        raise TemplateError("花括号没有配对。想输出花括号本身，请写成 {{ 或 }}") from None
    out = []
    for literal, name, spec, conv in parts:
        if name is not None:
            if spec or conv:
                raise TemplateError(f"变量 {{{name}{'!' + conv if conv else ''}{':' + spec if spec else ''}}} 的写法不支持，只能写成 {{变量名}}")
            if not name:
                raise TemplateError("出现了空的 {}，请写上变量名")
        out.append((literal, name))
    return out


def validate(defn: TemplateDef, variants: list[str]) -> None:
    for i, text in enumerate(variants, 1):
        where = f"第 {i} 条" if len(variants) > 1 else ""
        try:
            parts = parse(text)
        except TemplateError as e:
            raise TemplateError(f"{where}{e}") from None
        for _, name in parts:
            if name is not None and name not in defn.variables:
                allowed = "、".join("{" + v + "}" for v in defn.variables) or "无"
                raise TemplateError(f"{where}用到了未定义的变量 {{{name}}}。这条文案可用的变量：{allowed}")


def to_segments(text: str, values: dict) -> list[dict]:
    """渲染成 OneBot 消息段。变量值可以是字符串，也可以是消息段（比如 @）。"""
    segs: list[dict] = []

    def add_text(s: str):
        if not s:
            return
        if segs and segs[-1]["type"] == "text":
            segs[-1]["data"]["text"] += s
        else:
            segs.append({"type": "text", "data": {"text": s}})

    for literal, name in parse(text):
        add_text(literal)
        if name is None:
            continue
        value = values[name]
        if isinstance(value, dict) and "type" in value:
            segs.append(value)
        else:
            add_text(str(value))
    return segs


def to_plain(segs: list[dict]) -> str:
    out = []
    for s in segs:
        if s["type"] == "text":
            out.append(s["data"]["text"])
        elif s["type"] == "at":
            out.append(s["data"].get("name") or f"@{s['data']['qq']}")
        else:
            out.append(f"[{s['type']}]")
    return "".join(out)


def at(user_id: int, name: str = "") -> dict:
    return {"type": "at", "data": {"qq": str(user_id), "name": name}}


def clean_variants(variants: list[str]) -> list[str]:
    """去掉空白变体；全部为空时保留一条空串，表示不发送。"""
    kept = [v.replace("\r\n", "\n").strip("\n") for v in variants if v.strip()]
    return kept or [""]


class Templates:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def variants(self, key: str, scope: str = "") -> list[str]:
        """取某条文案的变体。按群设置的文案先找该群，找不到再用默认。"""
        defn = BY_KEY[key]
        scopes = [scope, ""] if scope and defn.per_group else [""]
        for s in scopes:
            row = self.conn.execute("SELECT variants FROM templates WHERE key = ? AND scope = ?", (key, s)).fetchone()
            if row:
                return json.loads(row["variants"])
        return list(defn.default)

    def is_customized(self, key: str, scope: str = "") -> bool:
        return self.conn.execute("SELECT 1 FROM templates WHERE key = ? AND scope = ?", (key, scope)).fetchone() is not None

    def scopes(self, key: str) -> list[str]:
        rows = self.conn.execute("SELECT scope FROM templates WHERE key = ? AND scope != '' ORDER BY scope", (key,))
        return [r["scope"] for r in rows]

    def render(self, key: str, scope: str = "", **values) -> list[dict] | None:
        """随机选一条变体渲染；文案为空时返回 None，表示不发送。"""
        choices = [v for v in self.variants(key, scope) if v.strip()]
        if not choices:
            return None
        return to_segments(random.choice(choices), values)

    def render_text(self, key: str, scope: str = "", **values) -> str | None:
        segs = self.render(key, scope, **values)
        return None if segs is None else to_plain(segs)

    def save(self, key: str, variants: list[str], operator: str, scope: str = "") -> None:
        defn = BY_KEY[key]
        if scope and not defn.per_group:
            raise TemplateError("这条文案不支持按群设置")
        variants = clean_variants(variants)
        validate(defn, variants)
        before = self.variants(key, scope)
        self.conn.execute("BEGIN")
        self.conn.execute(
            "INSERT INTO templates (key, scope, variants, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key, scope) DO UPDATE SET variants = excluded.variants, updated_at = excluded.updated_at",
            (key, scope, json.dumps(variants, ensure_ascii=False), int(time.time())),
        )
        log_op(self.conn, operator, "template.save", {"key": key, "scope": scope, "variants": before},
               {"key": key, "scope": scope, "variants": variants})
        self.conn.execute("COMMIT")

    def reset(self, key: str, operator: str, scope: str = "") -> None:
        """恢复默认。对按群设置的文案，就是删掉该群的单独设置。"""
        before = self.variants(key, scope)
        self.conn.execute("BEGIN")
        self.conn.execute("DELETE FROM templates WHERE key = ? AND scope = ?", (key, scope))
        log_op(self.conn, operator, "template.reset", {"key": key, "scope": scope, "variants": before}, None)
        self.conn.execute("COMMIT")


def preview(defn: TemplateDef, text: str) -> str:
    values = {name: defn.samples.get(name, SAMPLES.get(name, name)) for name in defn.variables}
    return to_plain(to_segments(text, values))
