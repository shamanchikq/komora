"""The last-resort error path at the Telegram edge.

Handlers translate every failure they can name; anything past them used to die with
nothing sent — the user waited on an answer that never came.
"""

from komora.bot.bot import build_dispatcher, deliver_unexpected
from komora.bot.handlers import UNEXPECTED


class RecordingBot:
    """Duck-typed stand-in for aiogram's Bot: records what would hit the wire."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self._fail = fail

    async def send_message(self, chat_id: int, text: str, **_: object) -> None:
        if self._fail:
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, text))


class FakeQuery:
    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        pass


async def test_the_notice_is_the_unexpected_copy() -> None:
    bot = RecordingBot()
    await deliver_unexpected(bot, 7)
    assert bot.sent == [(7, UNEXPECTED)]


async def test_a_callback_is_answered_so_the_spinner_stops() -> None:
    bot = RecordingBot()
    query = FakeQuery()
    await deliver_unexpected(bot, 7, query)
    assert len(bot.sent) == 1


async def test_a_failing_delivery_does_not_raise() -> None:
    """The safety net must not become a second failure of its own."""
    await deliver_unexpected(RecordingBot(fail=True), 7)


def test_the_error_net_is_registered() -> None:
    dispatcher = build_dispatcher(None)  # type: ignore[arg-type]
    assert dispatcher.errors.handlers, "no last-resort handler registered"
