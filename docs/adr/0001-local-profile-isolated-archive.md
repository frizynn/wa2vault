# ADR 0001: Local profile-isolated SQLite archive

[Project home](../../README.md) · [Documentation](../index.md) ·
[Architecture](../architecture.md)

- Status: Accepted
- Date: 2026-09-03

## Context

wacli exposes a local mirror, but a pull asks for a moving time window. Rendering
only that result makes a shorter or partially synced pull overwrite a more
complete note. Display-name filenames, message-only cache keys, and shared
state also allow personal/work accounts or same-named chats to collide.

The archive contains sensitive, untrusted data and must not require a hosted
service for normal operation. It also needs transactional upserts, efficient
per-chat reads, and a schema that can evolve without coupling persistence to
Markdown formatting.

## Decision

Keep a local SQLite archive partitioned by profile. Identify messages by the
composite source identity `(profile, chat JID, message ID)` and use the same
identity boundary for derived caches and output paths. A pull upserts its recent
window transactionally; rendering reads all accumulated rows for that profile
and chat. Repeating a pull therefore produces the same logical archive, while a
short pull does not delete older rows.

Profiles explicitly select either a named `wacli_account` or an explicit
`wacli_db`, never both. Display names remain presentation metadata and do not
determine identity. Database and output writes are atomic, and subprocesses are
bounded by configuration.

SQLite is a local implementation detail behind an archive-store boundary. The
pipeline depends on that boundary rather than SQL, allowing later migrations or
alternative implementations without changing wacli adapters or renderers.

## Consequences

Benefits:

- cumulative, idempotent pulls;
- transactionally consistent local updates;
- no required cloud database or service credentials;
- collision-resistant personal/work and same-name chat storage;
- straightforward backup of a small number of local artifacts.

Costs and risks:

- the archive database becomes durable sensitive state, not a disposable cache;
- profile mistakes can still expose data to a user with filesystem access;
- schema changes require explicit migrations and compatibility tests;
- deleting the archive can lose messages outside the wacli mirror or next pull
  window;
- SQLite supports concurrent readers but wa2vault must serialize writes and
  avoid long transactions.

## Privacy and operations

Use restrictive file permissions where supported. Never store the archive in
the source repository or publish it as a test artifact. Back up the archive and
rendered vault together, and apply the same encryption, retention, and deletion
policy to both. Chat content remains untrusted even after normalization or
transcription.

## Alternatives considered

- **Render only the current window:** simple, but destructive on shorter or
  partial pulls.
- **Merge Markdown:** couples persistence to presentation and is fragile when
  formatting changes.
- **JSON/JSONL sidecars:** portable, but transactional upserts and indexed reads
  are more complex.
- **Hosted database:** adds credentials, network availability, cost, and a much
  larger privacy boundary without solving a current requirement.
