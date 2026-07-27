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
| `peerparley/survey.py` | **new** — survey config + open/close schedule, roster snapshot with **owner**, encrypted storage of survey/roster/responses, link generation, `responses → long_df`, survey listing + access checks, and the public student form. |
| `peerparley/accounts.py` | **new** — multi-instructor accounts (PBKDF2 hashes in the vault), roles, and a shared-password break-glass admin. |
| `peerparley/auth.py` | **modified** — per-user username/password login (replaces the single shared-password gate; the old password still works as admin). |
| `peerparley/config.py` | **modified** — added optional `token_secret` and `public_url`. |
| `peerparley/grading.py` | **modified** — rating-matrix dimension grades, forced-ranking performance, pay grade, and self-evaluation (full `PeerParley_V2_3` parity); unchanged for allocation-only data. |
| `peerparley/pdfgen.py` | **modified** — V2_3-style student PDF (respects report toggles), landscape instructor summary with confidential comments, and a cross-round comparison PDF. |
| `peerparley/ui_helpers.py` | **modified** — `build_messages` passes the instructor's report display settings into each student PDF. |
| `app.py` | **modified** — student-link interceptor, two new tabs, user-aware sidebar with a survey picker + admin user panel, ownership enforcement, delivery-method pickers, and CSV downloads. |

Nothing in `grading.py`, `pdfgen.py`, `email_delivery.py`, `vault.py`, or
`security.py` changed. No new dependencies — `requirements.txt` is unchanged.

## The survey (matches the Qualtrics instrument)

The student form is modelled on the MGT 301 peer evaluation, block for block, and
every student evaluates **each teammate and themselves**:

1. **Header** — name, class, section, team, and the list of members being evaluated.
2. **Rating matrix** — four statements on a 7-point *Strongly agree → Strongly
   disagree* scale (team player · did their share · quality of work · team
   performed well because of them).
3. **Qualitative** — two anonymous questions per member: how to *increase* their
   contribution, and their *most significant* contribution.
4. **Forced ranking** — each member into High / Adequate / Low performer (every
   category used at least once).
5. **Pay allocation** — split $100 across the team.
6. **Your contribution** — free text, released to the team anonymously.
7. **Confidential note** — to the instructor, never released.

**The instructor turns any block on or off** in Survey setup → *Questions*, and
edits every prompt and the four statements in → *Wording*. Nothing is
pre-selected on the rating/ranking questions, so an unanswered item is caught
rather than silently recorded as neutral.

**Fast, self-checking form.** Ratings and the forced ranking use **radio buttons**
(not dropdowns), and the form validates **as the student answers** — a live
running total on the $100 allocation, inline "rank everyone / use every category"
checks, and a count of unanswered ratings. The **Submit** button stays disabled,
with a clear checklist of what's left, until everything required is complete, so
students can't submit an incomplete or mis-totalled evaluation.

**Full workbook parity.** The engine reproduces the original `PeerParley_V2_3`
calculations:

- **Grade adjustment** — `Team Score × [1 + B·A·Q·(peer ratio − 1)]`, agreement
  weight `A` banded at 10/20/30%.
- **Response-quality Points** — each student earns points for the **quality of the
  feedback they wrote** (author-side comment-support score `Q × max_points`),
  matching the workbook's feedback points.
- **Dimension letter grades** — Team Player / Quantity / Quality / Effect, each
  `mean(rating)/7 → letter` from the 4-statement matrix.
- **Performance** — from the **forced ranking** (High / Adequate / Low) when
  collected, otherwise the allocation ratio.
- **Pay grade** — average received allocation ÷ team average.
- **Self-evaluation** — the student's own ratings shown beside the peer grades.

**The reports:**

- **Instructor summary PDF** (landscape) — every student's dimension grades, pay
  grade, agreement `A`, support `Q`, **response-quality points**, multiplier,
  grade Δ, and performance, with a legend. Download it directly from Review &
  PDFs, or in the full zip.
- **Student feedback PDF** — styled like the original project: rating meters
  (teammates' average with your self-rating tick and peer/self letter badges),
  performance + pay grade + grade-adjustment meter, "What your teammates valued"
  and "Where to focus next", and "The feedback you gave" (your Q + points).

With only allocation + comments (a plain Qualtrics export), the engine degrades
gracefully — the dimension/ranking fields are simply blank.

**Default public URL.** `public_url` now defaults to
`https://peerparley.streamlit.app`, so student links prefill correctly without
any secret; set the `public_url` secret to override.

## Multiple instructors, per-section visibility

Several instructors share one deployment. Each signs in with their **own
username and password** (stored as salted PBKDF2 hashes in the vault, never in
plaintext). Every survey is stamped with an **owner**:

- **Instructors** see and administer only the sections they own — the sidebar
  survey picker lists just theirs, and another instructor's responses are
  blocked with a notice.
- **Administrators** see and administer **every** instructor's sections, and get
  a **👥 Manage instructors** panel in the sidebar to add accounts, reset
  passwords, change roles, deactivate, or remove them.

The app's original shared password (`app_password_sha256`) still works — it signs
you in as a built-in **admin** called `admin`, so nothing breaks and you can't be
locked out. Sign in with it first, then add instructors. New accounts get a
temporary password and must set their own on first sign-in.

## CSV everywhere (send it yourself)

Both sides can be exported instead of emailed from the app:

- **Invitations** — `links.csv` (every student's name, email, and personal link)
  in the Survey setup tab.
- **Results** — `recipients.csv` (name, email, rendered subject/body, and the
  individual PDF filename) in the Email tab, alongside the grades CSV in Review &
  PDFs and the PDF bundle. Download those and mail-merge them yourself.

## The two new tabs

**1 · Survey setup** — upload the contact list, edit the survey wording and the
points total, set an **open/close schedule** (or use the master on/off switch),
and **Save survey + roster to vault**. A status line shows whether the survey is
Open / Scheduled / Closed for students right now. Then generate every student's
personal link and either email it or download **links.csv** for your own mail
merge.

**Open / close dates.** The survey config carries optional `opens_at` /
`closes_at` datetimes (app-server clock). Before the open date the student form
says "opens on…"; after the close date it says "closed"; submissions are blocked
outside the window (re-checked on submit, so a deadline that passes mid-session
still holds). The master switch closes it instantly regardless of dates.

**Email is not tied to Microsoft 365.** Every send point (invitations,
reminders, and the results Email tab) has a **Send via** picker — *Microsoft 365
(Graph)* or *SMTP server* (Gmail/NAU/etc., credentials from `[email]` secrets) —
plus a no-server path: download `links.csv` for invites, or the PDF bundle for
results, and send them yourself.

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
