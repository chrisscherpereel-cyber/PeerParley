# Architecture

```
        ┌──────────────────────── PUBLIC TIER ────────────────────────┐
        │  Streamlit Community Cloud (from GitHub repo)                │
        │                                                             │
        │  app.py  ── shared-password gate (SHA-256)                   │
        │     │                                                       │
        │     ├─ ingest ─ grading ─ pdfgen   (all IN-MEMORY only)      │
        │     │                                                       │
        │     ├─ security.py  ──► Fernet AES encryption of any PII    │
        │     │                                                       │
        │     ├─ vault.py  ── encrypt-then-upload ──┐                 │
        │     └─ email_delivery.py ── Graph/SMTP ──┐│                 │
        └──────────────────────────────────────────┼┼────────────────┘
                                                    ││ HTTPS/443 only
                                    ciphertext .ppx ││ OAuth tokens
                                                    ▼▼
        ┌──────────────────── FIREWALL / UNIVERSITY TIER ─────────────┐
        │                                                             │
        │  Storage vault (choose one):                                │
        │    • Microsoft 365 OneDrive/SharePoint  (NAU tenant)        │
        │    • Dropbox (app folder)                                   │
        │    • pCloud                                                 │
        │  → holds ONLY encrypted bundles; provider sees ciphertext   │
        │                                                             │
        │  Microsoft 365 mailbox (course account)                     │
        │  → sends student emails via Graph                           │
        │                                                             │
        │  Secrets custody:                                           │
        │  → Fernet master key + app registration secrets held        │
        │    university-side (password manager / Entra)               │
        └─────────────────────────────────────────────────────────────┘
```

## Design principles

**Single application.** One Streamlit app (`app.py`) drives the whole workflow
in five tabs. The `peerparley/` package is internal structure, not separate
apps — you deploy and run exactly one process.

**PII never rests in plaintext on the public host.** Uploaded files are parsed
into an in-memory tidy frame. Anything written anywhere durable goes through
`security.encrypt_*` first. The `.gitignore` guarantees data files can't be
committed.

**Encryption is separate from transport.** Even though M365/Dropbox/pCloud all
use TLS in transit, the payload is *also* Fernet-encrypted at the application
layer, so the storage provider never holds decryptable student data. Confiden-
tiality reduces to custody of one Fernet key.

**Pluggable storage.** `vault.py` exposes a 4-method interface
(`put/get/list/delete`) with interchangeable backends, so moving from Dropbox to
your NAU M365 tenant is a secrets change, not a code change.

**Firewall-friendly by protocol.** All egress is HTTPS/443 (Graph, Dropbox,
pCloud REST). The email path uses the OAuth device-code flow specifically so it
works from networks that block SMTP and non-standard ports.

**Privacy validation at the last mile.** `email_delivery.validate_message`
checks that each outgoing message's attachments belong to its recipient before
anything is sent, preventing cross-student leakage.

## Grading model (summary)

```
individual = team_score × clamp(1 + B·A·Q·(peer_ratio − 1), min_mult, max_mult)
```

- `peer_ratio` = student's avg received allocation ÷ fair share (100/(n−1)).
- `A` = agreement weight from SD of received points, banded 10/20/30% →
  1.00/0.75/0.50/0.25.
- `Q` = comment-support score (0–1) from `comments.py` completeness +
  repetition + cross-comment similarity checks.
- `B`, caps, rounding, performance method, and the **agreement guard** (softens
  a forced "Low" when evaluators themselves disagree) are all instructor-set.

See `peerparley/grading.py` and `peerparley/comments.py` for the exact bands.
Swap in your production formula there without touching the rest of the app.
```
