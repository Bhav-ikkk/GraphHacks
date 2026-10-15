"""A throwaway tree built inside a test. Nothing here is shipped as sample data."""

from pathlib import Path


def write_compose_repo(root: Path) -> Path:
    (root / "docker-compose.yml").write_text(
        "services:\n"
        "  checkout:\n"
        "    image: scratch\n"
        "    depends_on:\n"
        "      - payments\n"
        "  payments:\n"
        "    image: scratch\n"
        "  search:\n"
        "    image: scratch\n",
        encoding="utf-8",
    )
    owners = root / ".github"
    owners.mkdir(parents=True)
    (owners / "CODEOWNERS").write_text("* @acme/platform\n/payments/ @acme/payments\n", encoding="utf-8")
    workflows = owners / "workflows"
    workflows.mkdir()
    (workflows / "ci.yml").write_text("name: ci\non: [push]\n", encoding="utf-8")
    payments = root / "payments"
    payments.mkdir()
    (payments / "charge.proto").write_text(
        'syntax = "proto3";\n\nservice Payments {\n  rpc Charge(ChargeRequest) returns (ChargeResponse);\n}\n',
        encoding="utf-8",
    )
    (payments / "charge.py").write_text("def charge(amount):\n    return amount\n", encoding="utf-8")
    checkout = root / "checkout"
    checkout.mkdir()
    (checkout / "checkout.py").write_text("def place_order(cart):\n    return cart\n", encoding="utf-8")
    (checkout / "test_checkout.py").write_text("def test_place_order():\n    assert True\n", encoding="utf-8")
    search = root / "search"
    search.mkdir()
    (search / "search.py").write_text("def lookup(query):\n    return [query]\n", encoding="utf-8")
    return root
