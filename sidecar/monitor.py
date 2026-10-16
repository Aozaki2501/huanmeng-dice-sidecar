"""掉线提醒：骰娘连不上协议端、QQ 掉线或心跳停了，持续几分钟后在电脑桌面弹通知；
恢复后再弹一次。后台页面顶部也会显示红字（见 base.html）。
QQ 掉线时没法用 QQ 发提醒，所以提醒发在运行骰娘的这台电脑上。"""
import asyncio
import logging
import os
import subprocess
import sys
import time

from .onebot import OneBot

log = logging.getLogger("monitor")

ALERT_AFTER = 300  # 问题持续多少秒才提醒，避免重连、重启时误报
CHECK_EVERY = 30

_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text></text><text></text></binding></visual></toast>')
$t = $xml.GetElementsByTagName('text')
$t.Item(0).AppendChild($xml.CreateTextNode($env:TOAST_TITLE)) | Out-Null
$t.Item(1).AppendChild($xml.CreateTextNode($env:TOAST_TEXT)) | Out-Null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def desktop_notify(title: str, text: str) -> None:
    """Windows 桌面右下角通知；其他系统只写日志。"""
    if sys.platform != "win32":
        return
    env = {**os.environ, "TOAST_TITLE": title, "TOAST_TEXT": text}
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _TOAST], env=env,
                           timeout=30, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode:
            log.warning("桌面通知发送失败：%s", r.stderr.decode("gbk", "replace").strip())
    except Exception as e:
        log.warning("桌面通知发送失败：%s", e)


class Monitor:
    def __init__(self, onebot: OneBot, notify=desktop_notify):
        self.onebot = onebot
        self.notify = notify
        self.problem: str | None = None  # 当前问题，后台页面显示用
        self.since: float | None = None  # 问题从什么时候开始
        self.alerted = False

    def check(self, now: float | None = None) -> None:
        now = now or time.time()
        problem = self.onebot.problem(now)
        if problem is None:
            if self.alerted:
                log.info("骰娘已恢复正常")
                self.notify("骰娘已恢复", "旁路服务重新连上了 QQ。")
            self.problem = self.since = None
            self.alerted = False
            return
        self.problem = problem
        self.since = self.since or now
        if not self.alerted and now - self.since >= ALERT_AFTER:
            self.alerted = True
            log.error("骰娘异常已持续 %d 分钟：%s", (now - self.since) // 60, problem)
            self.notify("骰娘掉线了", problem)

    async def run(self) -> None:
        await asyncio.sleep(CHECK_EVERY)  # 刚启动时给连接一点时间
        while True:
            await asyncio.to_thread(self.check)
            await asyncio.sleep(CHECK_EVERY)
