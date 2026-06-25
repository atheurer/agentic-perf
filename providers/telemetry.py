"""OpenTelemetry integration for LLM observability.

Instruments the Anthropic and OpenAI SDKs to automatically
capture token usage, model info, and timing as OTLP spans.
A custom span processor bridges span data into the EventBus
for per-ticket accumulation.

Usage:
    from providers.telemetry import setup_telemetry

    event_bus = EventBus()
    setup_telemetry(event_bus=event_bus)

Configuration via ~/.agentic-perf/config.json:
    {
        "telemetry": {
            "enabled": true,
            "otlp_endpoint": "http://localhost:4317"
        }
    }

When otlp_endpoint is set, spans are also exported to an
external OTLP collector (Jaeger, Grafana Tempo, etc.).
When omitted, spans are only processed internally for
EventBus accumulation.
"""

from __future__ import annotations

import logging
from typing import Any

from opentelemetry import context, trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import (
    ReadableSpan,
    SpanProcessor,
    TracerProvider,
)
from opentelemetry.semconv_ai import SpanAttributes

logger = logging.getLogger(__name__)

# Context keys for correlation. Set by the agent loop
# before each LLM call so span processors can attribute
# usage to the right ticket and agent.
_TICKET_ID_KEY = context.create_key("agentic_perf.ticket_id")
_AGENT_NAME_KEY = context.create_key("agentic_perf.agent_name")


def set_ticket_context(
    ticket_id: str,
    agent_name: str = "",
) -> Context:
    """Set ticket and agent in the OpenTelemetry context.

    Call this in the agent loop before making LLM calls so
    that span processors can correlate token usage to the
    ticket and agent.
    """
    ctx = context.get_current()
    ctx = context.set_value(_TICKET_ID_KEY, ticket_id, ctx)
    if agent_name:
        ctx = context.set_value(_AGENT_NAME_KEY, agent_name, ctx)
    return ctx


def get_ticket_from_context(
    ctx: Context | None = None,
) -> str | None:
    """Get the ticket ID from the OpenTelemetry context."""
    ctx = ctx or context.get_current()
    return context.get_value(_TICKET_ID_KEY, ctx)


def get_agent_from_context(
    ctx: Context | None = None,
) -> str | None:
    """Get the agent name from the OpenTelemetry context."""
    ctx = ctx or context.get_current()
    return context.get_value(_AGENT_NAME_KEY, ctx)


class EventBusSpanProcessor(SpanProcessor):
    """Captures completed LLM spans and feeds data to EventBus.

    Extracts token usage, model info, and duration from spans
    produced by the Anthropic/OpenAI instrumentation packages
    and records them as cumulative usage per ticket.
    """

    def __init__(self, event_bus: Any) -> None:
        self._event_bus = event_bus

    def on_start(
        self,
        span: ReadableSpan,
        parent_context: Context | None = None,
    ) -> None:
        """Store ticket and agent on the span."""
        ticket_id = get_ticket_from_context(parent_context)
        agent_name = get_agent_from_context(parent_context)
        if hasattr(span, "set_attribute"):
            if ticket_id:
                span.set_attribute("agentic_perf.ticket_id", ticket_id)
            if agent_name:
                span.set_attribute("agentic_perf.agent_name", agent_name)

    def on_end(self, span: ReadableSpan) -> None:
        """Process completed spans for token accounting."""
        attrs = span.attributes or {}

        # Only process GenAI/LLM spans
        if not attrs.get(SpanAttributes.LLM_REQUEST_MODEL) and not attrs.get(
            "gen_ai.request.model"
        ):
            return

        ticket_id = attrs.get("agentic_perf.ticket_id")
        if not ticket_id:
            return

        # Extract token usage from span attributes
        input_tokens = attrs.get(
            SpanAttributes.LLM_USAGE_PROMPT_TOKENS,
            attrs.get("gen_ai.usage.prompt_tokens", 0),
        )
        output_tokens = attrs.get(
            SpanAttributes.LLM_USAGE_COMPLETION_TOKENS,
            attrs.get("gen_ai.usage.completion_tokens", 0),
        )

        # Calculate duration from span timestamps
        duration_ms = 0
        if span.start_time and span.end_time:
            duration_ms = (span.end_time - span.start_time) // 1_000_000  # ns to ms

        model = attrs.get(
            SpanAttributes.LLM_REQUEST_MODEL,
            attrs.get("gen_ai.request.model", "unknown"),
        )

        agent_name = attrs.get("agentic_perf.agent_name", "")

        # Record in the EventBus
        if hasattr(self._event_bus, "record_llm_usage"):
            self._event_bus.record_llm_usage(
                ticket_id=str(ticket_id),
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                duration_ms=int(duration_ms),
                model=str(model),
                agent_name=str(agent_name),
            )

    def shutdown(self) -> None:
        """No-op — EventBus manages its own lifecycle."""
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """No-op — writes are synchronous."""
        return True


def setup_telemetry(
    event_bus: Any | None = None,
    otlp_endpoint: str | None = None,
    enabled: bool = True,
) -> None:
    """Initialize OpenTelemetry with LLM instrumentation.

    Args:
        event_bus: EventBus instance for per-ticket usage
            accumulation. If None, spans are only exported
            to the OTLP endpoint (if configured).
        otlp_endpoint: OTLP collector endpoint (e.g.,
            http://localhost:4317). If None, no external
            export — spans are only processed internally.
        enabled: Set to False to skip all instrumentation.
    """
    if not enabled:
        logger.info("[telemetry] Disabled by configuration")
        return

    provider = TracerProvider()

    # Internal processor: bridge spans to EventBus
    if event_bus is not None:
        provider.add_span_processor(EventBusSpanProcessor(event_bus))
        logger.info("[telemetry] EventBus span processor enabled")

    # External export: send spans to an OTLP collector
    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace.export import (
                BatchSpanProcessor,
            )

            exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info(f"[telemetry] OTLP export enabled: {otlp_endpoint}")
        except ImportError:
            logger.warning(
                "[telemetry] OTLP exporter not installed. "
                "Install opentelemetry-exporter-otlp-proto-"
                "grpc for external export."
            )

    trace.set_tracer_provider(provider)

    # Instrument the LLM SDKs
    try:
        from opentelemetry.instrumentation.anthropic import (
            AnthropicInstrumentor,
        )

        AnthropicInstrumentor().instrument()
        logger.info("[telemetry] Anthropic SDK instrumented")
    except ImportError:
        logger.debug("[telemetry] Anthropic instrumentation not available")

    try:
        from opentelemetry.instrumentation.openai import (
            OpenAIInstrumentor,
        )

        OpenAIInstrumentor().instrument()
        logger.info("[telemetry] OpenAI SDK instrumented")
    except ImportError:
        logger.debug("[telemetry] OpenAI instrumentation not available")
