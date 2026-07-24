"""Pluggable, encrypted storage vault (the "behind-the-firewall" tier).

The cloud app never persists plaintext PII. Instead it writes ENCRYPTED blobs
to a university-controlled storage backend chosen in secrets:

    backend = "m365"     -> Microsoft 365 OneDrive / SharePoint (Graph API)
    backend = "dropbox"  -> Dropbox
    backend = "pcloud"   -> pCloud
    backend = "local"    -> local encrypted cache (dev only)

Every backend implements the same tiny interface:
    put(name, data: bytes)      -> str  (path/id)
    get(name)   -> bytes
    list()      -> list[str]
    delete(name)-> None

Data is encrypted with security.encrypt_bytes BEFORE it ever leaves the
process, so the storage provider only ever sees ciphertext.
"""
from __future__ import annotations

import io
import os
from typing import List, Protocol

import requests

from .config import VaultConfig, load_config
from .security import decrypt_bytes, encrypt_bytes


class StorageBackend(Protocol):
    def put(self, name: str, data: bytes) -> str: ...
    def get(self, name: str) -> bytes: ...
    def list(self) -> List[str]: ...
    def delete(self, name: str) -> None: ...


# --------------------------------------------------------------------------- #
# Local encrypted cache (development / air-gapped fallback)
# --------------------------------------------------------------------------- #
class LocalBackend:
    def __init__(self, folder: str):
        self.root = os.path.abspath(os.path.join("vault_cache", folder))
        os.makedirs(self.root, exist_ok=True)

    def _p(self, name: str) -> str:
        return os.path.join(self.root, name)

    def put(self, name: str, data: bytes) -> str:
        with open(self._p(name), "wb") as fh:
            fh.write(data)
        return self._p(name)

    def get(self, name: str) -> bytes:
        with open(self._p(name), "rb") as fh:
            return fh.read()

    def list(self) -> List[str]:
        return sorted(os.listdir(self.root)) if os.path.isdir(self.root) else []

    def delete(self, name: str) -> None:
        try:
            os.remove(self._p(name))
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- #
# Microsoft 365 (OneDrive / SharePoint) via Graph API — confidential client
# --------------------------------------------------------------------------- #
class M365Backend:
    GRAPH = "https://graph.microsoft.com/v1.0"

    def __init__(self, folder: str, opts: dict):
        self.folder = folder.strip("/")
        self.tenant_id = opts["tenant_id"]
        self.client_id = opts["client_id"]
        self.client_secret = opts["client_secret"]
        self.drive = opts.get("drive", "onedrive")
        self.site_id = opts.get("site_id", "")
        self._token = None

    def _get_token(self) -> str:
        if self._token:
            return self._token
        import msal
        app = msal.ConfidentialClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            client_credential=self.client_secret,
        )
        result = app.acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )
        if "access_token" not in result:
            raise RuntimeError(f"M365 auth failed: {result.get('error_description')}")
        self._token = result["access_token"]
        return self._token

    def _drive_root(self) -> str:
        if self.drive == "sharepoint":
            if not self.site_id:
                raise RuntimeError("vault.m365.site_id required for SharePoint drive.")
            return f"{self.GRAPH}/sites/{self.site_id}/drive"
        return f"{self.GRAPH}/me/drive"  # app-only cannot use /me; see note below

    def _headers(self, content=False) -> dict:
        h = {"Authorization": f"Bearer {self._get_token()}"}
        if content:
            h["Content-Type"] = "application/octet-stream"
        return h

    def _item_path(self, name: str) -> str:
        return f"{self.folder}/{name}".strip("/")

    def put(self, name: str, data: bytes) -> str:
        # Simple upload (<4 MB). PUT to /root:/path:/content
        url = f"{self._drive_root()}/root:/{self._item_path(name)}:/content"
        r = requests.put(url, headers=self._headers(content=True), data=data, timeout=60)
        r.raise_for_status()
        return r.json().get("id", name)

    def get(self, name: str) -> bytes:
        url = f"{self._drive_root()}/root:/{self._item_path(name)}:/content"
        r = requests.get(url, headers=self._headers(), timeout=60)
        r.raise_for_status()
        return r.content

    def list(self) -> List[str]:
        url = f"{self._drive_root()}/root:/{self.folder}:/children"
        r = requests.get(url, headers=self._headers(), timeout=60)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return [c["name"] for c in r.json().get("value", [])]

    def delete(self, name: str) -> None:
        url = f"{self._drive_root()}/root:/{self._item_path(name)}:"
        requests.delete(url, headers=self._headers(), timeout=60)


