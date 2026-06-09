from __future__ import annotations

import os


class OrchestratorConfig:
    def __init__(
        self,
        state_store_url: str | None = None,
        poll_interval: float | None = None,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        anthropic_api_key: str | None = None,
        crucible_home: str | None = None,
        zathras_home: str | None = None,
    ) -> None:
        self.state_store_url = (
            state_store_url
            or os.environ.get("STATE_STORE_URL", "http://localhost:8090")
        )
        self.poll_interval = (
            poll_interval
            or float(os.environ.get("POLL_INTERVAL", "3.0"))
        )
        self.llm_provider = (
            llm_provider
            or os.environ.get("LLM_PROVIDER", "mock")
        )
        self.llm_model = (
            llm_model
            or os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
        )
        self.anthropic_api_key = (
            anthropic_api_key
            or os.environ.get("ANTHROPIC_API_KEY")
        )
        self.crucible_home = (
            crucible_home
            or os.environ.get("CRUCIBLE_HOME", "/home/atheurer/swdev/repos/crucible")
        )
        self.zathras_home = (
            zathras_home
            or os.environ.get("ZATHRAS_HOME", "")
        )
