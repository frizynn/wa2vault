"""Configuration loading for wa2vault.

Configuration lives in a single TOML file under the platform user-config
directory (via :mod:`platformdirs`), e.g. on Linux::

    ~/.config/wa2vault/config.toml

Values can be overridden, in increasing order of precedence:

    1. Built-in defaults (this module).
    2. The TOML config file.
    3. ``WA2VAULT_*`` environment variables.
    4. CLI flags (applied by the CLI layer after :meth:`Config.load`).

:meth:`Config.load` reads the TOML file, creating it with sane defaults the
first time it is missing, and applies environment overrides.

Only the standard-library :mod:`tomllib` is used for reading; the default file
is written with a tiny purpose-built serializer so wa2vault keeps its
dependency surface minimal (no TOML-writer dependency).
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from pathlib import Path
from typing import Literal

import platformdirs
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from wa2vault.fs import atomic_write_text
from wa2vault.identity import profile_key as identity_profile_key

APP_NAME = "wa2vault"
APP_AUTHOR = "wa2vault"

AsrBackend = Literal["faster-whisper"]
_WACLI_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class UnknownProfileError(ValueError):
    """Raised when a requested named profile is absent from the config."""


def default_config_path() -> Path:
    """Return the path to the user config TOML file."""
    return Path(platformdirs.user_config_dir(APP_NAME, APP_AUTHOR)) / "config.toml"


def default_contacts_file() -> Path:
    """Return the path to the local contact-book JSON file.

    Lives in the same directory as :func:`default_config_path`.
    """
    return Path(platformdirs.user_config_dir(APP_NAME, APP_AUTHOR)) / "contacts.json"


def default_cache_dir() -> Path:
    """Return the default cache/work directory (transcripts + scratch)."""
    return Path(platformdirs.user_cache_dir(APP_NAME, APP_AUTHOR))


def default_state_dir() -> Path:
    """Return the durable application-state directory."""
    return Path(platformdirs.user_state_dir(APP_NAME, APP_AUTHOR))


def default_vault_dir() -> Path:
    """Return the default Obsidian vault output directory."""
    return Path.home() / "Obsidian" / "wa2vault"


class ProfileConfig(BaseModel):
    """Optional overrides selected from a ``[profiles.<name>]`` TOML table."""

    model_config = ConfigDict(extra="forbid")

    vault_dir: Path | None = None
    output_subdir: str | None = None
    wacli_db: Path | None = None
    wacli_account: str | None = None
    asr_backend: AsrBackend | None = None
    asr_model: str | None = None
    language: str | None = None
    default_days: int | None = Field(default=None, ge=1)
    sync_timeout: float | None = Field(default=None, gt=0)
    command_timeout: float | None = Field(default=None, gt=0)
    media_timeout: float | None = Field(default=None, gt=0)
    ffmpeg_timeout: float | None = Field(default=None, gt=0)
    cache_dir: Path | None = None
    state_dir: Path | None = None
    contacts_file: Path | None = None
    allow_git_vault: bool | None = None

    @model_validator(mode="after")
    def _exclusive_location(self) -> ProfileConfig:
        if self.wacli_db is not None and self.wacli_account is not None:
            raise ValueError("wacli_db and wacli_account are mutually exclusive")
        return self


class Config(BaseModel):
    """Resolved wa2vault configuration.

    Field defaults match the documented defaults. Paths are stored as absolute
    :class:`~pathlib.Path` objects.
    """

    model_config = ConfigDict(extra="forbid")

    profile: str = Field(
        default="default",
        min_length=1,
        max_length=80,
        description="Logical account/profile name used to isolate all generated state.",
    )
    profiles: dict[str, ProfileConfig] = Field(
        default_factory=dict,
        description="Named personal/work configuration overlays.",
    )

    vault_dir: Path = Field(
        default_factory=default_vault_dir,
        description="Obsidian vault root where exported chat notes are written.",
    )
    output_subdir: str = Field(
        default="Chats",
        description="Subdirectory inside the vault for wa2vault chat notes.",
    )

    @field_validator("output_subdir")
    @classmethod
    def _safe_output_subdir(cls, value: str) -> str:
        path = Path(value)
        if not value.strip() or path.is_absolute() or ".." in path.parts:
            raise ValueError("output_subdir must stay inside vault_dir")
        return value

    wacli_bin: str = Field(
        default="wacli",
        description="wacli executable name or absolute path.",
    )
    wacli_db: Path | None = Field(
        default=None,
        description=(
            "Path to wacli's SQLite store directory. When None, wacli's own "
            "default is used (on Linux the XDG state dir: "
            "~/.local/state/wacli). Set this to point wacli at a custom store."
        ),
    )
    wacli_account: str | None = Field(
        default=None,
        description=(
            "Named wacli account selected with --account. Mutually exclusive with wacli_db/--store."
        ),
    )
    asr_backend: AsrBackend = Field(
        default="faster-whisper",
        description="ASR backend used to transcribe voice notes.",
    )
    asr_model: str = Field(
        default="medium",
        description="ASR model name (e.g. a faster-whisper model size like 'medium').",
    )
    language: str = Field(
        default="es",
        description="Default language hint for transcription (ISO-639-1 code).",
    )
    default_days: int = Field(
        default=30,
        ge=1,
        description="Default number of days of history to pull when --days is omitted.",
    )
    sync_timeout: float | None = Field(
        default=300.0,
        gt=0,
        description=(
            "Max seconds to wait for the best-effort store sync at the start of a "
            "pull. If the sync exceeds this (e.g. a large backlog on a stale "
            "store), it is stopped and the pull proceeds with whatever is already "
            "in the local store. Empty/None falls back to command_timeout."
        ),
    )
    command_timeout: float = Field(
        default=120.0,
        gt=0,
        description="Maximum seconds for ordinary wacli commands.",
    )
    media_timeout: float = Field(
        default=180.0,
        gt=0,
        description="Maximum seconds for one wacli media download.",
    )
    ffmpeg_timeout: float = Field(
        default=120.0,
        gt=0,
        description="Maximum seconds for decoding one audio file.",
    )
    cache_dir: Path = Field(
        default_factory=default_cache_dir,
        description="Directory for the transcript cache and scratch/work files.",
    )
    state_dir: Path = Field(
        default_factory=default_state_dir,
        description="Durable local state directory containing the cumulative archive.",
    )
    contacts_file: Path = Field(
        default_factory=default_contacts_file,
        description=(
            "JSON file mapping a chat JID to a local name, used to override "
            "missing WhatsApp contact names for direct-message chats."
        ),
    )
    allow_git_vault: bool = Field(
        default=False,
        description="Explicitly allow private generated archives inside a Git checkout.",
    )

    @field_validator("profile")
    @classmethod
    def _clean_profile(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("profile must not be blank")
        return cleaned

    @field_validator("wacli_account", mode="before")
    @classmethod
    def _empty_account_is_none(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("wacli_account")
    @classmethod
    def _valid_wacli_account(cls, value: str | None) -> str | None:
        if value is not None and not _WACLI_ACCOUNT_RE.fullmatch(value):
            raise ValueError("wacli_account must use letters, digits, '.', '_' or '-'")
        return value

    @model_validator(mode="after")
    def _exclusive_wacli_location(self) -> Config:
        if self.wacli_db is not None and self.wacli_account is not None:
            raise ValueError("wacli_db and wacli_account are mutually exclusive")
        return self

    @property
    def profile_key(self) -> str:
        """Stable, filesystem-safe namespace for this profile."""
        return identity_profile_key(self.profile)

    @property
    def profile_cache_dir(self) -> Path:
        return (
            self.cache_dir
            if self.profile == "default"
            else self.cache_dir / "profiles" / self.profile_key
        )

    @property
    def profile_state_dir(self) -> Path:
        if self.profile == "default":
            return self.state_dir
        return self.state_dir / "profiles" / self.profile_key

    @property
    def archive_state_dir(self) -> Path:
        """State namespace for this profile and destination vault."""
        vault = str(self.vault_dir.expanduser().resolve(strict=False))
        vault_key = hashlib.sha256(vault.encode("utf-8")).hexdigest()[:12]
        return self.profile_state_dir / "vaults" / vault_key

    @property
    def profile_contacts_file(self) -> Path:
        if self.profile == "default":
            return self.contacts_file
        return self.contacts_file.parent / "profiles" / self.profile_key / self.contacts_file.name

    def select_profile(self, profile: str) -> Config:
        """Apply a named profile's account/store and path overrides."""
        if profile == "default" and profile not in self.profiles:
            return type(self).model_validate({**self.model_dump(), "profile": profile})
        selected = self.profiles.get(profile)
        if selected is None:
            available = ", ".join(sorted(self.profiles)) or "(none)"
            raise UnknownProfileError(
                f"Unknown profile {profile!r}; configured profiles: {available}"
            )
        data = self.model_dump()
        overlay = selected.model_dump(exclude_none=True)
        if "wacli_account" in overlay:
            data["wacli_db"] = None
        if "wacli_db" in overlay:
            data["wacli_account"] = None
        data.update(overlay)
        data["profile"] = profile
        return type(self).model_validate(data)

    @field_validator("wacli_db", mode="before")
    @classmethod
    def _empty_db_path_is_none(cls, value: object) -> object:
        """Treat an empty/whitespace ``wacli_db`` value as None.

        The default TOML file serializes ``wacli_db = None`` as an empty string
        (TOML has no null literal); on load that empty string must round-trip
        back to None rather than becoming ``Path("")``.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("sync_timeout", mode="before")
    @classmethod
    def _blank_timeout_is_none(cls, value: object) -> object:
        """Treat blank ``sync_timeout`` as None (use ``command_timeout``).

        The default TOML file serializes ``sync_timeout = None`` as an empty
        string (TOML has no null literal); on load that must round-trip back to
        None rather than failing float validation.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, config_path: Path | None = None, *, profile: str | None = None) -> Config:
        """Load configuration, creating a default file if missing.

        Args:
            config_path: Explicit path to the TOML config file. Defaults to
                :func:`default_config_path`.
            profile: Optional named profile selected by the CLI. This has
                precedence over ``WA2VAULT_PROFILE`` and the top-level value.

        Returns:
            A fully resolved :class:`Config` with environment overrides applied.
        """
        path = config_path or default_config_path()

        if not path.exists():
            cls._write_default(path)

        with path.open("rb") as fh:
            data = tomllib.load(fh)

        config = cls.model_validate(data)
        selected_profile = profile or os.environ.get("WA2VAULT_PROFILE", config.profile)
        if selected_profile != "default" or selected_profile in config.profiles:
            config = config.select_profile(selected_profile)
        config = config._with_env_overrides()
        return config

    def _with_env_overrides(self) -> Config:
        """Return a copy with ``WA2VAULT_*`` environment overrides applied."""
        overrides: dict[str, object] = {}

        env_str = {
            "WA2VAULT_OUTPUT_SUBDIR": "output_subdir",
            "WA2VAULT_WACLI_BIN": "wacli_bin",
            "WA2VAULT_ASR_BACKEND": "asr_backend",
            "WA2VAULT_ASR_MODEL": "asr_model",
            "WA2VAULT_LANGUAGE": "language",
        }
        for env_key, field in env_str.items():
            value = os.environ.get(env_key)
            if value:
                overrides[field] = value

        env_path = {
            "WA2VAULT_VAULT_DIR": "vault_dir",
            "WA2VAULT_CACHE_DIR": "cache_dir",
            "WA2VAULT_STATE_DIR": "state_dir",
            "WA2VAULT_CONTACTS_FILE": "contacts_file",
        }
        for env_key, field in env_path.items():
            value = os.environ.get(env_key)
            if value:
                overrides[field] = Path(value).expanduser()

        env_account = os.environ.get("WA2VAULT_WACLI_ACCOUNT")
        env_store = os.environ.get("WA2VAULT_WACLI_DB")
        if env_account and env_store:
            raise ValueError("WA2VAULT_WACLI_ACCOUNT and WA2VAULT_WACLI_DB are mutually exclusive")
        if env_account:
            overrides["wacli_account"] = env_account
            overrides["wacli_db"] = None
        elif env_store:
            overrides["wacli_db"] = Path(env_store).expanduser()
            overrides["wacli_account"] = None

        days = os.environ.get("WA2VAULT_DEFAULT_DAYS")
        if days:
            overrides["default_days"] = int(days)

        sync_timeout = os.environ.get("WA2VAULT_SYNC_TIMEOUT")
        if sync_timeout is not None:
            stripped = sync_timeout.strip()
            overrides["sync_timeout"] = (
                None if not stripped or stripped.lower() == "none" else float(stripped)
            )

        for env_key, field in {
            "WA2VAULT_COMMAND_TIMEOUT": "command_timeout",
            "WA2VAULT_MEDIA_TIMEOUT": "media_timeout",
            "WA2VAULT_FFMPEG_TIMEOUT": "ffmpeg_timeout",
        }.items():
            value = os.environ.get(env_key)
            if value:
                overrides[field] = float(value)

        allow_git_vault = os.environ.get("WA2VAULT_ALLOW_GIT_VAULT")
        if allow_git_vault is not None:
            overrides["allow_git_vault"] = allow_git_vault.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }

        if not overrides:
            return self
        return type(self).model_validate({**self.model_dump(), **overrides})

    @classmethod
    def _write_default(cls, path: Path) -> None:
        """Write a default TOML config file at ``path``."""
        defaults = cls()
        atomic_write_text(path, defaults._to_toml())

    def _to_toml(self) -> str:
        """Serialize this config to a flat TOML document.

        A tiny serializer is used deliberately: all fields are scalars
        (strings, ints, or paths/None), so a single flat table suffices and we
        avoid adding a TOML-writer dependency.
        """
        lines = [
            "# wa2vault configuration",
            "# Edit values below; missing keys fall back to built-in defaults.",
            "# Environment variables WA2VAULT_* override these at runtime.",
            "",
        ]
        for name, value in self._toml_items():
            lines.append(f"{name} = {value}")
        return "\n".join(lines) + "\n"

    def _toml_items(self) -> list[tuple[str, str]]:
        """Yield ``(key, toml_literal)`` pairs for serialization."""
        items: list[tuple[str, str]] = [
            ("profile", _toml_str(self.profile)),
            ("vault_dir", _toml_str(str(self.vault_dir))),
            ("output_subdir", _toml_str(self.output_subdir)),
            ("wacli_bin", _toml_str(self.wacli_bin)),
            (
                "wacli_db",
                _toml_str(str(self.wacli_db)) if self.wacli_db is not None else '""',
            ),
            (
                "wacli_account",
                _toml_str(self.wacli_account) if self.wacli_account is not None else '""',
            ),
            ("asr_backend", _toml_str(self.asr_backend)),
            ("asr_model", _toml_str(self.asr_model)),
            ("language", _toml_str(self.language)),
            ("default_days", str(self.default_days)),
            (
                "sync_timeout",
                str(self.sync_timeout) if self.sync_timeout is not None else '""',
            ),
            ("command_timeout", str(self.command_timeout)),
            ("media_timeout", str(self.media_timeout)),
            ("ffmpeg_timeout", str(self.ffmpeg_timeout)),
            ("cache_dir", _toml_str(str(self.cache_dir))),
            ("state_dir", _toml_str(str(self.state_dir))),
            ("contacts_file", _toml_str(str(self.contacts_file))),
            ("allow_git_vault", str(self.allow_git_vault).lower()),
        ]
        return items


def _toml_str(value: str) -> str:
    """Quote a string as a TOML basic string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


__all__ = [
    "Config",
    "ProfileConfig",
    "UnknownProfileError",
    "AsrBackend",
    "default_config_path",
    "default_contacts_file",
    "default_cache_dir",
    "default_state_dir",
    "default_vault_dir",
]
