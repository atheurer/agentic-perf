# Crucible Run-File Construction — Learned Pitfalls

These are common mistakes when constructing crucible run files,
discovered through real benchmark runs and user feedback.

## Tags: what they are and are not for

Tags are metadata for information NOT already captured elsewhere
in the run data. Benchmark parameters (message size, thread count,
protocol, duration, etc.) are already stored in the run-file and
searchable in OpenSearch — do NOT duplicate them as tags.

Good tags:
```json
"tags": {
  "study": "throughput-scaling-june2026",
  "hypothesis": "linear scaling to 8 threads",
  "environment": "aws-us-east-2",
  "jira": "PERF-12345"
}
```

Bad tags (redundant with run-file params):
```json
"tags": {
  "benchmark": "uperf",
  "protocol": "tcp",
  "message-size": "16384",
  "thread-counts": "1,8,32",
  "duration": "30",
  "samples": "3"
}
```

Also: tag values must NOT contain commas. Crucible's tag parser
splits on commas, so `"thread-counts": "1,8,32"` causes a parse
error ("format for tag is not valid: 8"). Use dashes or other
separators if you must encode multiple values in one tag.

Keep tags minimal — 2-4 entries that help identify the study or
context, not a summary of the run-file.

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
  "settings": {"user": "root"},
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

## controller-ip-address: omit unless necessary

Do NOT include `controller-ip-address` in endpoint settings
unless you have a specific reason (e.g., the controller has
multiple network paths and crucible picks the wrong one).
Crucible can determine the controller's IP automatically in
most cases. Including a wrong IP (e.g., a libvirt bridge or
OpenStack network IP) will break the run.

If you do need to specify it, use the IP that the endpoints
can reach the controller through — typically the management
network IP. One way to discover this: SSH from the controller
to an endpoint, then check `ss -tn` on either side to see
which source IP was used.
