"""Credential-safe provider configuration for GenericAgent."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Mapping


_API_MODES = {
    "chat_completions": "chat_completions",
    "chat-completions": "chat_completions",
    "responses": "responses",
    "response": "responses",
    "messages": "messages",
    "anthropic": "messages",
}


def normalize_api_mode(value: str) -> str:
    mode = str(value or "chat_completions").strip().lower()
    try:
        return _API_MODES[mode]
    except KeyError as exc:
        raise ValueError(f"unsupported provider api_mode: {value!r}") from exc


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api_base: str
    model: str
    api_mode: str
    credential_env: str
    api_key: str = field(repr=False)
    session_type: str = "native_oai"
    capabilities: frozenset[str] = frozenset({"text", "tools"})
    connect_timeout: int = 5
    read_timeout: int = 120
    proxy: str = field(default="", repr=False)

    @classmethod
    def from_env(
        cls,
        *,
        name: str,
        prefix: str,
        default_api_base: str,
        default_model: str,
        default_api_mode: str = "chat_completions",
        capabilities: frozenset[str] = frozenset({"text", "tools"}),
        session_type: str = "native_oai",
        environ: Mapping[str, str] | None = None,
    ) -> "ProviderConfig":
        env = os.environ if environ is None else environ
        credential_env = f"{prefix}_API_KEY"
        api_key = str(env.get(credential_env, "")).strip()
        if not api_key:
            raise RuntimeError(f"required credential environment variable is missing: {credential_env}")
        return cls(
            name=name,
            api_base=str(env.get(f"{prefix}_API_BASE", default_api_base)).rstrip("/"),
            model=str(env.get(f"{prefix}_MODEL", default_model)),
            api_mode=normalize_api_mode(env.get(f"{prefix}_API_MODE", default_api_mode)),
            credential_env=credential_env,
            api_key=api_key,
            session_type=session_type,
            capabilities=frozenset(capabilities),
            connect_timeout=max(1, int(env.get(f"{prefix}_CONNECT_TIMEOUT", "5"))),
            read_timeout=max(5, int(env.get(f"{prefix}_READ_TIMEOUT", "120"))),
            proxy=str(env.get("GENERICAGENT_PROXY", "")),
        )

    def to_legacy_dict(self) -> dict:
        return {
            "name": self.name,
            "apikey": self.api_key,
            "apibase": self.api_base,
            "model": self.model,
            "api_mode": self.api_mode,
            "proxy": self.proxy,
            "timeout": self.connect_timeout,
            "read_timeout": self.read_timeout,
            "capabilities": sorted(self.capabilities),
            "credential_env": self.credential_env,
            "session_type": self.session_type,
        }


def normalize_provider_config(value: ProviderConfig | Mapping) -> dict:
    """Return the legacy transport mapping without weakening validation."""
    if isinstance(value, ProviderConfig):
        return value.to_legacy_dict()
    cfg = dict(value)
    cfg["api_mode"] = normalize_api_mode(cfg.get("api_mode", "chat_completions"))
    if not str(cfg.get("apikey", "")).strip():
        env_name = str(cfg.get("credential_env", "")).strip()
        if env_name:
            cfg["apikey"] = os.environ.get(env_name, "")
    if not str(cfg.get("apikey", "")).strip():
        raise RuntimeError("provider credential is missing")
    return cfg
