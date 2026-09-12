"""Inventory guard for direct local subprocess boundaries."""

from __future__ import annotations

from pathlib import Path


def test_subprocess_inventory_is_documented() -> None:
    root = Path(__file__).parents[1]
    inventory = (root / "docs/subprocess-inventory.md").read_text()
    assert "providers/ssh.py" in inventory
    assert "AuditedSubprocessRunner" in inventory
    # Direct SSH transport is #789-owned; this wave permits no other
    # unexplained local process boundary outside the shared runner.
    allowed = {
        "providers/ssh.py",
        "providers/execution/subprocess.py",
        # AWS remaining calls are executable ssh transport (#789).
        "providers/resource/aws.py",
    }
    matches = []
    for path in (root / "providers").rglob("*.py"):
        if str(path.relative_to(root)) in allowed:
            continue
        text = path.read_text()
        if "create_subprocess_exec" in text or "subprocess.run" in text:
            matches.append(str(path.relative_to(root)))
    assert matches == []
