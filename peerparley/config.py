"""Central configuration loader.

Reads from Streamlit secrets when available, otherwise from environment
variables / a local secrets.toml. Nothing sensitive is hard-coded here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


def _secrets() -> Dict[str, Any]:
    try:
        import streamlit as st
        return dict(st.secrets)
    except Exception:
        return {}


def _get(section: Optional[str], key: str, default: Any = None) -> Any:
    s = _secrets()
    if section:
        node = s.get(section, {}) or {}
        if key in node:
            return node[key]
    elif key in s:
        return s[key]
    env_key = (f"{section}_{key}" if section else key).upper()
    return os.environ.get(env_key, default)


@dataclass
class VaultConfig:
    backend: str = "local"
    folder: str = "PeerParley"
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EmailConfig:
    mode: str = "graph"
    sender: str = ""
    smtp_host: str = "smtp.office365.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""


@dataclass
class AppConfig:
    app_password_sha256: str = ""
    fernet_key: str = ""
    # Built-in survey: secret that signs student links, and the public app URL
    # used to build them. token_secret is optional — survey.token_secret()
    # derives a stable fallback from fernet_key when it's blank.
    token_secret: str = ""
    public_url: str = ""
    vault: VaultConfig = field(default_factory=VaultConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    m365: Dict[str, Any] = field(default_factory=dict)


def load_config() -> AppConfig:
    s = _secrets()
    vault_node = s.get("vault", {}) or {}
    backend = vault_node.get("backend", os.environ.get("VAULT_BACKEND", "local"))
    folder = vault_node.get("folder", "PeerParley")
    options = vault_node.get(backend, {}) or {}

    m365 = (s.get("vault", {}) or {}).get("m365", {}) or {}

    email_node = s.get("email", {}) or {}
    email = EmailConfig(
        mode=email_node.get("mode", "graph"),
        sender=email_node.get("sender", ""),
        smtp_host=email_node.get("smtp_host", "smtp.office365.com"),
        smtp_port=int(email_node.get("smtp_port", 587)),
        smtp_username=email_node.get("smtp_username", ""),
        smtp_password=email_node.get("smtp_password", ""),
    )

    return AppConfig(
        app_password_sha256=str(_get(None, "app_password_sha256", "")),
        fernet_key=str(_get(None, "fernet_key", "")),
        token_secret=str(_get(None, "token_secret", "")),
        public_url=str(_get(None, "public_url", "")),
        vault=VaultConfig(backend=backend, folder=folder, options=dict(options)),
        email=email,
        m365=dict(m365),
    )
