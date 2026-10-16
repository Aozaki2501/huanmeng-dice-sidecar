// ==UserScript==
// @name         记团（轮盘 + 复读链）
// @author       苍崎
// @version      2.1.0
// @description  俄罗斯轮盘与复读链共用一份跨群团账本，记团后在大群公告。.rr help / .团 help
// @timestamp    1760572800
// @license      MIT
// @homepageURL  https://github.com/sealdice/javascript
// ==/UserScript==

/*
 * 配置（WebUI → 扩展功能 → JS 扩展 → 插件设置）：
 *   公告群号       大群群号，记团公告发到这里，也是"消团"权限的依据（大群管理员可消团）
 *   复读检测群号   需要检测复读的群，逗号分隔；留空则只检测公告群
 *   记团管理员QQ   额外允许消团的 QQ 号，逗号分隔（骰主 Master 始终可以）
 */

let ext = seal.ext.find('jituan');
if (!ext) {
    ext = seal.ext.new('jituan', '苍崎', '2.1.0');
    seal.ext.register(ext);
}

seal.ext.registerStringConfig(ext, '公告群号', '', '大群群号，记团公告发送到这里');
seal.ext.registerStringConfig(ext, '复读检测群号', '', '检测复读的群号，逗号分隔；留空则只检测公告群');
seal.ext.registerStringConfig(ext, '记团管理员QQ', '', '额外允许消团的QQ号，逗号分隔');
seal.ext.registerStringConfig(ext, '名单说明', '榜上有名的须在死线日期（月/日）前开团，不然会被赠送飞机票哟。消团券获得者在全部名单链接中也有记录。', '记团名单标题下的说明文字');
seal.ext.registerStringConfig(ext, '名单链接', 'https://docs.qq.com/sheet/DQVFWdGlzdnF5VXFK', '全部名单链接，留空则不显示');

const MAX_PLAYERS = 6;
const GAME_TIMEOUT = 15 * 60 * 1000; // 开局后 15 分钟无人操作，本局作废
const MIN_CHAIN = 3;                 // 复读链成型的最小长度
const DEADLINE_MONTHS = 2;           // 记团后多久内须开团
const BJ = 8 * 3600 * 1000;

// ==========================================
// 配置与通用工具
// ==========================================
function cfg(key) {
    return String(seal.ext.getStringConfig(ext, key) || '').trim();
}

function cfgList(key) {
    return cfg(key).split(/[,，\s]+/).filter(Boolean);
}

function normGroup(id) {
    id = String(id).trim();
    if (!id) return '';
    return id.startsWith('QQ-Group:') ? id : 'QQ-Group:' + id;
}

function normUser(id) {
    id = String(id).trim();
    return id.startsWith('QQ:') ? id : 'QQ:' + id;
}

function bigGroup() {
    return normGroup(cfg('公告群号'));
}

function repeatGroups() {
    let list = cfgList('复读检测群号').map(normGroup);
    if (list.length === 0 && bigGroup()) list = [bigGroup()];
    return list;
}

// 消团权限：骰主 Master、在大群里的大群管理/群主、白名单
function isLedgerAdmin(ctx, msg) {
    if (ctx.privilegeLevel >= 100) return true;
    if (msg.groupId && msg.groupId === bigGroup() && ctx.privilegeLevel >= 50) return true;
    return cfgList('记团管理员QQ').map(normUser).includes(ctx.player.userId);
}

// 时间一律按北京时间处理，避免服务器时区不同
function bjParts(ms) {
    let d = new Date(ms + BJ);
    return { y: d.getUTCFullYear(), m: d.getUTCMonth(), d: d.getUTCDate() };
}

function md(ms) {
    let p = bjParts(ms);
    return `${p.m + 1}/${p.d}`;
}

function msgMs(msg) {
    return msg.time ? msg.time * 1000 : Date.now();
}

