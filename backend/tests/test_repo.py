"""Repository tests, against a real (in-memory) database rather than mocks."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from komora.core.models import ResolvedCart, ResolvedLine
from komora.db.repo import BasketRepo, ConversationRepo, OAuthClientRepo, UserRepo

USER = 4242


def cart() -> ResolvedCart:
    return ResolvedCart(
        lines=[
            ResolvedLine(
                product_id="p1",
                company_id="c1",
                branch_id="b1",
                name="Молоко Яготинське 2,6%",
                qty=2,
                unit="900 мл",
                unit_price=Decimal("42.90"),
                reason_kind="habit",
                reason_text="купуєте кожні ~4 дні",
            ),
            ResolvedLine(
                product_id="p2",
                company_id="c1",
                branch_id="b1",
                name="Йогурт «Галичина»",
                qty=1,
                unit="260 г",
                unit_price=Decimal("31.90"),
                reason_kind="sub",
                reason_text="заміна — немає в наявності",
                substituted_from="Йогурт «Активіа»",
            ),
        ],
        total=Decimal("117.70"),
        estimated_savings=Decimal("12.00"),
        savings_notes=["купон −25% на каву"],
    )


class TestUserRepo:
    async def test_ensure_is_idempotent(self, sessions: async_sessionmaker) -> None:
        repo = UserRepo(sessions)
        await repo.ensure(USER)
        await repo.ensure(USER)
        user = await repo.get(USER)
        assert user is not None and user.telegram_id == USER

    async def test_unknown_user_is_none(self, sessions: async_sessionmaker) -> None:
        assert await UserRepo(sessions).get(999) is None

    async def test_token_blob_roundtrip_with_absolute_expiry(
        self, sessions: async_sessionmaker
    ) -> None:
        """OAuthToken carries only a relative expires_in, so we persist an absolute
        expires_at — it is what the mcp #3250 workaround reads back."""
        repo = UserRepo(sessions)
        await repo.ensure(USER)
        expires = datetime.now(UTC) + timedelta(hours=1)
        await repo.set_token_blob(USER, b"\x00encrypted\xff", expires)

        blob, got_expires = await repo.get_token_blob(USER)
        assert blob == b"\x00encrypted\xff"
        assert got_expires is not None
        assert got_expires.tzinfo is not None, "must come back timezone-aware, not naive"
        assert abs((got_expires - expires).total_seconds()) < 1

    async def test_tokens_absent_before_linking(self, sessions: async_sessionmaker) -> None:
        repo = UserRepo(sessions)
        await repo.ensure(USER)
        assert await repo.get_token_blob(USER) == (None, None)

    async def test_clear_tokens_forces_relink(self, sessions: async_sessionmaker) -> None:
        repo = UserRepo(sessions)
        await repo.ensure(USER)
        await repo.set_token_blob(USER, b"x", datetime.now(UTC))
        await repo.clear_tokens(USER)
        assert await repo.get_token_blob(USER) == (None, None)

    async def test_setting_tokens_creates_the_user_if_absent(
        self, sessions: async_sessionmaker
    ) -> None:
        await UserRepo(sessions).set_token_blob(USER, b"x", None)
        assert await UserRepo(sessions).get(USER) is not None


class TestOAuthClientRepo:
    """The DCR registration is APP-WIDE. Scoping it per user would register a new
    OAuth client with Silpo for every Telegram user and get us rate-limited."""

    async def test_absent_initially(self, sessions: async_sessionmaker) -> None:
        assert await OAuthClientRepo(sessions).get() is None

    async def test_roundtrip(self, sessions: async_sessionmaker) -> None:
        repo = OAuthClientRepo(sessions)
        await repo.set({"client_id": "abc", "client_secret": "s"})
        assert await repo.get() == {"client_id": "abc", "client_secret": "s"}

    async def test_is_shared_not_per_user(self, sessions: async_sessionmaker) -> None:
        repo = OAuthClientRepo(sessions)
        await repo.set({"client_id": "first"})
        await repo.set({"client_id": "second"})
        assert await repo.get() == {"client_id": "second"}, "must overwrite one shared row"

    async def test_clear_enables_reregistration(self, sessions: async_sessionmaker) -> None:
        """Recovery path for an expired DCR secret — mcp #3256."""
        repo = OAuthClientRepo(sessions)
        await repo.set({"client_id": "abc"})
        await repo.clear()
        assert await repo.get() is None