# --------------------------------------------------------------------------- #
# Dropbox
# --------------------------------------------------------------------------- #
class DropboxBackend:
    def __init__(self, folder: str, opts: dict):
        import dropbox
        self.folder = "/" + folder.strip("/")
        if opts.get("refresh_token"):
            self.dbx = dropbox.Dropbox(
                oauth2_refresh_token=opts["refresh_token"],
                app_key=opts.get("app_key"),
                app_secret=opts.get("app_secret"),
            )
        else:
            self.dbx = dropbox.Dropbox(opts["access_token"])

    def _p(self, name: str) -> str:
        return f"{self.folder}/{name}"

    def put(self, name: str, data: bytes) -> str:
        import dropbox
        self.dbx.files_upload(
            data, self._p(name), mode=dropbox.files.WriteMode.overwrite
        )
        return self._p(name)

    def get(self, name: str) -> bytes:
        _md, resp = self.dbx.files_download(self._p(name))
        return resp.content

    def list(self) -> List[str]:
        try:
            res = self.dbx.files_list_folder(self.folder)
            return [e.name for e in res.entries]
        except Exception:
            return []

    def delete(self, name: str) -> None:
        try:
            self.dbx.files_delete_v2(self._p(name))
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# pCloud (simple HTTP API)
# --------------------------------------------------------------------------- #
class PCloudBackend:
    def __init__(self, folder: str, opts: dict):
        region = opts.get("region", "us")
        self.base = "https://eapi.pcloud.com" if region == "eu" else "https://api.pcloud.com"
        self.folder = folder.strip("/")
        self.access_token = opts.get("access_token", "")
        self.username = opts.get("username", "")
        self.password = opts.get("password", "")
        self._auth = None

    def _auth_params(self) -> dict:
        if self.access_token:
            return {"access_token": self.access_token}
        if self._auth:
            return {"auth": self._auth}
        r = requests.get(
            f"{self.base}/userinfo",
            params={"getauth": 1, "username": self.username, "password": self.password},
            timeout=30,
        )
        self._auth = r.json().get("auth", "")
        return {"auth": self._auth}

    def _ensure_folder(self) -> None:
        requests.get(
            f"{self.base}/createfolderifnotexists",
            params={**self._auth_params(), "path": f"/{self.folder}"},
            timeout=30,
        )

    def put(self, name: str, data: bytes) -> str:
        self._ensure_folder()
        r = requests.post(
            f"{self.base}/uploadfile",
            params={**self._auth_params(), "path": f"/{self.folder}", "filename": name, "nopartial": 1},
            files={name: (name, io.BytesIO(data))},
            timeout=120,
        )
        r.raise_for_status()
        return name

    def get(self, name: str) -> bytes:
        link = requests.get(
            f"{self.base}/getfilelink",
            params={**self._auth_params(), "path": f"/{self.folder}/{name}"},
            timeout=30,
        ).json()
        host = link["hosts"][0]
        path = link["path"]
        return requests.get(f"https://{host}{path}", timeout=120).content

    def list(self) -> List[str]:
        r = requests.get(
            f"{self.base}/listfolder",
            params={**self._auth_params(), "path": f"/{self.folder}"},
            timeout=30,
        ).json()
        meta = r.get("metadata", {})
        return [c["name"] for c in meta.get("contents", [])]

    def delete(self, name: str) -> None:
        requests.get(
            f"{self.base}/deletefile",
            params={**self._auth_params(), "path": f"/{self.folder}/{name}"},
            timeout=30,
        )


# --------------------------------------------------------------------------- #
# Factory + encrypted convenience wrapper
# --------------------------------------------------------------------------- #
def _make_backend(cfg: VaultConfig) -> StorageBackend:
    b = cfg.backend.lower()
    if b == "m365":
        return M365Backend(cfg.folder, cfg.options)
    if b == "dropbox":
        return DropboxBackend(cfg.folder, cfg.options)
    if b == "pcloud":
        return PCloudBackend(cfg.folder, cfg.options)
    return LocalBackend(cfg.folder)


class Vault:
    """Encrypt-on-write / decrypt-on-read wrapper around any backend."""

    def __init__(self, cfg: VaultConfig | None = None):
        self.cfg = cfg or load_config().vault
        self.backend = _make_backend(self.cfg)

    @property
    def name(self) -> str:
        return self.cfg.backend

    def put_bytes(self, name: str, data: bytes) -> str:
        return self.backend.put(name, encrypt_bytes(data))

    def get_bytes(self, name: str) -> bytes:
        return decrypt_bytes(self.backend.get(name))

    def list(self) -> List[str]:
        return self.backend.list()

    def delete(self, name: str) -> None:
        self.backend.delete(name)

    def healthcheck(self) -> tuple[bool, str]:
        try:
            self.backend.list()
            return True, f"Vault '{self.cfg.backend}' reachable."
        except Exception as exc:  # noqa: BLE001
            return False, f"Vault '{self.cfg.backend}' error: {exc}"