// 加 n 个月，落到当天北京时间 0 点；目标月没有这一天时取月末（12/31 → 2/28）
function addMonths(ms, n) {
    let p = bjParts(ms);
    let m = p.m + n;
    let y = p.y + Math.floor(m / 12);
    m = ((m % 12) + 12) % 12;
    let last = new Date(Date.UTC(y, m + 1, 0)).getUTCDate();
    return Date.UTC(y, m, Math.min(p.d, last)) - BJ;
}

// 解析手动录入的"月/日"死线，年份取离现在最近的那一年
function parseMd(text) {
    let mt = /^(\d{1,2})[\/.月](\d{1,2})日?$/.exec(text);
    if (!mt) return null;
    let mo = parseInt(mt[1]) - 1, da = parseInt(mt[2]);
    if (mo < 0 || mo > 11 || da < 1 || da > 31) return null;
    let now = Date.now(), y = bjParts(now).y, best = null;
    [y - 1, y, y + 1].forEach(yy => {
        let t = Date.UTC(yy, mo, da) - BJ;
        if (best === null || Math.abs(t - now) < Math.abs(best - now)) best = t;
    });
    return best;
}

// ==========================================
// 团账本（全局，跨群共用）
// [{ id, name, records: [{ at, deadline, reason }] }]  at/deadline 为毫秒时间戳
// ==========================================
function getLedger() {
    let s = ext.storageGet('LEDGER');
    return s ? JSON.parse(s) : [];
}

function saveLedger(list) {
    ext.storageSet('LEDGER', JSON.stringify(list));
}

// 写入一条记录；deadline 不传则按记团时间 + 2 个月计算
function addRecord(id, name, at, reason, deadline) {
    let rec = { at, deadline: deadline || addMonths(at, DEADLINE_MONTHS), reason };
    let list = getLedger();
    let e = list.find(x => x.id === id);
    if (e) {
        if (name) e.name = name;
        e.records.push(rec);
        e.records.sort((a, b) => a.deadline - b.deadline);
    } else {
        e = { id, name, records: [rec] };
        list.push(e);
    }
    saveLedger(list);
    return { e, rec };
}

// 名单按最早死线排序
function sortLedger(list) {
    return list.slice().sort((a, b) => a.records[0].deadline - b.records[0].deadline);
}

function entryLine(e) {
    return `${e.name}（${e.records.map(r => md(r.deadline)).join('，')}）`;
}

const RULE = '————————————';

function formatLedger(list) {
    let note = cfg('名单说明'), link = cfg('名单链接');
    let s = '【记团名单】';
    if (note) s += '\n' + note;
    s += '\n' + RULE;
    if (link) s += '\n全部名单：\n' + link + '\n' + RULE;
    if (list.length === 0) return s + '\n暂无。';
    return s + '\n' + sortLedger(list).map(entryLine).join('\n');
}

// 按名字或QQ号找人；名字重名时返回 -2（此时改用QQ号）
function findEntry(list, target) {
    let hits = list.map((e, i) => e.name === target ? i : -1).filter(i => i >= 0);
    if (hits.length > 1) return -2;
    if (hits.length === 1) return hits[0];
    if (/^\d{5,}$/.test(target)) return list.findIndex(e => e.id === normUser(target));
    return -1;
}

// 向大群发公告；当前就在大群时不重复发
function announce(ctx, msg, text) {
    let big = bigGroup();
    if (!big || msg.groupId === big) return;
    try {
        let m = seal.newMessage();
        m.messageType = 'group';
        m.groupId = big;
        m.sender = {};
        let c = seal.createTempCtx(ctx.endPoint, m);
        seal.replyToSender(c, m, text);
    } catch (e) {
        console.log('记团公告发送失败：' + e);
    }
}

// ==========================================
// 俄罗斯轮盘
// ==========================================
function getGame(groupId) {
    let s = ext.storageGet('GAME_' + groupId);
    if (!s) return null;
    let game = JSON.parse(s);
    if (game.started && game.updated && Date.now() - game.updated > GAME_TIMEOUT) {
        clearGame(groupId);
        return null;
    }
    return game;
}

