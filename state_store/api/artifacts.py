"""Ticket artifact listing and download endpoints."""

from __future__ import annotations

import re
import tarfile
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from paths import ARTIFACT_DIR

router = APIRouter(
    prefix="/tickets/{ticket_id}/artifacts",
    tags=["artifacts"],
)


def _ticket_artifact_dir(ticket_id: str) -> Path:
    """Return the artifact directory for a ticket."""
    # Strict validation: ticket IDs are alphanumeric with hyphens
    if not re.match(r"^[a-zA-Z0-9_-]+$", ticket_id):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid ticket ID: {ticket_id!r}",
        )
    return ARTIFACT_DIR / ticket_id


@router.get("")
def list_artifacts(ticket_id: str):
    """List all artifacts for a ticket.

    Returns a list of files with names, sizes, modification
    times, and paths relative to the ticket artifact directory.
    """
    artifact_dir = _ticket_artifact_dir(ticket_id)
    if not artifact_dir.is_dir():
        return {"ticket_id": ticket_id, "artifacts": []}

    artifacts = []
    for f in sorted(artifact_dir.rglob("*")):
        if f.is_file():
            rel = f.relative_to(artifact_dir)
            stat = f.stat()
            artifacts.append(
                {
                    "path": str(rel),
                    "size_bytes": stat.st_size,
                    "modified": stat.st_mtime,
                    "run": (str(rel.parts[0]) if len(rel.parts) > 1 else ""),
                }
            )

    return {
        "ticket_id": ticket_id,
        "artifact_dir": str(artifact_dir),
        "artifacts": artifacts,
        "total_files": len(artifacts),
        "total_bytes": sum(a["size_bytes"] for a in artifacts),
    }


@router.get("/download/{file_path:path}")
def download_artifact(ticket_id: str, file_path: str):
    """Download a specific artifact file."""
    artifact_dir = _ticket_artifact_dir(ticket_id)
    full_path = artifact_dir / file_path

    # Prevent path traversal
    try:
        full_path.resolve().relative_to(artifact_dir.resolve())
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail="Path traversal denied",
        )

    if not full_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Artifact not found: {file_path}",
        )

    return FileResponse(
        path=str(full_path),
        filename=full_path.name,
        media_type="application/octet-stream",
    )


@router.get("/archive")
def download_archive(ticket_id: str):
    """Download all artifacts as a .tar.gz archive.

    Streams via a temporary file to avoid memory exhaustion
    on tickets with large artifacts.
    """
    artifact_dir = _ticket_artifact_dir(ticket_id)
    if not artifact_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"No artifacts for ticket {ticket_id}",
        )

    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    try:
        with tarfile.open(fileobj=tmp, mode="w:gz") as tar:
            for f in artifact_dir.rglob("*"):
                if f.is_file():
                    arcname = f"{ticket_id}/{f.relative_to(artifact_dir)}"
                    tar.add(str(f), arcname=arcname)
        tmp.close()

        return FileResponse(
            path=tmp.name,
            filename=f"{ticket_id}-artifacts.tar.gz",
            media_type="application/gzip",
            background=BackgroundTask(Path(tmp.name).unlink, missing_ok=True),
        )
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise
