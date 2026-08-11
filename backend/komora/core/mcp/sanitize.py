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
)


def _is_sensitive(key: str) -> bool:
    normalised = key.replace("_", "").replace("-", "").lower()
    if normalised in _SENSITIVE_KEYS:
        return True
    return any(fragment in normalised for fragment in _SENSITIVE_FRAGMENTS)


def sanitize(value: Any) -> Any:
    """Recursively redact sensitive values, preserving structure and types.

    Only **scalars** are redacted. Personal data is always a leaf — a phone number, a
    street name — never a nested structure. Redacting containers destroyed real data
    the first time this ran: `silpo_find_address`'s JSON Schema has a *property* named
    `address` whose value is a schema object, and it was replaced wholesale.

    A genuinely nested address is still covered: recursion reaches its `street` and
    `house` leaves and redacts those individually.

    Product fields — `name`, `price`, `barcode` — are deliberately kept: a fixture
    without them would be useless for testing the passes.
    """
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if _is_sensitive(str(key)) and not isinstance(item, dict | list)
                else sanitize(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value
