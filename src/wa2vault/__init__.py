"""wa2vault: read-only WhatsApp chat -> Obsidian Markdown exporter.

wa2vault is a thin orchestration layer on top of `wacli`
(github.com/openclaw/wacli), an external Go binary that mirrors a linked
WhatsApp Web device into a local SQLite store and downloads media.

wa2vault queries wacli for the last N days of a chat, transcribes voice
notes locally with a pluggable ASR backend, and renders a structured
Markdown "message history" note into an Obsidian vault so that AI agents
can read it.

The tool is strictly READ-ONLY with respect to WhatsApp: it never sends
messages. All wacli invocations run with the read-only guard enabled.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("wa2vault")
except PackageNotFoundError:  # Package not installed (e.g. running from a source tree).
    __version__ = "0.0.0"

__all__ = ["__version__"]
