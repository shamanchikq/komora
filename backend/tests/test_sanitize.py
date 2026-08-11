"""The sanitizer is the last thing between a real Silpo account and a public repo."""

import pytest

from komora.core.mcp.sanitize import REDACTED, sanitize


class TestRedacts:
    @pytest.mark.parametrize(
        "key",
        [
            "firstName",
            "last_name",
            "phone",
            "phoneNumber",
            "email",
            "birthDate",
            "address",
            "latitude",
            "cardNumber",
            "userId",
            "accessToken",
            "refresh_token",
            "clientSecret",
            "Authorization",
        ],
    )
    def test_sensitive_keys(self, key: str) -> None:
        assert sanitize({key: "sensitive"})[key] == REDACTED

    def test_case_and_separators_do_not_evade_it(self) -> None:
        for key in ("FIRSTNAME", "first_name", "first-name", "FirstName"):
            assert sanitize({key: "Захар"})[key] == REDACTED

    def test_nested_structures(self) -> None:
        payload = {"profile": {"contacts": [{"phone": "+380..."}, {"email": "a@b.c"}]}}
        cleaned = sanitize(payload)
        assert cleaned["profile"]["contacts"][0]["phone"] == REDACTED
        assert cleaned["profile"]["contacts"][1]["email"] == REDACTED


class TestPreserves:
    def test_product_fields_survive(self) -> None:
        """A fixture stripped of product data would be useless for testing the passes."""
        product = {
            "name": "Молоко Яготинське 2,6%",
            "price": 42.90,
            "barcode": "4820000000000",
            "slug": "moloko-yagotynske",
            "inStock": True,
            "quantity": 2,
        }
        assert sanitize(product) == product

    def test_structure_and_types_are_unchanged(self) -> None:
        payload = {"items": [{"price": 1.5, "qty": 2, "ok": True, "none": None}]}
        assert sanitize(payload) == payload

    def test_empty_containers(self) -> None:
        assert sanitize({}) == {}
        assert sanitize([]) == []

    def test_scalars_pass_through(self) -> None:
        assert sanitize("plain") == "plain"
        assert sanitize(7) == 7