function saveGame(groupId, game) {
    game.updated = Date.now();
    ext.storageSet('GAME_' + groupId, JSON.stringify(game));
}

function clearGame(groupId) {
    ext.storageSet('GAME_' + groupId, '');
}

function formatOrder(game) {
    return game.order.map((p, i) => `${i + 1}. ${p.name}（d100=${p.roll}）`).join('\n');
}

function pendingNames(game) {
    return game.players.filter(p => !p.roll).map(p => p.name).join('、');
}

function rollOrder(ctx, msg) {
    let groupId = msg.groupId;
    let game = getGame(groupId);
    if (!game || !game.started) {
        seal.replyToSender(ctx, msg, '还没满6人开局，现在不用掷点。');
        return;
    }
    if (game.order) {
        seal.replyToSender(ctx, msg, '顺序已经定好了，轮到你时发送 .开枪');
        return;
    }
    let player = game.players.find(p => p.id === ctx.player.userId);
    if (!player) {
        seal.replyToSender(ctx, msg, '你没有参与这一局。');
        return;
    }
    if (player.roll) {
        seal.replyToSender(ctx, msg, `${player.name} 已经掷过了（d100=${player.roll}）。\n待掷：${pendingNames(game)}`);
        return;
    }

    player.roll = Math.floor(Math.random() * 100) + 1;
    let reply = `${player.name} 掷出 d100=${player.roll}`;

    let pending = pendingNames(game);
    if (pending) {
        saveGame(groupId, game);
        seal.replyToSender(ctx, msg, `${reply}\n待掷：${pending}`);
        return;
    }

    let count = {};
    game.players.forEach(p => { count[p.roll] = (count[p.roll] || 0) + 1; });
    let tied = game.players.filter(p => count[p.roll] > 1);
    if (tied.length > 0) {
        let tieText = tied.map(p => `${p.name}(${p.roll})`).join('、');
        tied.forEach(p => { p.roll = 0; });
        saveGame(groupId, game);
        seal.replyToSender(ctx, msg, `${reply}\n点数相同：${tieText}\n请以上玩家发送 .rr roll 重掷。`);
        return;
    }

    game.order = game.players.slice().sort((a, b) => b.roll - a.roll);
    game.bullet = Math.floor(Math.random() * MAX_PLAYERS);
    game.turn = 0;
    saveGame(groupId, game);
    seal.replyToSender(ctx, msg, `${reply}\n全员掷点完毕！左轮已装填1颗子弹，转轮……\n按d100从大到小扣扳机：\n${formatOrder(game)}\n请 ${game.order[0].name} 发送 .开枪`);
}

function shoot(ctx, msg) {
    let groupId = msg.groupId;
    let game = getGame(groupId);
    if (!game || !game.started) {
        seal.replyToSender(ctx, msg, '当前没有进行中的轮盘。发送 .rr join 报名，满6人开局。');
        return;
    }
    if (!game.order) {
        seal.replyToSender(ctx, msg, `还有人没掷点，请发送 .rr roll\n待掷：${pendingNames(game)}`);
        return;
    }

    let current = game.order[game.turn];
    if (current.id !== ctx.player.userId) {
        seal.replyToSender(ctx, msg, `还没轮到你，现在轮到 ${current.name} 扣扳机。`);
        return;
    }

    if (game.turn === game.bullet) {
        let { e, rec } = addRecord(current.id, current.name, msgMs(msg), '轮盘');
        clearGame(groupId);
        seal.replyToSender(ctx, msg, `${current.name} 扣动扳机……\n砰！！${current.name} 中弹倒地！记一团，死线 ${md(rec.deadline)}。\n${entryLine(e)}`);
        announce(ctx, msg, `【记团公告】${current.name} 在俄罗斯轮盘中中弹，记一团，死线 ${md(rec.deadline)}。\n${entryLine(e)}`);
        return;
    }

    game.turn++;
    saveGame(groupId, game);
    seal.replyToSender(ctx, msg, `${current.name} 扣动扳机……\n咔哒。空弹，存活确认！\n剩余弹仓 ${MAX_PLAYERS - game.turn} 格，下一位：${game.order[game.turn].name}`);
}

