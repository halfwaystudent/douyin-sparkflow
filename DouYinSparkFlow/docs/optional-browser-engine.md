# Optional fingerprint browser engine

This feature is disabled by default. With
`SPARKFLOW_FINGERPRINT_BROWSER_ENABLED` unset, empty, `0`, `false`, `no`, or
`off`, every browser launch remains the normal stock Playwright Chromium launch.
In that state the application does not read the engine policy, inspect an
external executable, or create an engine marker in ordinary stock profile
directories.

The optional engine is a configurable external executable dependency. SparkFlow
does not bundle, download, install, or modify a browser binary at runtime.
Enabling it does not change messages, schedules, account identity, dispatch
safety, locks, or platform behavior.

## Server-owned policy

For a known account profile only, set both:

```sh
SPARKFLOW_FINGERPRINT_BROWSER_ENABLED=1
SPARKFLOW_BROWSER_ENGINE_POLICY_FILE=/run/browser-engine/policy.json
```

The policy file path must be absolute and is server-owned: no HTTP request,
client payload, or browser caller supplies an executable path or seed. The gate
accepts only `1/true/yes/on` and the disabled values above; other values fail
closed.

A policy has this exact JSON schema (JSON-compatible YAML is acceptable only
when it is valid JSON):

```json
{
  "schema": 1,
  "fingerprint": {
    "executable_path": "/opt/sparkflow-browser-binaries/fingerprint-150/chrome",
    "sha256": "64-lowercase-hex-characters-for-the-executable"
  },
  "profiles": {
    "uid-42": {"engine": "fingerprint", "seed": 1000},
    "uid-43": {"engine": "stock"}
  }
}
```

Each profile is an explicit canonical account profile name. Unlisted profiles
remain stock; there is no wildcard or global automatic opt-in. Anonymous or
staging workspaces (`profile_name=None`) are always stock, even when the gate is
on. Seeds are stable explicit uint32 integers, never random values or values
derived from dates, record references, or login generations. Multiple record
references for the same physical UID therefore share the one UID policy entry.

The fingerprint executable must be an existing regular executable at a real,
absolute path with no symlink ancestors. Its SHA-256 is calculated from the
**executable**, not from a release archive, and must match the lowercase digest
in the policy. A missing, replaced, non-executable, or digest-mismatched
fingerprint binary fails closed; it never silently falls back to stock or asks
Playwright to install a stock browser. Hashing the executable proves only that
one configured binary matches policy; trust for the complete deployed bundle is
a separate deployment responsibility.

## Durable profile binding

Before Chrome starts, a fingerprint selection binds a fresh persistent profile
to `.sparkflow-browser-engine.json`. The marker records only engine name, seed,
and executable digest; it intentionally contains no filesystem path. The write
is exclusive and atomic. An existing marker must exactly match the selected
seed and binary digest.

A stock launch never creates this marker. A fingerprint-bound directory cannot
be reopened with stock, and a different fingerprint seed or binary cannot reuse
it. A pre-existing Chromium data directory with no engine marker is rejected
with a migration/backup-required error. The only files tolerated in an otherwise
fresh directory are SparkFlow's existing cookie-session and login-pending
metadata files. Invalid, foreign, schema-mismatched, or symlinked markers and
profile paths fail closed.

Existing profile migration is deliberately **not implemented** by this feature.
Back up and migrate existing browser profiles with a later offline migration;
do not point this option at an existing production Chrome profile.

## Container deployment

Use the optional fingerprint compose overlay maintained with the deployment
assets; the base compose configuration stays unchanged. The overlay keeps the
gate defaulted to `${SPARKFLOW_FINGERPRINT_BROWSER_ENABLED:-0}`, mounts the
server-owned policy read-only at `/run/browser-engine`, and mounts browser
binaries read-only at `/opt/sparkflow-browser-binaries`. All relevant roles
must receive the same policy and binary mount so a UID resolves to the same
choice everywhere.

The desktop login service must run a **matching rebuilt login image** that
includes this integration. Merely enabling the environment variable against an
older baked desktop image is not supported and must not be treated as a working
fingerprint login deployment. Desktop and web sides exchange a private engine
expectation containing only engine, seed, and executable digest; a mismatch
fails before network preflight or Chrome start. No executable path is sent in
that payload.
