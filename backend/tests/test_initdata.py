"""The Mini App handshake: `initData` in, a telegram id out — or a named refusal.

The payloads are built by hand (`fakes.sign_init_data`), never by the code under
test, so these check the verifier against Telegram's documented construction.
"""

import json
import time

import pytest

from komora.core.initdata import InitDataRejected, verify_init_data
from tests.fakes import INITDATA_TOKEN, sign_init_data, signed_init_data

USER = 4242


class TestVerifyInitData:
    def test_a_valid_payload_names_its_user(self) -> None:
        assert verify_init_data(signed_init_data(USER), INITDATA_TOKEN) == USER

    def test_a_tampered_payload_is_rejected(self) -> None:
        payload = signed_init_data(USER)
        tampered = f"{payload[:-2]}xy"
        with pytest.raises(InitDataRejected):
            verify_init_data(tampered, INITDATA_TOKEN)

    def test_a_payload_signed_by_another_bot_is_rejected(self) -> None:
        """The token is not just an identifier — it is the verification key."""
        with pytest.raises(InitDataRejected):
            verify_init_data(signed_init_data(USER), "42:some-other-bot")

    def test_a_stale_payload_is_rejected(self) -> None:
        """initData is replayable within its TTL; past it, it is worthless."""
        with pytest.raises(InitDataRejected, match="stale"):
            verify_init_data(signed_init_data(USER, age_s=86_401), INITDATA_TOKEN)

    def test_the_window_boundary_is_inclusive(self) -> None:
        assert verify_init_data(signed_init_data(USER, age_s=86_400), INITDATA_TOKEN) == USER

    def test_an_implausible_future_auth_date_is_rejected_too(self) -> None:
        """A clock skewed forward past the window is as suspect as a stale one."""
        forged = sign_init_data(
            {
                "auth_date": str(int(time.time()) + 86_401),
                "user": json.dumps({"id": USER}),
            }
        )
        with pytest.raises(InitDataRejected, match="stale"):
            verify_init_data(forged, INITDATA_TOKEN)

    def test_a_modest_future_skew_is_tolerated(self) -> None:
        """`abs()`: the window guards replays in both directions, not client clocks."""
        skewed = sign_init_data(
            {
                "auth_date": str(int(time.time()) + 120),
                "user": json.dumps({"id": USER}),
            }
        )
        assert verify_init_data(skewed, INITDATA_TOKEN) == USER

    def test_a_missing_hash_is_rejected(self) -> None:
        with pytest.raises(InitDataRejected, match="hash"):
            verify_init_data(signed_init_data(USER, omit_hash=True), INITDATA_TOKEN)

    def test_a_missing_user_field_is_rejected(self) -> None:
        fields = {"auth_date": str(int(time.time())), "query_id": "AAF"}
        with pytest.raises(InitDataRejected, match="user"):
            verify_init_data(sign_init_data(fields), INITDATA_TOKEN)

    def test_a_malformed_user_field_is_rejected(self) -> None:
        fields = {"auth_date": str(int(time.time())), "user": "{not json"}
        with pytest.raises(InitDataRejected, match="user"):
            verify_init_data(sign_init_data(fields), INITDATA_TOKEN)

    def test_a_user_field_without_an_integer_id_is_rejected(self) -> None:
        fields = {
            "auth_date": str(int(time.time())),
            "user": '{"id": "soon"}',
        }
        with pytest.raises(InitDataRejected, match="user"):
            verify_init_data(sign_init_data(fields), INITDATA_TOKEN)

    def test_a_non_positive_user_id_is_rejected(self) -> None:
        fields = {
            "auth_date": str(int(time.time())),
            "user": '{"id": 0}',
        }
        with pytest.raises(InitDataRejected, match="user"):
            verify_init_data(sign_init_data(fields), INITDATA_TOKEN)