const cmdShoot = seal.ext.newCmdItemInfo();
cmdShoot.name = '开枪';
cmdShoot.help = '俄罗斯轮盘：轮到你时发送 .开枪';
cmdShoot.disabledInPrivate = true;
cmdShoot.solve = (ctx, msg, cmdArgs) => {
    shoot(ctx, msg);
    return seal.ext.newCmdExecuteResult(true);
};

const cmdRr = seal.ext.newCmdItemInfo();
cmdRr.name = 'rr';
cmdRr.help = '俄罗斯轮盘：一把左轮6个弹仓，只装1颗子弹，中弹者记一团。\n' +
    '.rr join  报名（满6人开局）\n' +
    '.rr quit  取消报名（开局前）\n' +
    '.rr roll  开局后各自掷d100，大者先开枪\n' +
    '.rr       查看当前状态\n' +
    '.开枪 / .rr shoot  轮到自己时扣扳机\n' +
    '.rr off   强制结束本局，不记团（本群管理）\n' +
    '开局后15分钟无人操作，本局自动作废。记团名单见 .团';
cmdRr.disabledInPrivate = true;
cmdRr.solve = (ctx, msg, cmdArgs) => {
    let sub = cmdArgs.getArgN(1);
    let groupId = msg.groupId;
    let game = getGame(groupId);
    let uid = ctx.player.userId;
    let name = ctx.player.name;

    if (sub === 'help') {
        seal.replyToSender(ctx, msg, cmdRr.help);
    } else if (sub === 'join' || sub === '报名') {
        if (game && game.started) {
            seal.replyToSender(ctx, msg, '本局已经开始，请等这一局结束。');
        } else {
            if (!game) game = { players: [], started: false };
            if (game.players.some(p => p.id === uid)) {
                seal.replyToSender(ctx, msg, `${name} 已经报过名了（${game.players.length}/${MAX_PLAYERS}）。`);
            } else {
                game.players.push({ id: uid, name: name });
                if (game.players.length < MAX_PLAYERS) {
                    saveGame(groupId, game);
                    seal.replyToSender(ctx, msg, `${name} 报名参与俄罗斯轮盘（${game.players.length}/${MAX_PLAYERS}）。`);
                } else {
                    game.started = true;
                    saveGame(groupId, game);
                    seal.replyToSender(ctx, msg, `${name} 报名参与俄罗斯轮盘（${MAX_PLAYERS}/${MAX_PLAYERS}）。\n人齐了！请每位参与者发送 .rr roll 掷d100，点数大的先扣扳机。\n待掷：${pendingNames(game)}`);
                }
            }
        }
    } else if (sub === 'quit' || sub === '退出') {
        if (!game || !game.players.some(p => p.id === uid)) {
            seal.replyToSender(ctx, msg, '你没有报名。');
        } else if (game.started) {
            seal.replyToSender(ctx, msg, '已经开局了，枪在手里就别想跑。');
        } else {
            game.players = game.players.filter(p => p.id !== uid);
            if (game.players.length === 0) clearGame(groupId);
            else saveGame(groupId, game);
            seal.replyToSender(ctx, msg, `${name} 取消了报名（${game.players.length}/${MAX_PLAYERS}）。`);
        }
    } else if (sub === 'roll' || sub === '掷点') {
        rollOrder(ctx, msg);
    } else if (sub === 'shoot' || sub === '开枪') {
        shoot(ctx, msg);
    } else if (sub === 'list' || sub === '名单') {
        seal.replyToSender(ctx, msg, formatLedger(getLedger()));
    } else if (sub === 'off' || sub === '结束') {
        if (!game) {
            seal.replyToSender(ctx, msg, '当前没有进行中的轮盘。');
        } else if (ctx.privilegeLevel < 50) {
            seal.replyToSender(ctx, msg, '只有管理员可以强制结束。');
        } else {
            clearGame(groupId);
            seal.replyToSender(ctx, msg, '本局俄罗斯轮盘已被强制结束，不记团。');
        }
    } else if (!sub) {
        if (!game) {
            seal.replyToSender(ctx, msg, `当前没有进行中的轮盘。\n\n${cmdRr.help}`);
        } else if (!game.started) {
            seal.replyToSender(ctx, msg, `报名中（${game.players.length}/${MAX_PLAYERS}）：\n${game.players.map(p => p.name).join('、')}`);
        } else if (!game.order) {
            let rolled = game.players.filter(p => p.roll).map(p => `${p.name}(${p.roll})`).join('、') || '无';
            seal.replyToSender(ctx, msg, `掷点中，请发送 .rr roll\n已掷：${rolled}\n待掷：${pendingNames(game)}`);
        } else {
            seal.replyToSender(ctx, msg, `进行中，已开 ${game.turn} 枪，剩余弹仓 ${MAX_PLAYERS - game.turn} 格。\n${formatOrder(game)}\n现在轮到：${game.order[game.turn].name}`);
        }
    } else {
        seal.replyToSender(ctx, msg, cmdRr.help);
    }
    return seal.ext.newCmdExecuteResult(true);
};

