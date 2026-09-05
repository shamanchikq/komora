"""The last-resort error path at the Telegram edge.

Handlers translate every failure they can name; anything past them used to die with
nothing sent — the user waited on an answer that never came.
"""

from aiogram.exceptions import TelegramBadRequest

from komora.bot.bot import (
    answer_quietly,
    build_dispatcher,
    deliver_unexpected,
    should_clear_keyboard,
)
from komora.bot.handlers import CANCELLED, SILPO_DOWN, STALE, UNEXPECTED
from komora.bot.outcomes import Spoke, Synced
from komora.core.models import SyncReport


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


class LateQuery:
    """A callback Telegram will no longer accept an answer for."""

    def __init__(self) -> None:
        self.answered: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answered.append(text)
        raise TelegramBadRequest(
            method=None,  # type: ignore[arg-type]
            message="Bad Request: query is too old and response timeout expired",
        )


async def test_a_late_callback_answer_does_not_cost_the_reply() -> None:
    """A push can outlast Telegram's answer window on Silpo alone. The failed answer
    used to raise out of the handler before the reply was sent, so a write that had
    happened was reported as silence."""
    query = LateQuery()
    await answer_quietly(query, "Готово")  # type: ignore[arg-type]
    assert query.answered == ["Готово"], "the answer is still attempted"


class TestWhichTapsSpendTheirKeyboard:
    """The keyboard a tap came from is cleared only when the basket has moved on."""

    def test_a_completed_push_spends_it(self) -> None:
        assert should_clear_keyboard("push", Synced(basket_id=1, report=SyncReport(ok=True)))

    def test_a_partial_push_spends_it_too(self) -> None:
        # The new message carries its own «Спробувати ще раз».
        report = SyncReport(ok=False, failed=[("Хліб", "не додалося")])
        assert should_clear_keyboard("push", Synced(basket_id=1, report=report))

    def test_a_push_silpo_did_not_answer_keeps_it(self) -> None:
        # Nobody decided anything: the draft is still open and «Додати в кошик» is
        # still the way to try again. Clearing it left `/basket` as the only road.
        assert not should_clear_keyboard("push", Spoke(SILPO_DOWN))

    def test_a_refused_basket_spends_it(self) -> None:
        assert should_clear_keyboard("push", Spoke(STALE, toast="Чернетка вже неактуальна"))

    def test_a_cancel_always_spends_it(self) -> None:
        assert should_clear_keyboard("cancel", Spoke(CANCELLED))

    def test_a_swap_never_does(self) -> None:
        assert not should_clear_keyboard("swap", Spoke(SILPO_DOWN))
