"""Product identity for the Syzygy CLI and human-facing reports."""

from __future__ import annotations

PRODUCT = "Syzygy"
PRODUCT_LONG = "Syzygy EDL"
TAGLINE = "Engineering Decision Layer — graph-backed change decisions on FalkorDB"
VERSION = "0.2.0"


def banner() -> str:
    return (
        f"{PRODUCT_LONG}  v{VERSION}\n"
        f"{TAGLINE}\n"
        "────────────────────────────────────────────────────────────"
    )


def line(title: str) -> str:
    return f"\n▸ {title}"
