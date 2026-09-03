# Changelog

[Project home](README.md) · [Documentation](docs/index.md) ·
[Operations](docs/operations.md)

Notable changes are recorded here using
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories. Releases
follow semantic versioning.

## Unreleased

### Added

- Named, isolated personal/work profiles with wacli account or store selection.
- Durable cumulative SQLite archives and composite transcript-cache identities.
- Atomic/private file helpers, subprocess timeouts, and Git-worktree output
  protection.
- Python 3.12–3.14 CI, coverage, package smoke tests, CodeQL, Gitleaks,
  Dependabot, and Markdown linting.
- Architecture, configuration, operations, security, contribution, and wacli
  contract documentation.

### Changed

- Notes and media now use collision-resistant profile/chat/message paths.
- Chat and transcript content is explicitly rendered as untrusted data.
- wacli support now targets `>=0.17.1,<0.18` and named `--account` profiles.
- wacli command construction now has one canonical implementation.

### Removed

- The selectable but unimplemented `nemotron` backend.
- Silent creation of unconfigured profile namespaces.

### Migration

Legacy slug-only notes are preserved and are not moved automatically. Follow
the [migration runbook](docs/operations.md#migration-from-legacy-slug-only-output)
before deleting old output or archive state.

## 0.1.0

- Initial read-only chat export, local faster-whisper transcription, media
  handling, Markdown rendering, contact aliases, and wacli store locking.
