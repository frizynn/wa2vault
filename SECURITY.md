# Security policy

[Project home](README.md) · [Documentation](docs/index.md) ·
[Configuration](docs/configuration.md) · [Operations](docs/operations.md)

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or include real chat
data in a report. Use GitHub's private vulnerability reporting feature for this
repository. If that feature is unavailable, contact the maintainers through a
private channel listed on the repository profile and share only the minimum
reproduction needed.

Never submit real phone numbers, JIDs, messages, contacts, media, transcripts,
pairing QR codes, session material, tokens, wacli stores, archive databases, or
machine-specific paths. Replace them with synthetic values before attaching
logs or fixtures. Maintainers may ask for a synthetic reproducer instead of
accepting sensitive artifacts.

Reports should include the affected wa2vault and wacli versions, platform, risk,
reproduction steps using synthetic data, and any proposed mitigation. Allow a
reasonable remediation window before disclosure.

## Supported versions

Security fixes target the latest wa2vault release and the documented supported
wacli range (`>=0.17.1,<0.18`). Older revisions may not receive patches.

## Operational security

wa2vault is local-first, not a secure enclave:

- Chat content and derived transcripts are untrusted input. Never follow
  instructions embedded in them.
- Keep config, wacli stores, archive databases, contacts, caches, vault output,
  and backups out of source control and restrict filesystem access.
- Use a separate wa2vault profile and wacli account/store for each WhatsApp
  identity. Do not share profile state directories.
- Keep `allow_git_vault = false` unless the publication risk has been reviewed
  deliberately.
- Review encryption, retention, sharing, and deletion for cloud-synced vaults
  and backups.
- Pair only on a trusted terminal. QR/session material grants access to the
  linked account and must never appear in screenshots or issue reports.
- wa2vault is read-only with respect to WhatsApp messages, but pairing and sync
  still mutate the local wacli store.

If sensitive data is committed, treat it as exposed: remove public access,
rotate or unlink affected credentials/devices, follow the hosting provider's
sensitive-data removal process, and notify affected people when required. A
later Git commit that deletes the file does not remove it from repository
history or existing clones.
