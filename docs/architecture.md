# Architecture

[Project home](../README.md) · [Documentation](index.md) ·
[Configuration](configuration.md) · [ADR 0001](adr/0001-local-profile-isolated-archive.md)

wa2vault is a local CLI pipeline with explicit adapters at external and durable
state boundaries. It intentionally has no hosted service, message-sending API,
or background daemon.

## Components

| Module | Owns | Must not own |
| --- | --- | --- |
| `cli.py` | Typer commands, terminal presentation, exit codes | wacli argv/env construction or archive rules |
| `config.py` | Defaults, TOML/env loading, profile resolution, path namespaces | subprocess execution |
| `wacli.py` | The complete wacli process and JSON boundary | Markdown or durable archive policy |
| `pipeline.py` | Ordered pull orchestration and best-effort warnings | low-level SQL or subprocess construction |
| `archive.py` | Transactional cumulative message persistence | presentation |
| `transcribe/` | ASR contract, implementation, and composite cache | chat resolution |
| `render.py` | Deterministic, inert Markdown representation | raw wacli parsing |
| `identity.py` | Stable filesystem-safe profile/chat/media identities | I/O |
| `fs.py` | Atomic/private writes and Git-vault safety | domain behavior |
| `contacts.py` | Local aliases | WhatsApp synchronization |

## Pull data flow

```text
Config.select_profile
        |
        v
WacliClient sync/resolve/export
        |
        v
normalized MessageRecord list
        |
        +--> media cache/copy
        +--> transcript cache/local ASR
        |
        v
ArchiveStore transactional upsert
        |
        v
complete profile+chat timeline
        |
        v
untrusted-data Markdown render --> atomic vault write
```

## Invariants

1. **Never send:** the adapter exposes sync, reads, group refresh, and media
   download only. Read operations enable both wacli read-only mechanisms.
2. **One selected profile:** unknown profiles fail before external work. Account
   and explicit store selection are mutually exclusive.
3. **Stable source identity:** display names are presentation metadata. Durable
   identity is profile + chat JID + message ID.
4. **Monotonic archive:** a pull upserts observed messages and never deletes
   unseen history because a moving window became shorter.
5. **No raw payload persistence:** normalized fields enter the archive; the
   adapter's ungoverned `raw` object does not.
6. **Inert output:** every message is delimited and Markdown-significant text is
   neutralized. Consumers must still treat all content as untrusted data.
7. **Bounded external work:** every non-interactive subprocess has a configured
   timeout. Pairing remains interactive.
8. **Private output boundary:** Git-worktree output is refused by default and
   writes use restrictive permissions where the platform supports them.

## Failure and atomicity model

Sync, individual media downloads, and individual transcriptions are
best-effort; they produce warnings so useful local history can still render.
Chat resolution, archive corruption/version incompatibility, and unsafe output
paths fail the pull.

SQLite upsert commits before the note is replaced. This ordering deliberately
keeps the durable source ahead of its derived Markdown projection: if rendering
or replacement fails, the next pull can regenerate the note. Filesystem and
SQLite commits cannot form one portable atomic transaction, so the reverse
ordering would risk a note containing data absent from the durable archive.

Notes, config, contacts, and copied media use a same-directory temporary file,
flush, `fsync`, and `os.replace`. SQLite uses a transaction, WAL, a busy timeout,
and full synchronous durability.

## Scaling boundary

The design targets one machine and many local messages, not a multi-node
service. SQLite provides indexed per-chat timelines and concurrent readers;
wacli remains single-writer. The archive boundary can be replaced later without
changing CLI, wacli parsing, or rendering, but a hosted database would add
credentials and a larger privacy boundary without solving a current need.

The near-term scaling constraints are media/ASR cost and full-timeline Markdown
rendering. Optimize those with measured workloads before adding concurrency:
wacli store access and local model memory make naive parallelism risky.
