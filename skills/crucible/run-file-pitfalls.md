# Crucible Run-File Construction — Learned Pitfalls

These are common mistakes when constructing crucible run files,
discovered through real benchmark runs and user feedback.

## Engine ID pairing for client-server benchmarks

For benchmarks with client and server roles (uperf, iperf,
trafficgen), the client and server engines must share the
same ID. The benchmark `ids` field should reference that
single ID.

Correct:
```json
"remotes": [
  {"engines": [{"role": "client", "ids": ["1"]}], "config": {"host": "..."}},
  {"engines": [{"role": "server", "ids": ["1"]}], "config": {"host": "..."}}
]
"benchmarks": [{"name": "uperf", "ids": "1", ...}]
```

Wrong:
```json
"remotes": [
  {"engines": [{"role": "client", "ids": ["1"]}], "config": {"host": "..."}},
  {"engines": [{"role": "server", "ids": ["2"]}], "config": {"host": "..."}}
]
"benchmarks": [{"name": "uperf", "ids": "1-2", ...}]
```

The matching ID creates a paired client-server unit. Multiple
pairs use incrementing IDs (pair 1 gets id "1", pair 2 gets
id "2", etc.).

## Multi-pair runs: scope remotehost by ID

When running multiple client-server pairs in parallel (e.g.,
RHEL 9 pair as ID 1 and RHEL 10 pair as ID 2), the
`remotehost` param in mv-params MUST include an `"id"` field.
Without it, every client connects to every server, causing
cross-pair connection failures.

### Example: two OS pairs compared in parallel

Given these endpoints (two client-server pairs, each with
its own ID):

```json
"endpoints": [{
  "type": "remotehosts",
  "settings": {"user": "root", "controller-ip-address": "172.31.15.4"},
  "remotes": [
    {"engines": [{"role": "client", "ids": ["1"]}], "config": {"host": "172.31.7.247"}},
    {"engines": [{"role": "server", "ids": ["1"]}], "config": {"host": "172.31.13.125"}},
    {"engines": [{"role": "client", "ids": ["2"]}], "config": {"host": "172.31.2.99"}},
    {"engines": [{"role": "server", "ids": ["2"]}], "config": {"host": "172.31.15.205"}}
  ]
}]
```

**WRONG** — two separate sets without `"id"` on remotehost.
Both clients run both sets, so client-2 tries to connect to
the ID 1 server (172.31.13.125) and fails:

```json
"benchmarks": [{
  "name": "uperf", "ids": "1+2",
  "mv-params": {
    "sets": [
      {
        "params": [
          {"arg": "test-type", "vals": ["stream"], "role": "client"},
          {"arg": "protocol", "vals": ["tcp"], "role": "client"},
          {"arg": "duration", "vals": ["60"], "role": "client"},
          {"arg": "wsize", "vals": ["256", "1024", "16384"], "role": "client"},
          {"arg": "nthreads", "vals": ["1", "4", "8", "32"], "role": "client"},
          {"arg": "remotehost", "vals": ["172.31.13.125"], "role": "client"}
        ]
      },
      {
        "params": [
          {"arg": "test-type", "vals": ["stream"], "role": "client"},
          {"arg": "protocol", "vals": ["tcp"], "role": "client"},
          {"arg": "duration", "vals": ["60"], "role": "client"},
          {"arg": "wsize", "vals": ["256", "1024", "16384"], "role": "client"},
          {"arg": "nthreads", "vals": ["1", "4", "8", "32"], "role": "client"},
          {"arg": "remotehost", "vals": ["172.31.15.205"], "role": "client"}
        ]
      }
    ]
  }
}]
```

**CORRECT** — single set, remotehost scoped by `"id"`. Each
client only connects to its own paired server:

```json
"benchmarks": [{
  "name": "uperf", "ids": "1+2",
  "mv-params": {
    "sets": [{
      "params": [
        {"arg": "test-type", "vals": ["stream"], "role": "client"},
        {"arg": "protocol", "vals": ["tcp"], "role": "client"},
        {"arg": "duration", "vals": ["60"], "role": "client"},
        {"arg": "wsize", "vals": ["256", "1024", "16384"], "role": "client"},
        {"arg": "nthreads", "vals": ["1", "4", "8", "32"], "role": "client"},
        {"arg": "remotehost", "vals": ["172.31.13.125"], "role": "client", "id": "1"},
        {"arg": "remotehost", "vals": ["172.31.15.205"], "role": "client", "id": "2"}
      ]
    }]
  }
}]
```

The `"id"` field on remotehost scopes that param to only the
engines with that ID. All other params (wsize, nthreads, etc.)
without an `"id"` field apply to all IDs. This keeps them in
a single set — no duplication of common params.

## Always include tool-params

Even when the docs say tool-params is optional, always include
at least a basic set. An empty or missing tool-params section
can cause crucible to write an empty JSON file that fails to
parse on read-back.

Minimum:
```json
"tool-params": [
  {"tool": "sysstat"},
  {"tool": "procstat"}
]
```

## mv-params is mandatory

Every benchmark object in the `benchmarks` array MUST include
an `mv-params` key — the schema requires it. This is where you
define what the benchmark actually does (test type, message sizes,
duration, etc.).

Use `get_benchmark_params` to discover valid parameters and
presets for each benchmark. At minimum:

```json
"benchmarks": [
  {
    "name": "uperf",
    "ids": "1",
    "mv-params": {
      "sets": [
        {
          "params": [
            {"arg": "test-type", "vals": ["stream"], "role": "client"},
            {"arg": "protocol", "vals": ["tcp"], "role": "client"},
            {"arg": "wsize", "vals": ["16384"], "role": "client"},
            {"arg": "duration", "vals": ["60"], "role": "client"},
            {"arg": "nthreads", "vals": ["1"], "role": "client"},
            {"arg": "remotehost", "vals": ["server-host"], "role": "client"}
          ]
        }
      ]
    }
  }
]
```

For benchmarks with global-options, you can define named param
groups and reference them from sets via `include`:

```json
"mv-params": {
  "global-options": [
    {"name": "common", "params": [...]}
  ],
  "sets": [
    {"include": "common", "params": [...additional per-set...]}
  ]
}
```

## "Use IPs not hostnames" scope

The pitfall about using IP addresses instead of hostnames
applies specifically to the endpoint `host` field in the
`remotes` config section. This is because SSH to hostnames
can trigger IPv6 link-local resolution, causing timeouts.

This rule does NOT apply to benchmark parameters like
`remotehost` in mv-params — hostnames work fine there because
the benchmark itself resolves them within the container.
