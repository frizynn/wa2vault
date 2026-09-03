# Documentation

[Project home](../README.md) · [Configuration](configuration.md) ·
[Operations](operations.md) · [Architecture](architecture.md) ·
[Security](../SECURITY.md) · [Changelog](../CHANGELOG.md) ·
[Contributing](../CONTRIBUTING.md)

wa2vault's documentation is split by responsibility so each fact has one
canonical home.

## For users and operators

- Start with the [project README](../README.md) for requirements, installation,
  a minimal two-profile setup, and the core guarantees.
- Use the [configuration reference](configuration.md) for every TOML field,
  environment override, profile rule, and generated path.
- Use the [operations guide](operations.md) for pairing, routine pulls, backups,
  recovery, migration, and troubleshooting.
- Read the [security policy](../SECURITY.md) before reporting a vulnerability or
  handling an accidental data exposure.

## For maintainers

- [Architecture](architecture.md) defines component ownership, invariants, and
  failure semantics.
- [wacli contract](internals/wacli-contract.md) records the exact external
  commands and JSON fields the adapter supports.
- [ADR 0001](adr/0001-local-profile-isolated-archive.md) records the durable
  local archive decision.
- [Contributing](../CONTRIBUTING.md) defines checks, privacy-safe fixtures, and
  compatibility expectations.

## Documentation rules

- Examples must use synthetic names, identifiers, and paths.
- User-visible behavior belongs in the configuration or operations guide;
  implementation contracts belong under `docs/internals/`.
- Design decisions that should survive a refactor belong under `docs/adr/`.
- Update the canonical page and links instead of copying the same reference
  table into several files.
