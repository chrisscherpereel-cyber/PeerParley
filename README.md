# PeerParley

**Peer evaluation, made clear.** A single instructor/administrator application
that runs the entire peer-evaluation workflow — ingest → grade → PDF → email —
from one Streamlit app you can deploy on **Streamlit Community Cloud**, while
keeping all student PII **encrypted and stored behind your university's
firewall**.

The public cloud host never persists plaintext student data. Everything
sensitive is AES-encrypted in-process and written only to a
university-controlled storage vault (Microsoft 365, Dropbox, or pCloud).

---

## What it does

One app, five steps (tabs):

1. **Upload & map** — Qualtrics peer-eval export (CSV/XLSX) + contact roster.
   Auto-detects `Q22.1_x` / `Q23.1_x` / `Q24.1` / `Q24.2` columns with a manual
   mapper fallback, normalizes names, runs a data-quality check.
2. **Configure** — sensitivity, multiplier caps, comment points, rounding, and
   the performance method (allocation ratio, composite, rank tiers), with the
   agreement guard on/off.
3. **Review & PDFs** — results table + one-click generation of all four
   deliverables as a zip.
4. **Email** — template editor with live preview; deliver via Microsoft 365
   Graph (OAuth device code, drafts or send) or SMTP, with per-student privacy
   validation.
5. **Vault** — encrypt the working dataset and save/load it to the
   firewall-side storage backend.

### The four deliverables
- Individual anonymous feedback PDF (student-facing)
- Team self-reported contribution PDF (team-facing)
- Instructor section-summary PDF (instructor-only)
- Instructor confidential feedback PDF (instructor-only)

---

## Privacy model (read this)

| Concern | How it's handled |
|---|---|
| App is on a public host | Shared-password gate (SHA-256 hash in secrets). |
| PII on the cloud disk | Never persisted in plaintext. In-session memory only; any cache is Fernet-encrypted. |
| Where PII actually lives | Encrypted `.ppx` bundles in **your** M365 / Dropbox / pCloud folder. |
| Who can decrypt | Only holders of the Fernet key (kept university-side in secrets). |
| Cross-student leakage | Each student PDF is built alone; email delivery validates attachment ↔ recipient before sending. |
| Secrets & data in git | `.gitignore` blocks `secrets.toml`, all CSV/XLSX/PDF, and `vault_cache/`. |

**FERPA note:** with `backend = "m365"` and your NAU tenant, student data stays
under university identity, DLP, and retention governance. See `ARCHITECTURE.md`.

---

## Quick start (local)

```bash
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r requirements.txt

# create secrets
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# generate an app password hash:
python3 -c "import hashlib,getpass;print(hashlib.sha256(getpass.getpass().encode()).hexdigest())"
# generate an encryption key:
python3 -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
# paste both into secrets.toml, choose a vault backend, fill its credentials

python3 -m streamlit run app.py
```

> On macOS, if `pip` and `streamlit` disagree about the interpreter, always use
> `python3 -m pip …` and `python3 -m streamlit run …`.

## Deploy to Streamlit Cloud
See **`DEPLOYMENT.md`** — push this repo to GitHub, point Streamlit Cloud at
`app.py`, and paste your secrets into the app's **Settings → Secrets** box
(never commit them).

---

## Repo layout

```
peerparley/
├── app.py                     # single Streamlit application (entry point)
├── requirements.txt
├── .gitignore                 # blocks secrets + all student data
├── .streamlit/
│   ├── config.toml
│   └── secrets.toml.example   # template — copy, fill, never commit
├── peerparley/
│   ├── config.py              # secrets/env loader
│   ├── auth.py                # shared-password gate
│   ├── security.py            # Fernet encryption
│   ├── vault.py               # M365 / Dropbox / pCloud / local backends
│   ├── ingest.py              # Qualtrics + roster parsing, QA
│   ├── comments.py            # comment-support score Q
│   ├── grading.py             # allocation grading engine
│   ├── pdfgen.py              # branded ReportLab PDFs
│   ├── email_delivery.py      # Graph device-code + SMTP
│   ├── ui_helpers.py          # app-side glue
│   └── branding.py
├── DEPLOYMENT.md
└── ARCHITECTURE.md
```
