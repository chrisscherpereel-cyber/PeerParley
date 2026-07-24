"""Encryption helpers.

Every byte of student PII that is cached on the cloud host or written to the
vault is encrypted with a Fernet (AES-128-CBC + HMAC) key held in secrets.
The plaintext key master copy is kept university-side. Losing the key means the
cached/vaulted data is unrecoverable — which is the point.
"""
from __future__ import annotations

import io
import json
from typing import Any

import pandas as pd
from cryptography.fernet import Fernet, InvalidToken

from .config import load_config


class CryptoError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = (load_config().fernet_key or "").strip()
    if not key:
        raise CryptoError(
            "No fernet_key configured. Generate one with:\n"
            "python -c \"from cryptography.fernet import Fernet;"
            "print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"Invalid fernet_key: {exc}") from exc


def encrypt_bytes(data: bytes) -> bytes:
    return _fernet().encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    try:
        return _fernet().decrypt(token)
    except InvalidToken as exc:
        raise CryptoError("Could not decrypt — wrong key or corrupt data.") from exc


def encrypt_json(obj: Any) -> bytes:
    return encrypt_bytes(json.dumps(obj, default=str).encode("utf-8"))


def decrypt_json(token: bytes) -> Any:
    return json.loads(decrypt_bytes(token).decode("utf-8"))


def encrypt_dataframe(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False) if _has_parquet() else df.to_csv(buf, index=False)
    return encrypt_bytes(buf.getvalue())


def decrypt_dataframe(token: bytes) -> pd.DataFrame:
    raw = decrypt_bytes(token)
    buf = io.BytesIO(raw)
    try:
        return pd.read_parquet(buf)
    except Exception:
        buf.seek(0)
        return pd.read_csv(buf)


def _has_parquet() -> bool:
    try:
        import pyarrow  # noqa: F401
        return True
    except Exception:
        return False
