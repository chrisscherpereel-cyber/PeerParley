# Uploading this update to your PeerParley repo

Your repo (`chrisscherpereel-cyber/PeerParley`) is the modular app — `app.py`
plus the `peerparley/` package. This update adds the built-in survey (setup +
collection) so the contact list is the only input. See `SURVEY_FEATURE.md` for
what it does.

## Files in this folder

```
PeerParley_github_update/
├── app.py                     # MODIFIED — replaces repo root app.py
├── peerparley/
│   ├── config.py              # MODIFIED — replaces peerparley/config.py
│   ├── tokens.py              # NEW      — add to peerparley/
│   └── survey.py              # NEW      — add to peerparley/
├── SURVEY_FEATURE.md          # optional — documents the feature
└── UPLOAD_TO_GITHUB.md        # this file
```

Only **four** code files change: two replaced (`app.py`, `peerparley/config.py`)
and two new (`peerparley/tokens.py`, `peerparley/survey.py`). `requirements.txt`
does **not** change — no new dependencies.

## Option A — GitHub web upload (matches how you built the repo)

1. Go to the repo → **Add file → Upload files**.
2. Drag in **`app.py`** (drops at the repo root, replacing the old one).
3. To place the `peerparley/` files, either:
   - drag the whole **`peerparley/`** folder from this update in (GitHub keeps
     the folder path, so `config.py` replaces the old one and `tokens.py` /
     `survey.py` are added); **or**
   - open each existing file on GitHub (`peerparley/config.py`) → pencil **Edit**
     → paste the new contents; and use **Add file → Create new file** named
     `peerparley/tokens.py` and `peerparley/survey.py` to add the two new ones.
4. Commit with a message like `Add built-in survey (setup + collection)`.

## Option B — git command line

```bash
git clone https://github.com/chrisscherpereel-cyber/PeerParley.git
cd PeerParley
# copy the four files from this update, preserving paths:
cp /path/to/PeerParley_github_update/app.py .
cp /path/to/PeerParley_github_update/peerparley/config.py peerparley/
cp /path/to/PeerParley_github_update/peerparley/tokens.py peerparley/
cp /path/to/PeerParley_github_update/peerparley/survey.py peerparley/
git add app.py peerparley/config.py peerparley/tokens.py peerparley/survey.py
git commit -m "Add built-in survey (setup + collection)"
git push
```

## After uploading

1. **Secrets (optional).** In the deployed app's *Settings → Secrets* (and your
   local `.streamlit/secrets.toml`) you may add:
   ```toml
   token_secret = "any long random string"
   public_url   = "https://your-app.streamlit.app"
   ```
   Both are optional — links work without them (see `SURVEY_FEATURE.md`).

2. **Use a durable vault backend for real collection.** With `vault.backend =
   "local"`, student responses live only in the ephemeral cloud container. Set it
   to `m365` / `dropbox` / `pcloud` so the student form and instructor console
   share one encrypted store. (Your existing vault credentials already cover
   this — the student form uses the same backend.)

3. Streamlit Cloud redeploys automatically on the new commit. Open the app,
   go to **1 · Survey setup**, upload a contact list, save, send links; watch
   **2 · Responses**; then **Load responses into grading** and continue through
   **Review & PDFs → Email** as usual.

## What was verified

- Token round-trip + tamper/forgery rejection.
- Contact list → survey setup → simulated student submissions (encrypted to a
  real Fernet vault) → `responses → long_df` → the repo's `grading.compute`,
  producing correctly scored teams. Mixed team sizes (4 and 3), self-rows
  excluded, emails resolved for delivery.
- All four changed files byte-compile.