class TestConversationRepo:
    async def test_appends_and_reads_back_oldest_first(self, sessions: async_sessionmaker) -> None:
        repo = ConversationRepo(sessions)
        await UserRepo(sessions).ensure(USER)
        for i in range(3):
            await repo.append(USER, "user" if i % 2 == 0 else "assistant", f"повідомлення {i}")
        history = await repo.last_n(USER, 10)
        assert [m.content for m in history] == [f"повідомлення {i}" for i in range(3)]

    async def test_last_n_keeps_the_most_recent_but_returns_chronological(
        self, sessions: async_sessionmaker
    ) -> None:
        repo = ConversationRepo(sessions)
        await UserRepo(sessions).ensure(USER)
        for i in range(10):
            await repo.append(USER, "user", f"m{i}")
        history = await repo.last_n(USER, 3)
        assert [m.content for m in history] == ["m7", "m8", "m9"]

    async def test_history_is_scoped_per_user(self, sessions: async_sessionmaker) -> None:
        repo = ConversationRepo(sessions)
        users = UserRepo(sessions)
        await users.ensure(USER)
        await users.ensure(USER + 1)
        await repo.append(USER, "user", "mine")
        await repo.append(USER + 1, "user", "theirs")
        assert [m.content for m in await repo.last_n(USER, 10)] == ["mine"]


