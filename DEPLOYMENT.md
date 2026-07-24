# Deployment & the behind-the-firewall pieces

This describes exactly what to stand up outside the GitHub repo so the public
Streamlit Cloud app can run "seamlessly" while student data stays behind the
university firewall.

There are **three** things to provision: (1) the GitHub repo + Streamlit Cloud
app, (2) the encrypted storage vault, (3) the Microsoft app registration used
for email (and, optionally, M365 storage).

---

## 1. GitHub + Streamlit Cloud (the public tier)

1. Create a **private** GitHub repo and push this folder.
2. Confirm `.gitignore` is doing its job: no `secrets.toml`, no CSV/XLSX/PDF, no
   `vault_cache/` should ever be committed.
3. On https://share.streamlit.io, create an app from the repo, main file
   `app.py`.
4. Open **Settings → Secrets** and paste the contents of your filled-in
   `secrets.toml` (see `.streamlit/secrets.toml.example`). These live in
   Streamlit's encrypted secret store — not in git.
5. Restrict who can reach the app: set the **shared app password**
   (`app_password_sha256`). Optionally use Streamlit Cloud's viewer allow-list
   to limit to `@nau.edu` accounts.

Generate the two required secrets:

```bash
# app password hash (paste the plaintext when prompted)
python3 -c "import hashlib,getpass;print(hashlib.sha256(getpass.getpass().encode()).hexdigest())"

# Fernet encryption key — KEEP A MASTER COPY UNIVERSITY-SIDE
python3 -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
```

> The Fernet key is the single most important secret. Anyone with it can decrypt
> the vault; anyone without it cannot. Store the master copy in your university
> password manager / secrets vault, not just in Streamlit.

---

## 2. The encrypted storage vault (the firewall tier)

Pick **one** backend in `secrets.toml` under `[vault]`. The app encrypts every
bundle *before* upload, so the provider only ever sees ciphertext.

### Option A — Microsoft 365 / SharePoint / OneDrive  *(recommended for NAU)*
Keeps data inside the NAU tenant, under university identity + DLP.

1. In **Entra ID (Azure AD) → App registrations → New registration**, create
   `PeerParley-Storage`.
2. **API permissions → Microsoft Graph → Application permissions**: add
   `Files.ReadWrite.All` (or `Sites.ReadWrite.All` for a SharePoint site). Click
   **Grant admin consent** (needs an NAU tenant admin).
3. **Certificates & secrets → New client secret**; copy the value.
4. Fill `[vault.m365]`: `tenant_id`, `client_id`, `client_secret`, and either
   `drive = "onedrive"` or `drive = "sharepoint"` + `site_id`.
5. To find a SharePoint `site_id`:
   `GET https://graph.microsoft.com/v1.0/sites/nau.sharepoint.com:/sites/<yoursite>`

> App-only (client-credentials) tokens cannot use `/me/drive`. For OneDrive,
> target a specific user/site drive or use SharePoint. If you prefer per-user
> delegated storage, reuse the device-code login from the email module.

### Option B — Dropbox
1. https://www.dropbox.com/developers/apps → **Create app** → *Scoped access*,
   *App folder* (safest — sandboxes to one folder).
2. Add scopes: `files.content.write`, `files.content.read`, `files.metadata.read`.
3. Generate a **refresh token** (recommended over a short-lived access token).
4. Fill `[vault.dropbox]`: `refresh_token`, `app_key`, `app_secret`.

### Option C — pCloud
1. Create an app at https://docs.pcloud.com to obtain an OAuth access token, or
   use username/password (less preferred).
2. Set `region` (`us` → `api.pcloud.com`, `eu` → `eapi.pcloud.com`).
3. Fill `[vault.pcloud]`: `access_token` (preferred) or `username`/`password`.

### Option D — local (dev only)
`backend = "local"` writes encrypted bundles to `./vault_cache/`. Fine for
testing on your Mac; not for the cloud deployment.

**Access control on the folder:** whichever backend you choose, restrict the
storage folder's sharing to the instructor(s)/TA(s) who should see evaluation
history. The encryption protects the *contents*; folder permissions protect
*who can list/download the ciphertext*.

---

## 3. Microsoft app registration for email

The email module sends via **Microsoft Graph** using the OAuth2
**device-code flow** over HTTPS/443 (firewall-friendly; no SMTP ports needed).

1. Reuse the app registration from step 2A, or create `PeerParley-Mail`.
2. **Authentication → Advanced settings →** enable **Allow public client flows =
   Yes** (required for device code).
3. **API permissions → Delegated**: `Mail.Send`, `Mail.ReadWrite`, `User.Read`;
   grant consent.
4. In `secrets.toml` set `[email] mode = "graph"` and `sender =
   "your-course-account@nau.edu"`. The module reuses `vault.m365.tenant_id` and
   `client_id`.
5. At send time the app shows a code + `microsoft.com/devicelogin` URL; sign in
   as the course account once per session.

**SMTP alternative** (`mode = "smtp"`): requires SMTP AUTH enabled on the
mailbox (many tenants disable it) and an app password. Fill `[email]`
`smtp_host/port/username/password`.

---

## Runbook (each evaluation cycle)

1. Sign in to the app with the shared password.
2. **Upload & map** the Qualtrics export + roster; clear the QA panel.
3. **Configure** the grading method for this cycle.
4. **Review & PDFs** → build the zip; spot-check a student PDF.
5. **Email** → preview → *drafts only* first for a sanity check, then send.
6. **Vault** → encrypt & save the dataset so next cycle / audits can reload it.

## Data-retention & rotation
- Rotate the Fernet key by loading each `.ppx` with the old key and re-saving
  with the new one, then update the secret.
- Delete bundles from the **Vault → Danger zone** when retention lapses.
- Nothing PII persists on Streamlit Cloud between sessions by design.
