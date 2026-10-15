"""Shared constants. Scenario freshness is measured against a fixed clock."""

from datetime import datetime, timezone

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

STATES = (
    "PROCEED",
    "PROCEED_WITH_GUARDS",
    "NEEDS_REVIEW",
    "HOLD",
    "INSUFFICIENT_EVIDENCE",
)

MODES = ("full", "one_hop", "graph_off")

PRECEDENCE = ("hold", "insufficient", "review", "guard")

STATE_FOR = {
    "hold": "HOLD",
    "insufficient": "INSUFFICIENT_EVIDENCE",
    "review": "NEEDS_REVIEW",
    "guard": "PROCEED_WITH_GUARDS",
}

# Name tokens that mark a payment or auth path. Matched on word boundaries
# so "author" is not treated as "auth", and a real service does not have to
# be named exactly like the old calibration set.
_PAYMENT_OR_AUTH_TOKENS = frozenset(
    {"payment", "payments", "auth", "authentication", "billing", "ledger"}
)


def is_payment_or_auth(name: str) -> bool:
    parts = {part for part in _split_name(name) if part}
    return bool(parts & _PAYMENT_OR_AUTH_TOKENS)


def _split_name(name: str) -> list[str]:
    cleaned = []
    current = []
    for char in name.lower():
        if char.isalnum():
            current.append(char)
        elif current:
            cleaned.append("".join(current))
            current = []
    if current:
        cleaned.append("".join(current))
    return cleaned
