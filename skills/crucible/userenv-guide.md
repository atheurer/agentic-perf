# Crucible Userenv Selection Guide

A userenv (user environment) determines the base container
image used to run benchmark and tool engines. Choosing the
right userenv is critical — a bad choice causes image build
failures or missing benchmark dependencies.

## How userenvs work

Two things must align for a userenv to work:

1. **Rickshaw must define it.** Each userenv has a
   `<name>.json` file in rickshaw's `userenvs/` directory
   that specifies the base container image, OS distro,
   package manager, and core packages. Run `crucible userenvs list`
   on the running controller to see available userenvs.

2. **The benchmark must support it.** Each benchmark has a
   `workshop.json` that declares per-userenv requirements
   (packages, source builds, scripts). The file is at:
   `/opt/crucible/subprojects/benchmarks/<name>/workshop.json`

### How workshop.json resolution works

When crucible builds a container image for a benchmark +
userenv combination, `workshop.py` looks at the benchmark's
workshop.json `userenvs` array:

- If there is an entry whose `name` matches the requested
  userenv, those requirements are used. This means the
  benchmark has **explicit support** for that userenv.
- If there is no explicit match but a `"name": "default"`
  entry exists, those requirements are used as a **fallback**.
  The benchmark will probably work but has not been
  specifically validated for this userenv.
- If neither matches, the build fails.

**"default" is NOT a userenv name.** It is a fallback
category in workshop.json. Setting `"userenv": "default"`
in a run file will fail — there is no `default.json` in
rickshaw's userenvs directory.

## How to choose a userenv

Before constructing a run file, determine the right userenv
by cross-referencing two sources on the controller:

### Step 1: Get available userenvs

Run `crucible userenvs list` on the running controller. This shows all
userenvs defined in rickshaw (official) and any external
userenv repos. Note the "Pull Token" and "Notes" columns:

This controller-side output is authoritative for the run. Never use a
hard-coded or remembered userenv name, and do not infer availability from a
local Rickshaw checkout or benchmark repository. If the command cannot be
run, stop and request assistance rather than constructing a run file.

- `Pull Token: yes` means a registry auth token is needed
- `not CI tested` means crucible CI does not validate this
  userenv — there is lower confidence it works

### Step 2: Check benchmark support

Read the benchmark's workshop.json:
`/opt/crucible/subprojects/benchmarks/<name>/workshop.json`

Look at the `userenvs` array. Each entry has a `name` and
a list of `requirements`. Check whether the userenvs you
are considering have explicit entries or would fall through
to "default".

### Step 3: Pick with confidence ranking

Rank your options by confidence:

1. **High confidence** — the benchmark's workshop.json has
   an explicit entry for this userenv. The benchmark was
   designed and tested with it.

2. **Medium confidence** — the userenv is CI-tested (no
   "not CI tested" note in `crucible userenvs list` output) and
   the benchmark has a `"default"` fallback entry. The
   container image is validated by CI and the benchmark's
   generic requirements should install correctly.

3. **Low confidence** — the userenv is NOT CI-tested and
   would only match the `"default"` fallback. The container
   image may have issues and the benchmark was never
   validated on this OS.

Prefer high-confidence options. If only medium-confidence
options exist, prefer CI-tested userenvs. Avoid
low-confidence options unless the user specifically
requests that OS.

### Common patterns

- **uperf, fio, iperf** — workshop.json only has "default"
  with source builds. Any CI-tested userenv works (medium
  confidence). Good defaults: `stream10`, `fedora-latest`.
- **trafficgen** — workshop.json has explicit `alma8` for
  client (TRex packages). Client MUST use `alma8`. Server
  can use any userenv via "default" fallback.
- **ilab** — uses specialized RHEL AI userenvs that require
  pull tokens.

### Matching user OS preferences

If the user specifies an OS (e.g., "use RHEL9"), map it to
the closest available userenv:

| User says     | Userenv      | Notes                        |
|---------------|--------------|------------------------------|
| RHEL 8        | `rhubi8`     | Red Hat UBI 8                |
| RHEL 9        | `rhubi9`     | Red Hat UBI 9                |
| RHEL 10       | `rhubi10`    | Red Hat UBI 10               |
| CentOS        | `stream10`   | CentOS Stream                |
| Fedora        | `fedora-latest` | Latest Fedora             |
| AlmaLinux     | `alma9`      | Or `alma8` for trafficgen    |

If the user does not specify an OS preference, use a
CI-tested userenv with at least medium confidence. Good
general-purpose defaults are `stream10` or `fedora-latest`.

## Tools also have workshop.json

Tools (sysstat, procstat, etc.) each have their own
workshop.json. They resolve userenvs independently from
the benchmark. Most tools only have a "default" entry and
work with any CI-tested userenv.
