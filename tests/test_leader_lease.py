from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from orchestrator.leader_lease import LeaderLeaseClient


def _http_client(response: MagicMock) -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_lease_lifecycle_uses_untraced_http_client() -> None:
    response = MagicMock(status_code=200, text="")
    response.json.side_effect = [
        {"epoch": 7},
        {"epoch": 7},
        {"released": True},
    ]
    response.raise_for_status = MagicMock()
    client = _http_client(response)
    lease = LeaderLeaseClient(
        "http://state-store",
        instance_name="test-instance",
        session_id=uuid4(),
    )

    with patch("orchestrator.leader_lease.httpx.AsyncClient", return_value=client):
        assert (await lease.acquire())["epoch"] == 7
        assert (await lease.renew())["epoch"] == 7
        assert await lease.release()

    assert client.post.await_count == 3
    response.raise_for_status.assert_called()
