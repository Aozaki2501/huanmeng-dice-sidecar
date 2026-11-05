import asyncio
import json

import pytest

from sidecar.onebot import ActionError, OneBot


class ReplyWS:
    def __init__(self, bot, reply=None, error=None):
        self.bot, self.reply, self.error = bot, reply, error

    async def send(self, raw):
        if self.error:
            raise self.error
        if self.reply is not None:
            echo = json.loads(raw)["echo"]
            self.bot._dispatch(json.dumps({**self.reply, "echo": echo}))


@pytest.mark.parametrize("reply", [
    {"status": "failed", "retcode": 0},
    {"status": "ok", "retcode": 1400},
    {"status": "failed", "retcode": 1400},
    {},
])
def test_failed_action_is_reported(reply):
    bot = OneBot("ws://test", "", None)
    bot.ws = ReplyWS(bot, {**reply, "message": "发送被拒绝"})
    with pytest.raises(ActionError, match="send_group_msg.*发送被拒绝"):
        asyncio.run(bot.call("send_group_msg"))
    assert bot._pending == {}


@pytest.mark.parametrize("reply", [
    {"status": "ok", "retcode": 0},
    {"retcode": 0},
    {"status": "ok"},
])
def test_successful_action_returns_data(reply):
    bot = OneBot("ws://test", "", None)
    bot.ws = ReplyWS(bot, {**reply, "data": {"message_id": 123}})
    assert asyncio.run(bot.call("send_group_msg")) == {"message_id": 123}
    assert bot._pending == {}


def test_action_timeout_is_reported_and_cleaned_up():
    bot = OneBot("ws://test", "", None)
    bot.ws = ReplyWS(bot)
    with pytest.raises(ActionError, match="send_private_msg.*超时"):
        asyncio.run(bot.call("send_private_msg", timeout=0))
    assert bot._pending == {}


def test_send_failure_is_reported_and_cleaned_up():
    bot = OneBot("ws://test", "", None)
    bot.ws = ReplyWS(bot, error=OSError("连接中断"))
    with pytest.raises(ActionError, match="send_group_msg.*连接中断"):
        asyncio.run(bot.call("send_group_msg"))
    assert bot._pending == {}


def test_cancellation_is_not_swallowed():
    bot = OneBot("ws://test", "", None)
    bot.ws = ReplyWS(bot)

    async def run():
        task = asyncio.create_task(bot.call("send_group_msg"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert bot._pending == {}
