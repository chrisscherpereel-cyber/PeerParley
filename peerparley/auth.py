"""Login gate for the instructor/administrator app.

Now multi-user: each instructor signs in with their own username and password
(stored as salted PBKDF2 hashes in the vault via `accounts.py`). The app's
original shared password still works — it signs you in as a built-in admin
called `admin`, so existing deployments keep working and you can never be
locked out. All real PII still lives encrypted in the firewall-side vault.
"""
from __future__ import annotations

import streamlit as st

from . import accounts
from .config import load_config


def current_user():
    return st.session_state.get("pp_user")


def require_login() -> bool:
    """Render the sign-in (and forced first-password change). True once in."""
    cfg = load_config()
    user = st.session_state.get("pp_user")

    if user and user.get("must_change"):
        return _force_password_change(user)
    if user:
        return True

    st.markdown("#### 🔐 Instructor sign-in")
    expected = (cfg.app_password_sha256 or "").strip().lower()
    if not expected and not accounts.load_accounts():
        st.error(
            "No accounts and no app password configured. Set `app_password_sha256` "
            "in secrets, sign in as **admin**, then add instructors.\n\n"
            "`python -c \"import hashlib,getpass;"
            "print(hashlib.sha256(getpass.getpass().encode()).hexdigest())\"`"
        )
        return False

    with st.form("pp_login", clear_on_submit=False):
        username = st.text_input("Username", value="admin")
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")

    if submitted:
        u = accounts.authenticate(username, pw, cfg)
        if u:
            st.session_state["pp_user"] = u
            st.rerun()
        else:
            st.error("Wrong username or password (or the account is deactivated).")
    st.caption("First time? Sign in as **admin** with the app password, then add "
               "instructors from the admin panel in the sidebar.")
    return False


def _force_password_change(user) -> bool:
    st.subheader("Choose your password")
    st.info(f"Welcome, {user.get('name')}. You signed in with a temporary password — "
            "pick your own before continuing. Nobody else will know it.")
    n1 = st.text_input("New password", type="password")
    n2 = st.text_input("Confirm", type="password")
    if st.button("Set password", type="primary"):
        if n1 != n2:
            st.error("Those don't match.")
        elif len(n1) < 8:
            st.error("Use at least 8 characters.")
        else:
            try:
                accounts.set_password(user["user"], n1, must_change=False)
                st.session_state["pp_user"] = {**user, "must_change": False}
                st.success("Done.")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))
    if st.button("Sign out"):
        logout(); st.rerun()
    return False


def logout() -> None:
    for k in list(st.session_state.keys()):
        if str(k).startswith("pp_"):
            del st.session_state[k]
