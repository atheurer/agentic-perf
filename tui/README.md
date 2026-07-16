# aptui — agentic-perf Terminal UI

A standalone terminal client for agentic-perf that provides real-time
visibility into agent activity, inline HITL interaction, and ticket
lifecycle management.

## Status

**Phase 0 — scaffolding complete.** Not yet functional.

See `docs/tui-implementation-plan.md` for the full roadmap and
`docs/tui-concept.md` for the design rationale.

## Building

```bash
cd tui
make build    # produces ./aptui
make check    # fmt + vet + test
make cross    # static binaries for linux/darwin × amd64/arm64
```

Requires Go 1.23+. Builds with `CGO_ENABLED=0` for static binaries.

## Configuration

Configuration is loaded in order of precedence:

1. Command-line flags (`--url`, `--token`)
2. Environment variables (`AGENTIC_PERF_API_TOKEN`)
3. Config file (`~/.config/aptui/client.toml`)
4. Secrets file (`~/.agentic-perf/secrets/api-token`)

## License

Apache 2.0 — see repository root.
