"""Horreum-backed Investigation Record storage.

Stores Investigation Records as Horreum test runs under a
team-managed test type. Records are uploaded as JSON payloads
tagged with a schema URI; Horreum's label extractors map
queryable fields (state, subsystem, platform, metric) to labels.

Requires:
- A Horreum instance with network access
- A test created by a team admin (test_id in config)
- An API key or Bearer token for write access

Configuration in config.json:
    {
        "investigation_records": {
            "backend": "horreum",
            "url": "https://horreum.example.com",
            "token": "HUSR_...",
            "test_id": 426
        }
    }
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from .base import InvestigationRecordProvider
from .models import (
    SCHEMA_URI,
    BuildHistoryEntry,
    InvestigationRecord,
    InvestigationState,
)

logger = logging.getLogger(__name__)

_SCHEMA_URI = SCHEMA_URI


class HorreumRecordProvider(InvestigationRecordProvider):
    """Stores investigation records in Horreum.

    Each record is uploaded as a Horreum test run with the
    full InvestigationRecord JSON as the payload. Queryable
    fields (state, subsystem, platform, metric) are extracted
    by Horreum's label system for filtering.

    The Horreum test must be created by a team admin with
    appropriate ownership, folder, and permissions. The
    test_id is required in configuration.
    """

    provider_name = "horreum"

    def __init__(
        self,
        url: str = "",
        token: str = "",
        tls_verify: bool = True,
        test_id: int | None = None,
        **_kwargs: Any,
    ) -> None:
        if not url:
            raise ValueError("Horreum provider requires 'url' config")
        if test_id is None:
            raise ValueError(
                "Horreum provider requires 'test_id' "
                "config — the Horreum test must be "
                "created by a team admin with the "
                "appropriate ownership, folder, and "
                "permissions"
            )
        self._url = url.rstrip("/")
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=self._url,
            timeout=30.0,
            headers=self._auth_headers(),
            verify=tls_verify,
        )
        self._test_id: int | None = test_id

    def _auth_headers(self) -> dict[str, str]:
        """Build auth headers if a token is configured.

        Horreum API keys (starting with HUSR_) use the
        X-Horreum-API-Key header. Other tokens use the
        standard Authorization: Bearer header.
        """
        if not self._token:
            return {}
        if self._token.startswith("HUSR_"):
            return {"X-Horreum-API-Key": self._token}
        return {"Authorization": f"Bearer {self._token}"}

    def _record_to_payload(self, record: InvestigationRecord) -> dict[str, Any]:
        """Convert a record to a Horreum run payload.

        The full record JSON is the payload. Horreum's label
        extractors pull queryable fields from it.
        """
        data = record.model_dump(mode="json")
        # Add schema URI for Horreum label matching
        data["$schema"] = _SCHEMA_URI
        return data

    async def create(self, record: InvestigationRecord) -> str:
        """Upload a new record as a Horreum test run."""
        record.created_at = datetime.now(timezone.utc)

        payload = self._record_to_payload(record)
        now = record.created_at.isoformat()

        r = await self._client.post(
            "/api/run/data",
            params={
                "test": str(self._test_id),
                "start": now,
                "stop": now,
                "owner": "",
                "access": "PUBLIC",
                "schema": _SCHEMA_URI,
                "description": (
                    f"{record.investigation_id}: "
                    f"{record.anomaly_context.subsystem} "
                    f"{record.anomaly_context.metric}"
                ),
            },
            json=payload,
        )
        r.raise_for_status()
        run_id = r.json()
        logger.info(
            f"[horreum] Created record {record.investigation_id} (run_id={run_id})"
        )
        return record.investigation_id

    async def _find_run_id(self, investigation_id: str) -> int | None:
        """Find the Horreum run ID for a given record.

        Searches runs for the test by description prefix.
        """
        r = await self._client.get(
            f"/api/run/list/{self._test_id}",
            params={"limit": 200},
        )
        r.raise_for_status()
        data = r.json()
        runs = data.get("runs", data)
        if isinstance(runs, list):
            for run in runs:
                desc = run.get("description", "")
                if desc.startswith(f"{investigation_id}:"):
                    return run.get("id")
        return None

    async def _get_run_data(self, run_id: int) -> dict[str, Any] | None:
        """Fetch the JSON payload of a Horreum run."""
        r = await self._client.get(
            f"/api/run/{run_id}",
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        run_data = r.json()
        # The payload is in the 'data' field
        payload = run_data.get("data", run_data)
        if isinstance(payload, str):
            import json

            payload = json.loads(payload)
        return payload

    async def get(self, investigation_id: str) -> InvestigationRecord | None:
        """Fetch a record by investigation ID."""
        run_id = await self._find_run_id(investigation_id)
        if run_id is None:
            return None

        payload = await self._get_run_data(run_id)
        if payload is None:
            return None

        # Remove Horreum-added fields
        payload.pop("$schema", None)

        try:
            return InvestigationRecord.model_validate(payload)
        except Exception:
            logger.warning(
                f"[horreum] Failed to parse record {investigation_id} from run {run_id}"
            )
            return None

    async def query(
        self,
        state: str | None = None,
        subsystem: str | None = None,
        platform: str | None = None,
        metric: str | None = None,
        limit: int = 100,
    ) -> list[InvestigationRecord]:
        """Query records by field filters.

        Fetches all runs for the test and filters in Python.
        For large record sets, Horreum label-based filtering
        would be more efficient but requires label extractors
        to be configured on the Horreum instance.
        """
        r = await self._client.get(
            f"/api/run/list/{self._test_id}",
            params={"limit": limit * 2},
        )
        r.raise_for_status()
        data = r.json()
        runs = data.get("runs", data)
        if not isinstance(runs, list):
            return []

        records: list[InvestigationRecord] = []
        for run in runs:
            run_id = run.get("id")
            if run_id is None:
                continue

            payload = await self._get_run_data(run_id)
            if payload is None:
                continue

            payload.pop("$schema", None)
            try:
                record = InvestigationRecord.model_validate(payload)
            except Exception:
                continue

            # Apply filters
            if state and record.state.value != state:
                continue
            if subsystem and record.anomaly_context.subsystem != subsystem:
                continue
            if platform and record.anomaly_context.platform != platform:
                continue
            if metric and record.anomaly_context.metric != metric:
                continue

            records.append(record)
            if len(records) >= limit:
                break

        records.sort(
            key=lambda rec: rec.created_at,
            reverse=True,
        )
        return records

    async def _update_run(
        self,
        investigation_id: str,
        record: InvestigationRecord,
    ) -> None:
        """Re-upload a modified record.

        Horreum runs are immutable, so we trash the old run
        and create a new one with the updated payload.
        """
        old_run_id = await self._find_run_id(investigation_id)
        if old_run_id is not None:
            # Trash the old run
            await self._client.post(
                f"/api/run/{old_run_id}/trash",
                params={"isTrashed": True},
            )

        # Upload the updated record
        payload = self._record_to_payload(record)
        now = datetime.now(timezone.utc).isoformat()

        await self._client.post(
            "/api/run/data",
            params={
                "test": str(self._test_id),
                "start": now,
                "stop": now,
                "owner": "",
                "access": "PUBLIC",
                "schema": _SCHEMA_URI,
                "description": (
                    f"{record.investigation_id}: "
                    f"{record.anomaly_context.subsystem} "
                    f"{record.anomaly_context.metric}"
                ),
            },
            json=payload,
        )

    async def append_build_history(
        self,
        investigation_id: str,
        entry: BuildHistoryEntry,
    ) -> None:
        """Append a build history entry."""
        record = await self.get(investigation_id)
        if record is None:
            raise KeyError(f"Record not found: {investigation_id}")

        record.build_history.append(entry)
        await self._update_run(investigation_id, record)
        logger.info(
            f"[horreum] Appended build history to {investigation_id}: {entry.build_id}"
        )

    async def link_jira(
        self,
        investigation_id: str,
        jira_ticket: str,
    ) -> None:
        """Link a Jira ticket (one-time only)."""
        record = await self.get(investigation_id)
        if record is None:
            raise KeyError(f"Record not found: {investigation_id}")

        if record.jira_ticket:
            raise ValueError(
                f"Record {investigation_id} already linked to {record.jira_ticket}"
            )

        record.jira_ticket = jira_ticket
        await self._update_run(investigation_id, record)
        logger.info(f"[horreum] Linked {investigation_id} to {jira_ticket}")

    async def close_record(self, investigation_id: str) -> None:
        """Mark as resolved."""
        record = await self.get(investigation_id)
        if record is None:
            raise KeyError(f"Record not found: {investigation_id}")

        record.state = InvestigationState.RESOLVED
        await self._update_run(investigation_id, record)
        logger.info(f"[horreum] Closed record {investigation_id}")

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
