"""OneBot v11 正向 WebSocket 客户端：连 SnowLuma（或任何 OneBot v11 实现），
收事件、调动作，断线后自动重连。"""
import asyncio
import itertools
import json
import logging
import time
from collections.abc import Awaitable, Callable

import websockets

log = logging.getLogger("onebot")

EventHandler = Callable[[dict], Awaitable[None]]


class ActionError(RuntimeError):
    pass


class OneBot:
    def __init__(self, url: str, token: str, on_event: EventHandler):
        self.url = url
        self.token = token
        self.on_event = on_event
        self.ws = None
        self.self_id: int | None = None
        self.connected_at: float | None = None
        self.last_error: str = ""
        # 协议端心跳：QQ 掉线时 status.online 变成 false，协议端卡死时心跳会停
        self.last_heartbeat: float | None = None
        self.heartbeat_interval: float = 30
        self.online: bool | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._echo = itertools.count(1)

    @property
    def connected(self) -> bool:
        return self.ws is not None

    async def run(self) -> None:
        delay = 1
        while True:
            try:
                headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
                async with websockets.connect(self.url, additional_headers=headers, max_size=None) as ws:
                    self.ws = ws
                    self.connected_at = time.time()
                    delay = 1
                    log.info("已连接 %s", self.url)
                    asyncio.create_task(self._fetch_self_id())
                    async for raw in ws:
                        self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{time.strftime('%m-%d %H:%M:%S')} {type(e).__name__}: {e}"
                log.warning("连接断开：%s，%d 秒后重连", e, delay)
            finally:
                self.ws = None
                self.connected_at = None
                self.last_heartbeat = self.online = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ActionError("连接已断开"))
                self._pending.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _fetch_self_id(self) -> None:
        try:
            info = await self.call("get_login_info")
            self.self_id = int(info["user_id"])
            log.info("骰娘账号：%s（%s）", info.get("nickname"), self.self_id)
        except Exception as e:
            log.warning("获取登录信息失败：%s", e)

    def _dispatch(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except ValueError:
            return
        if "post_type" in data:
            if data.get("self_id"):
                self.self_id = int(data["self_id"])
            if data["post_type"] == "meta_event":
                status = data.get("status") or {}
                if "online" in status:
                    self.online = bool(status["online"]) and status.get("good", True) is not False
                if data.get("meta_event_type") == "heartbeat":
                    self.last_heartbeat = time.time()
                    if data.get("interval"):
                        self.heartbeat_interval = data["interval"] / 1000
                return
            task = asyncio.create_task(self.on_event(data))
            task.add_done_callback(_log_task_error)
        elif (echo := data.get("echo")) in self._pending:
            fut = self._pending.pop(echo)
            if not fut.done():
                fut.set_result(data)

    def problem(self, now: float | None = None) -> str | None:
        """现在有什么问题；正常时返回 None。"""
        now = now or time.time()
        if self.ws is None:
            return f"连不上协议端（{self.url}）。{self.last_error}".strip()
        if self.online is False:
            return "QQ 已掉线，可能需要在 SnowLuma 里重新扫码登录。"
        if self.last_heartbeat and now - self.last_heartbeat > 3 * self.heartbeat_interval:
            return f"协议端已经 {int(now - self.last_heartbeat)} 秒没有心跳，可能卡住了。"
        return None

    async def call(self, action: str, timeout: float = 30, **params):
        if self.ws is None:
            raise ActionError("未连接")
        echo = f"sidecar-{next(self._echo)}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        await self.ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
        try:
            resp = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(echo, None)
        if resp.get("status") != "ok" and resp.get("retcode") != 0:
            raise ActionError(f"{action} 失败：{resp.get('message') or resp.get('wording') or resp.get('retcode')}")
        return resp.get("data")

    async def send_group(self, group_id: int, message: list[dict]):
        return await self.call("send_group_msg", group_id=group_id, message=message)

    async def send_private(self, user_id: int, message: list[dict]):
        return await self.call("send_private_msg", user_id=user_id, message=message)


def _log_task_error(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        log.error("处理事件出错", exc_info=task.exception())