// ==========================================
// 复读链
// 规则：同一人连发也计入链长；链长 >= 3 才成型；
// 成型后被打断：打断者在链中出现过则记打断者，否则记倒数第三人。
// 只有纯文字能开链/续链；图片、表情、指令等只会打断。
// 链状态只存在内存里，重载 JS 后清空。
// ==========================================
let chains = {}; // groupId -> { text, members: [{ id, name }] }

function plainText(msg) {
    let raw = String(msg.message || '');
    if (/\[CQ:/.test(raw)) return null;
    let t = raw.trim();
    return t || null;
}

function settleChain(ctx, msg, ch) {
    let breakerId = ctx.player.userId;
    let n = ch.members.length;
    let target, why;
    if (ch.members.some(m => m.id === breakerId)) {
        target = { id: breakerId, name: ctx.player.name };
        why = '复读者亲手打断';
    } else {
        target = ch.members[n - 3];
        why = '复读链倒数第三';
    }
    let { e, rec } = addRecord(target.id, target.name, msgMs(msg), '复读');
    let preview = ch.text.length > 20 ? ch.text.slice(0, 20) + '…' : ch.text;
    let text = `【记团公告】「${preview}」复读 ${n} 次后被打断。\n${target.name}：${why}，记一团，死线 ${md(rec.deadline)}。\n${entryLine(e)}`;
    seal.replyToSender(ctx, msg, text);
    announce(ctx, msg, text);
}

// text 为 null 表示非纯文字消息或指令：只打断，不开链
function trackRepeat(ctx, msg, text) {
    let g = msg.groupId;
    if (!g || !repeatGroups().includes(g)) return;
    let ch = chains[g];
    if (ch && text !== null && text === ch.text) {
        ch.members.push({ id: ctx.player.userId, name: ctx.player.name });
        return;
    }
    if (ch && ch.members.length >= MIN_CHAIN) settleChain(ctx, msg, ch);
    chains[g] = text === null ? null : { text, members: [{ id: ctx.player.userId, name: ctx.player.name }] };
}

ext.onNotCommandReceived = (ctx, msg) => {
    if (msg.messageType !== 'group') return;
    trackRepeat(ctx, msg, plainText(msg));
};

ext.onCommandReceived = (ctx, msg, cmdArgs) => {
    if (msg.messageType !== 'group') return;
    trackRepeat(ctx, msg, null);
};

// ==========================================
// 团账本指令 .团
// ==========================================
const cmdTuan = seal.ext.newCmdItemInfo();
cmdTuan.name = '团';
cmdTuan.help = '记团名单（轮盘、复读共用，跨群通用）。记团后两个月内须开团，括号内为死线。\n' +
    '.团            查看记团名单\n' +
    '.团 我         查看自己欠的团\n' +
    '.团 消 <名字或QQ号>  消去此人死线最早的一团（管理）\n' +
    '.团 加 <QQ号> <名字> <死线月/日>  手动录入一团，用于导入旧名单（管理）';
cmdTuan.solve = (ctx, msg, cmdArgs) => {
    let sub = cmdArgs.getArgN(1);
    let list = getLedger();

    if (!sub || sub === 'list' || sub === '名单') {
        seal.replyToSender(ctx, msg, formatLedger(list));
    } else if (sub === 'help') {
        seal.replyToSender(ctx, msg, cmdTuan.help);
    } else if (sub === '我' || sub === 'me') {
        let e = list.find(x => x.id === ctx.player.userId);
        seal.replyToSender(ctx, msg, e ? `${entryLine(e)}，共 ${e.records.length} 团。` : '你目前不欠团。');
    } else if (sub === '加' || sub === 'add') {
        if (!isLedgerAdmin(ctx, msg)) {
            seal.replyToSender(ctx, msg, '只有大群管理员可以手动记团。');
            return seal.ext.newCmdExecuteResult(true);
        }
        let qq = cmdArgs.getArgN(2), nm = cmdArgs.getArgN(3), dl = parseMd(cmdArgs.getArgN(4));
        if (!/^\d{5,}$/.test(qq) || !nm || dl === null) {
            seal.replyToSender(ctx, msg, '用法：.团 加 <QQ号> <名字> <死线月/日>\n例：.团 加 123456789 Flame 11/30');
        } else {
            let { e } = addRecord(normUser(qq), nm, null, '手动', dl);
            seal.replyToSender(ctx, msg, `已记团：${entryLine(e)}`);
        }
    } else if (sub === '消' || sub === 'del') {
        if (!isLedgerAdmin(ctx, msg)) {
            seal.replyToSender(ctx, msg, '只有大群管理员可以消团。');
            return seal.ext.newCmdExecuteResult(true);
        }
        let target = cmdArgs.getRestArgsFrom(2).trim();
        let idx = target ? findEntry(list, target) : -1;
        if (idx === -2) {
            let dup = list.filter(e => e.name === target).map(e => `${entryLine(e)}  QQ ${e.id.replace('QQ:', '')}`).join('\n');
            seal.replyToSender(ctx, msg, `名单里有多个「${target}」，请改用QQ号：\n${dup}`);
        } else if (idx < 0) {
            seal.replyToSender(ctx, msg, `没有找到「${target}」。用法：.团 消 <名字或QQ号>`);
        } else {
            let e = list[idx];
            let r = e.records.shift();
            let text;
            if (e.records.length === 0) {
                list.splice(idx, 1);
                text = `${e.name} 消去一团（死线 ${md(r.deadline)}），已还清。`;
            } else {
                text = `${e.name} 消去一团（死线 ${md(r.deadline)}），剩余：${entryLine(e)}`;
            }
            saveLedger(list);
            seal.replyToSender(ctx, msg, text);
        }
    } else {
        seal.replyToSender(ctx, msg, cmdTuan.help);
    }
    return seal.ext.newCmdExecuteResult(true);
};

ext.cmdMap['rr'] = cmdRr;
ext.cmdMap['轮盘'] = cmdRr;
ext.cmdMap['开枪'] = cmdShoot;
ext.cmdMap['团'] = cmdTuan;
ext.cmdMap['jt'] = cmdTuan;
