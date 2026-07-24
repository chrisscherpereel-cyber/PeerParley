"""Shared-password gate for the instructor/administrator app.

A single SHA-256 password hash is stored in secrets. The plaintext password
is never stored anywhere. This is intentionally lightweight — it keeps the
public Streamlit Cloud deployment from being world-open. All actual PII lives
encrypted in the firewall-side vault, not on the cloud host.
"""
from __future__ import annotations

import hashlib
import hmac

import streamlit as st

from .config import load_config


def _hash(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def require_login() -> bool:
    """Render a password gate. Returns True once authenticated."""
    cfg = load_config()

    if st.session_state.get("pp_authenticated"):
        return True

    expected = (cfg.app_password_sha256 or "").strip().lower()

    st.markdown("#### 🔐 Instructor sign-in")
    if not expected:
        st.error(
            "No app password configured. Set `app_password_sha256` in secrets. "
            "Generate it with:\n\n"
            "`python -c \"import hashlib,getpass;"
            "print(hashlib.sha256(getpass.getpass().encode()).hexdigest())\"`"
        )
        return False

    with st.form("pp_login", clear_on_submit=False):
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")

    if submitted:
        if hmac.compare_digest(_hash(pw), expected):
            st.session_state["pp_authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False


def logout() -> None:
    for k in list(st.session_state.keys()):
        if str(k).startswith("pp_"):
            del st.session_state[k]
