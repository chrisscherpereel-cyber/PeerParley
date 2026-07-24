# Built-in survey — setup & collection

PeerParley can now **run the peer evaluation itself** instead of importing a
Qualtrics export. The instructor uploads only the **contact list** (names,
emails, teams); PeerParley serves each student a personal evaluation form and
turns their answers into the same grading input the rest of the app already
uses. The Qualtrics upload path is still there as a fallback.

## What was added

| File | Change |
|---|---|
| `peerparley/tokens.py` | **new** — signed, tamper-proof student-link tokens (HMAC-SHA256). |
| `peerparley/survey.py` | **new** — survey config, roster snapshot, encrypted storage of the survey/roster/responses in the vault, link generation, `responses → long_df`, and the public student form. |
| `peerparley/config.py` | added optional `token_secret` and `public_url` secrets. |
| `app.py` | student-link interceptor (before the login gate) + two new tabs. |

Nothing in `grading.py`, `pdfgen.py`, `email_delivery.py`, `vault.py`, or
`security.py` changed. No new dependencies — `requirements.txt` is unchanged.

## The two new tabs

**1 · Survey setup** — upload the contact list, edit the survey wording and the
points total, toggle the survey open/closed, and **Save survey + roster to
vault**. Then generate every student's personal link and email them via the
app's existing Microsoft 365 / SMTP mailer (dry-run drafts or live send).

**2 · Responses** — response counts overall and per team, a non-responder list
you can download or email a reminder to, and **Load responses into grading**,
which pulls every submission, builds the tidy grading input, and hands it to the
existing **Review & PDFs** and **Email** tabs. From there everything works
exactly as before.

## How students submit

Each link is `https://<your-app>/?t=<token>`. The token is a signed
`{slug, team, position}`; the app detects it **before** the instructor password
gate and shows that student their form — no login, and no student can edit the
URL to reach anyone else (a bad signature is rejected). Students can reopen their
link to revise until the survey is closed.

## Privacy / storage

Responses are written to the **same encrypted vault** as the rest of the app
(`survey__<slug>.ppj`, `roster__<slug>.ppj`, `resp__<slug>__<team>__p<n>.ppj`),
Fernet-encrypted before they leave the process. This matches the app's existing
single-key model — simpler than a split-key scheme, and consistent with how the
vault already protects PII.

> **Use a real vault backend for collection.** With `backend = "local"` the
> responses live only in the ephemeral Streamlit Cloud container and vanish on
> restart. Set `vault.backend` to `m365` / `dropbox` / `pcloud` so the student
> form and the instructor console share one durable, encrypted store.

## New secrets (both optional)

```toml
# .streamlit/secrets.toml
token_secret = "any long random string"   # signs student links
public_url   = "https://your-app.streamlit.app"   # used to build links
```

`token_secret` is optional — if omitted, a stable secret is derived from your
existing `fernet_key`, so links work out of the box. `public_url` just
pre-fills the link base in the Survey setup tab; you can also type it there.
Changing `token_secret` (or `fernet_key`, if you rely on the fallback)
invalidates links already sent.
