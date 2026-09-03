# Contributing

[Project home](README.md) · [Documentation](docs/index.md) ·
[Architecture](docs/architecture.md) · [Security](SECURITY.md)

Contributions are welcome. Keep changes small, testable, and compatible with
wa2vault's read-only and local-first guarantees.

## Development setup

```bash
git clone https://github.com/frizynn/wa2vault.git
cd wa2vault
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
```

For documentation changes, also run:

```bash
npx --yes markdownlint-cli2@0.19.0 \
  README.md CHANGELOG.md SECURITY.md CONTRIBUTING.md \
  'docs/**/*.md' 'skills/**/*.md'
```

Run tests without a real WhatsApp account or network dependency. External
commands, time, and filesystem boundaries should be injected or mocked.

## Privacy-safe fixtures

Tests, examples, bug reports, commits, and documentation must use synthetic
content. Never contribute:

- real messages, phone numbers, JIDs, contact or group names;
- voice notes, images, transcripts, EXIF data, or other media metadata;
- wacli stores, wa2vault archive databases, pairing/session material, tokens,
  QR codes, logs containing payloads, or local config files;
- machine-specific home directories, employer/customer names, or private vault
  paths.

Use unmistakably reserved examples such as `Example Group`, `profile-a`, and
non-routable identifiers. Generate tiny fixtures in the test itself where
practical. Before committing, inspect both staged files and staged diffs:

```bash
git status --short
git diff --cached
```

Repository ignore rules reduce accidents but are not a data-loss prevention
system. If sensitive material enters Git history, follow [SECURITY.md](SECURITY.md)
immediately.

## Design and compatibility

- Preserve profile isolation across wacli stores/accounts, contacts, archive
  rows, caches, notes, media, and locks.
- Use stable source identity, not display names, for keys and paths.
- Make writes atomic and repeated pulls idempotent.
- Bound subprocesses with configurable timeouts and return actionable errors.
- Treat every upstream WhatsApp/wacli field as untrusted input.
- Keep adapters around external contracts so wacli or ASR changes do not leak
  through the domain and rendering layers.
- Changes to archive schemas need a forward migration, rollback/recovery notes,
  and tests for existing state.
- Avoid logging payloads or identifiers. Diagnostics should expose counts and
  operation status, not conversation content.

For a durable design choice, add a short ADR under `docs/adr/` and include its
privacy, migration, and failure-mode implications.

Keep documentation facts in their canonical guide: configuration fields in
`docs/configuration.md`, operator workflows in `docs/operations.md`, component
ownership in `docs/architecture.md`, and external payload details under
`docs/internals/`. Update links instead of duplicating reference sections.

## Pull requests

Describe the behavior change, tests run, data/schema compatibility, security or
privacy impact, and documentation updates. Dependency changes should explain
why the new package is necessary. A pull request must not require maintainers to
use a real account to verify it.