class TestBasketRepo:
    async def test_persists_a_cart_and_reloads_it_intact(
        self, sessions: async_sessionmaker
    ) -> None:
        repo = BasketRepo(sessions)
        await UserRepo(sessions).ensure(USER)
        basket_id = await repo.create_from_cart(USER, "Звичайний кошик", "stated", cart())

        loaded = await repo.load_cart(basket_id)
        assert loaded is not None
        assert [ln.name for ln in loaded.lines] == [ln.name for ln in cart().lines]
        assert loaded.lines[0].unit_price == Decimal("42.90"), "Decimal must survive the DB"
        assert loaded.lines[1].substituted_from == "Йогурт «Активіа»"
        assert loaded.lines[1].reason_kind == "sub"
        assert loaded.total == Decimal("117.70")
        assert loaded.estimated_savings == Decimal("12.00")

    async def test_new_basket_is_the_active_draft(self, sessions: async_sessionmaker) -> None:
        repo = BasketRepo(sessions)
        await UserRepo(sessions).ensure(USER)
        basket_id = await repo.create_from_cart(USER, "t", "stated", cart())
        active = await repo.get_active(USER)
        assert active is not None and active.id == basket_id and active.status == "draft"

    async def test_synced_basket_is_no_longer_active(self, sessions: async_sessionmaker) -> None:
        repo = BasketRepo(sessions)
        await UserRepo(sessions).ensure(USER)
        basket_id = await repo.create_from_cart(USER, "t", "stated", cart())
        await repo.set_status(basket_id, "synced")
        assert await repo.get_active(USER) is None

    async def test_creating_a_draft_supersedes_the_previous_one(
        self, sessions: async_sessionmaker
    ) -> None:
        """Two live drafts would make 'confirm' ambiguous."""
        repo = BasketRepo(sessions)
        await UserRepo(sessions).ensure(USER)
        first = await repo.create_from_cart(USER, "перший", "stated", cart())
        second = await repo.create_from_cart(USER, "другий", "stated", cart())
        active = await repo.get_active(USER)
        assert active is not None and active.id == second
        assert (await repo.get_status(first)) == "discarded"

    async def test_no_active_basket_for_a_new_user(self, sessions: async_sessionmaker) -> None:
        assert await BasketRepo(sessions).get_active(USER) is None

    async def test_a_swap_edits_the_line_the_user_tapped_past_a_removed_row(
        self, sessions: async_sessionmaker
    ) -> None:
        """«⇄ N» carries an index into what `load_cart` returned, not a stored position.

        `load_cart` hides `removed` rows, so every line below one sits at a lower index
        than its `position` column. Matching on the column would have swapped a product
        the user was not looking at. Nothing sets the flag yet, which is why this is
        set directly — the mismatch would otherwise be found in the Mini App, by a user.
        """
        from sqlalchemy import select, update

        from komora.db.tables import DraftItem

        repo = BasketRepo(sessions)
        await UserRepo(sessions).ensure(USER)
        basket_id = await repo.create_from_cart(USER, "кошик", "stated", cart())

        # Hide the first line: the yoghurt is now index 0 of the visible cart.
        async with sessions() as session, session.begin():
            await session.execute(
                update(DraftItem)
                .where(DraftItem.basket_id == basket_id, DraftItem.position == 0)
                .values(removed=True)
            )

        loaded = await repo.load_cart(basket_id)
        assert loaded is not None and [ln.name for ln in loaded.lines] == ["Йогурт «Галичина»"]

        replacement = loaded.lines[0].model_copy(update={"product_id": "p9", "name": "Йогурт Live"})
        assert await repo.replace_item(basket_id, 0, replacement) is True

        reloaded = await repo.load_cart(basket_id)
        assert reloaded is not None
        assert [ln.name for ln in reloaded.lines] == ["Йогурт Live"]

        # The hidden row is untouched — it was never the one on screen.
        async with sessions() as session:
            hidden = (
                await session.execute(
                    select(DraftItem).where(
                        DraftItem.basket_id == basket_id, DraftItem.position == 0
                    )
                )
            ).scalar_one()
            assert hidden.product_id == "p1"

    async def test_load_cart_of_unknown_basket_is_none(self, sessions: async_sessionmaker) -> None:
        assert await BasketRepo(sessions).load_cart(12345) is None

    @pytest.mark.parametrize("status", ["confirmed", "synced", "discarded"])
    async def test_status_transitions_persist(
        self, sessions: async_sessionmaker, status: str
    ) -> None:
        repo = BasketRepo(sessions)
        await UserRepo(sessions).ensure(USER)
        basket_id = await repo.create_from_cart(USER, "t", "stated", cart())
        await repo.set_status(basket_id, status)  # type: ignore[arg-type]
        assert await repo.get_status(basket_id) == status


class TestSchemaGuard:
    """The failure this guards against: `alembic upgrade head` reported success,
    `alembic check` reported no drift, and the first basket died on a missing column —
    because alembic had migrated a different database than the bot opened.
    """

    def test_a_database_at_head_is_accepted(self, tmp_path) -> None:
        from komora.db.migrate import assert_current, current_and_head

        url = f"sqlite:///{tmp_path / 'at-head.db'}"
        _, head = current_and_head(url)
        _stamp(url, head)
        assert_current(url)  # must not raise

    def test_an_unmigrated_database_is_refused(self, tmp_path) -> None:
        from komora.db.migrate import SchemaOutOfDate, assert_current

        url = f"sqlite:///{tmp_path / 'blank.db'}"
        with pytest.raises(SchemaOutOfDate) as caught:
            assert_current(url)
        assert "no migration at all" in str(caught.value)

    def test_the_message_names_the_database_and_the_fix(self, tmp_path) -> None:
        """A message that does not say *which* database is what cost the live run."""
        from komora.db.migrate import SchemaOutOfDate, assert_current

        path = tmp_path / "stale.db"
        url = f"sqlite:///{path}"
        _stamp(url, "3de2908b9836")
        with pytest.raises(SchemaOutOfDate) as caught:
            assert_current(url)
        message = str(caught.value)
        assert str(path) in message
        assert "alembic upgrade head" in message
        assert "KOMORA_DATABASE_URL" in message


def _stamp(url: str, revision: str | None) -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        connection.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": revision})
    engine.dispose()
