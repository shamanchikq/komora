"""Scrub personal data out of captured Silpo responses before they become fixtures.

Fixtures are committed to a public repository, and the live captures come from a real
Silpo account. Product data must survive intact — that is the whole point of the
fixture — so this redacts by key name rather than by guessing at values.
"""

from typing import Any

REDACTED = "<redacted>"

_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        # identity
        "firstname",
        "lastname",
        "middlename",
        "fullname",
        "patronymic",
        "phone",
        "phonenumber",
        "email",
        "birthdate",
        "dateofbirth",
        "dob",
        "gender",
        # location
        "address",
        "addressline",
        # In Silpo's payloads `locality` is a street address, not a city name. It
        # leaked a real one into a fixture before being added here.
        "locality",
        "couriercomment",
        "courriercomment",  # Silpo's own spelling
        "street",
        "house",
        "apartment",
        "flat",
        "entrance",
        "floor",
        "zip",
        "postcode",
        "latitude",
        "longitude",
        "lat",
        "lon",
        "lng",
        # account and loyalty identifiers
        "userid",
        "clientid",
        "customerid",
        "cardnumber",
        "loyaltycardnumber",
        "personalaccount",
        "accountnumber",
    }
)

_SENSITIVE_FRAGMENTS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "authorization",
    "apikey",
    # Matched as substrings, because the exact list above only ever caught the exact
    # spelling: `phones`, `addresses`, `emails`, `userIds` and `contactPhone` are all
    # ordinary shapes for the same data and every one of them passed straight through
    # into a fixture bound for a public repository.
    #
    # Only terms that cannot appear inside a field worth keeping belong here. No
    # product carries a `phone` or an `email`, so these are safe; `lat`, `zip`,
    # `house` and `floor` are NOT — they live inside `translate`, `zipper` and
    # `warehouseId` — and those stay exact-match above.
    "phone",
    "email",
    "address",
    "birthdate",
    "dateofbirth",
    "firstname",
    "lastname",
    "middlename",
    "fullname",
    "patronymic",
    "cardnumber",
    "loyaltycard",
    "courier",
)


def _is_sensitive(key: str) -> bool:
    normalised = key.replace("_", "").replace("-", "").lower()
    if normalised in _SENSITIVE_KEYS:
        return True
    return any(fragment in normalised for fragment in _SENSITIVE_FRAGMENTS)


def _under_sensitive_key(value: Any) -> Any:
    """The value of a key that names personal data.

    Every scalar it reaches is redacted, however deep the list nesting, because a
    sensitive key's leaves are its own: `{"phones": ["+380…", "+380…"]}` is two phone
    numbers, and walking it as ordinary data left both in a committed fixture — the
    elements have no key of their own for `_is_sensitive` to catch them by.

    A **dict** is handed back to `sanitize`, which is what keeps `silpo_find_address`'s
    JSON Schema intact: its `address` property holds a schema object, and redacting
    that wholesale destroyed real structure the first time this ran.
    """
    if isinstance(value, list):
        return [_under_sensitive_key(item) for item in value]
    if isinstance(value, dict):
        return sanitize(value)
    return REDACTED


def sanitize(value: Any) -> Any:
    """Recursively redact sensitive values, preserving structure and types.

    Only **scalars** are redacted, never a container — see `_under_sensitive_key` for
    the two things that pulls in opposite directions.

    A genuinely nested address is still covered: recursion reaches its `street` and
    `house` leaves and redacts those individually.

    Product fields — `name`, `price`, `barcode` — are deliberately kept: a fixture
    without them would be useless for testing the passes.
    """
    if isinstance(value, dict):
        return {
            key: (_under_sensitive_key(item) if _is_sensitive(str(key)) else sanitize(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value
