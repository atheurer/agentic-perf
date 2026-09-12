"""Keep ticket-owned filesystem mutations behind the audited boundary."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
OWNED = (
    "providers/workspace/manager.py",
    "state_store/store.py",
    "state_store/api/artifacts.py",
)
# Direct temporary fallback paths are allowed only when no ticket identity is
# available.  Their exact rationale is documented in the inventory document.
MUTATIONS = {"write_text", "write_bytes"}


def test_ticket_filesystem_inventory_is_checked_and_documented() -> None:
    document = (ROOT / "docs/filesystem-audit-inventory.md").read_text()
    assert "Ticket-owned paths must use `AuditedFilesystem`" in document
    for relative in OWNED:
        source = (ROOT / relative).read_text()
        for mutation in MUTATIONS:
            assert f".{mutation}(" not in source, (
                f"un-audited filesystem mutation in {relative}: {mutation}"
            )
