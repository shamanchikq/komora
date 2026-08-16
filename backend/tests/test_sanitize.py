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

    def test_a_schema_property_named_address_is_not_destroyed(self) -> None:
        """Regression: this corrupted three captured tool schemas.

        silpo_find_address declares a *property* called `address` whose value is a
        schema object. Redacting by key name alone replaced the whole definition.
        """
        schema = {
            "type": "object",
            "properties": {
                "address": {"type": "string", "description": "Address to geocode"},
                "latitude": {"type": "number"},
            },
            "required": ["address"],
        }
        assert sanitize(schema) == schema

    def test_nested_personal_data_is_still_reached(self) -> None:
        """Only scalars are redacted — but recursion still finds the leaves."""
        cleaned = sanitize({"address": {"street": "вул. Хрещатик", "city": "Київ"}})
        assert cleaned["address"]["street"] == REDACTED
        assert cleaned["address"]["city"] == "Київ", "city alone is not identifying"

    def test_scalar_personal_data_is_still_redacted(self) -> None:
        assert sanitize({"address": "вул. Хрещатик 1"})["address"] == REDACTED


class TestLeavesUnderASensitiveKey:
    """A sensitive key's leaves are its own, however they are wrapped."""

    def test_a_list_of_scalars_is_redacted(self) -> None:
        """The elements have no key of their own, so walking them as ordinary data
        left every one of them in the clear."""
        assert sanitize({"phone": ["+380671111111", "+380672222222"]})["phone"] == [
            REDACTED,
            REDACTED,
        ]

    def test_a_nested_list_is_reached(self) -> None:
        assert sanitize({"address": [["вул. Хрещатик"], ["кв. 5"]]})["address"] == [
            [REDACTED],
            [REDACTED],
        ]

    def test_a_dict_under_a_sensitive_key_keeps_its_structure(self) -> None:
        """The `find_address` schema case: recursion, not wholesale redaction."""
        schema = {"address": {"type": "object", "properties": {"city": {"type": "string"}}}}
        assert sanitize(schema) == schema


class TestKeyNameVariants:
    """Exact matching only ever caught the exact spelling."""

    @pytest.mark.parametrize(
        "key",
        ["phones", "contactPhone", "addresses", "deliveryAddress", "emails", "recipientEmail"],
    )
    def test_plurals_and_compounds_are_caught(self, key: str) -> None:
        assert sanitize({key: "личное"})[key] == REDACTED

    @pytest.mark.parametrize("key", ["warehouseId", "translateTo", "zipperCount", "latest"])
    def test_a_fragment_does_not_swallow_an_innocent_field(self, key: str) -> None:
        """`house`, `lat` and `zip` stay exact-match precisely for these."""
        assert sanitize({key: "keep me"})[key] == "keep me"

    @pytest.mark.parametrize(
        "key",
        ["name", "price", "barcode", "productId", "slug", "ratio", "stock", "oldPrice"],
    )
    def test_every_product_field_survives(self, key: str) -> None:
        assert sanitize({key: "keep me"})[key] == "keep me"
