"""PeerParley — single instructor/administrator application.

Runs on Streamlit Cloud. All student PII is encrypted at rest and persisted
only to the university-controlled vault (M365 / Dropbox / pCloud). The public
host never stores plaintext PII.

Workflow (one app):
    Set up & send survey  →  Collect responses  →  (or Upload a Qualtrics export)
    →  Configure grading   →  Review + PDFs      →  Deliver / draft emails
    plus: save/load encrypted vault bundles.

Students never sign in: a personal ?t=<token> link opens their evaluation form
directly (see peerparley/survey.py), bypassing the instructor password gate.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import io
import re
import urllib.parse
import zipfile

import pandas as pd
import streamlit as st

from peerparley import __version__
from peerparley import accounts
from peerparley.auth import logout, require_login
from peerparley.config import load_config
from peerparley import ingest
from peerparley import survey
from peerparley import emailpack
from peerparley.grading import GradeSettings, compute, results_to_frame
from peerparley import pdfgen
from peerparley import email_delivery as mail
from peerparley.security import encrypt_dataframe, decrypt_dataframe
from peerparley.vault import Vault
from peerparley.ui_helpers import (
    render_pdf as _render_pdf,
    student_ctx as _ctx,
    build_messages as _build_messages,
    make_mailer as _make_mailer,
)

st.set_page_config(page_title="PeerParley", page_icon="✅", layout="wide")

# --------------------------------------------------------------------------- #
# Brand header
# --------------------------------------------------------------------------- #
st.markdown(
    """
    <div style="display:flex;align-items:center;gap:14px;padding:6px 0 2px">
      <div style="background:#0E2A3B;color:#fff;border-radius:12px;
                  padding:8px 14px;font-weight:800;font-size:22px">PeerParley</div>
      <div style="color:#6B7A80;font-style:italic">Peer evaluation, made clear.</div>
    </div>
    """,
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- #
# Student form — the only public surface. A valid ?t=<token> link renders the
# student's personal evaluation and records their submission, BEFORE (and
# instead of) the instructor password gate.
# --------------------------------------------------------------------------- #
_token = None
try:
    _token = st.query_params.get("t")
except Exception:
    _qp = st.experimental_get_query_params()
    _token = (_qp.get("t") or [None])[0]
if _token:
    survey.render_student_app(_token)
    st.stop()

if not require_login():
    st.stop()

cfg = load_config()
S = st.session_state
user = st.session_state.get("pp_user") or {"user": "admin", "name": "Administrator",
                                           "role": "admin", "source": "secrets"}
is_admin = accounts.is_admin(user)

with st.sidebar:
    st.markdown(f"**{user.get('name', 'User')}**")
    st.caption(f"`{user.get('user')}` · "
               + ("administrator — every section" if is_admin
                  else "instructor — your sections only"))
    ok, msg = Vault().healthcheck()
    (st.success if ok else st.warning)(msg)
    st.caption(f"Storage: **{cfg.vault.backend}** · v{__version__}")
    if st.button("Sign out"):
        logout(); st.rerun()

    # ---- change my own password (vault accounts only) --------------------
    if user.get("source") == "vault":
        with st.expander("Change my password"):
            p1 = st.text_input("New password", type="password", key="own_pw1")
            p2 = st.text_input("Confirm", type="password", key="own_pw2")
            if st.button("Save password", key="own_pw_save"):
                if p1 != p2:
                    st.error("Those don't match.")
                elif len(p1) < 8:
                    st.error("Use at least 8 characters.")
                else:
                    try:
                        accounts.set_password(user["user"], p1, must_change=False)
                        st.success("Changed.")
                    except Exception as exc:  # noqa: BLE001
                        st.error(str(exc))

    st.divider()
    st.markdown("### Working on")
    _all = survey.list_surveys()
    _mine = survey.visible_surveys(_all, user)
    _labels = [f"{(s['course'] or '(no course)')} · Eval {s['eval_no']}"
               + (f" — {s['owner'] or 'unowned'}" if is_admin else "")
               for s in _mine]
    choice = st.selectbox("Survey", ["➕ New survey…"] + _labels, key="survey_pick")
    if choice == "➕ New survey…":
        course = st.text_input("Course / section", S.get("course", ""), key="new_course")
        eval_no = st.text_input("Evaluation #", S.get("eval_no", "1"), key="new_eval")
    else:
        _s = _mine[_labels.index(choice)]
        course, eval_no = _s["course"], str(_s["eval_no"])
        st.caption(f"Editing **{course} · Eval {eval_no}**"
                   + (f" · owner `{_s['owner'] or 'unowned'}`" if is_admin else ""))
    S["course"], S["eval_no"] = course, eval_no

    # ---- admin: manage instructor accounts -------------------------------
    if is_admin:
        st.divider()
        with st.expander("👥 Manage instructors"):
            accts = accounts.load_accounts()
            st.caption(f"{len(accts)} stored account(s), plus the built-in `admin`.")
            st.markdown("**Add an instructor**")
            nu = st.text_input("Username", key="acct_new_user")
            nn = st.text_input("Display name", key="acct_new_name")
            nr = st.selectbox("Role", ["instructor", "admin"], key="acct_new_role")
            npw = st.text_input("Temporary password", value="PeerParley-Welcome",
                                key="acct_new_pw")
            if st.button("Add instructor", key="acct_add"):
                try:
                    accounts.add_account(nu, nn, nr, npw or "PeerParley-Welcome")
                    st.success(f"Added `{nu}`. Give them username `{nu}` and password "
                               f"`{npw or 'PeerParley-Welcome'}` — they'll set their own "
                               "on first sign-in.")
                except Exception as exc:  # noqa: BLE001
                    st.error(str(exc))
            if accts:
                st.markdown("**Manage an account**")
                who = st.selectbox("Account", list(accts), key="acct_pick")
                a1, a2 = st.columns(2)
                if a1.button("Reset password", key="acct_reset"):
                    accounts.set_password(who, "PeerParley-Welcome", must_change=True)
                    st.success("Reset to `PeerParley-Welcome` (must change on next sign-in).")
                if a2.button("Toggle admin/instructor", key="acct_role"):
                    accounts.set_role(who, "instructor"
                                      if accts[who].get("role") == "admin" else "admin")
                    st.success("Role changed.")
                a3, a4 = st.columns(2)
                if a3.button(("Deactivate" if accts[who].get("active", True) else "Activate"),
                             key="acct_active"):
                    accounts.set_active(who, not accts[who].get("active", True))
                    st.success("Updated.")
                if a4.button("Remove", key="acct_remove"):
                    accounts.remove_account(who)
                    st.success(f"Removed `{who}`.")

DEFAULT_INVITE_BODY = (
    "Hi {first_name},<br><br>"
    "It's time for the peer evaluation for {class} (Evaluation {eval_no}). "
    "Please open your personal link below and split your points across your "
    "teammates. It only takes a few minutes, and you can revise until it "
    "closes.<br><br>"
    '<a href="{link}">Open my peer evaluation</a><br><br>'
    "If the button doesn't work, copy this address into your browser:<br>"
    "{link}<br><br>Thanks,<br>The teaching team"
)


GRADING_EXPLAINER = """
Every number a student sees is produced by one of these steps. Defaults are set so
you can grade without changing anything.

**1. Team score** — the grade the whole team starts with (default **100**). Peer
input then nudges each member up or down from this.

**2. Pay grade — the core signal.** Each student splits **$100** across teammates.
A student's *pay grade* is the average dollars they **received** ÷ the **team
average**. So **100% = exactly the team average**. Above 100% means teammates
valued them more than average; below 100% means less. Because it's measured
against the team's own average, self-allocations or blank answers can never push a
whole team into the negative.

**3. Grade adjustment (the ± %).** This is what moves the grade. You choose *which*
peer signal drives it under **Peer-adjustment method** below — the $100 allocation
(default, the WebPA/CATME factor), the four rating statements, the forced ranking,
or a combination. Whichever you pick, it works the same way:

> adjustment ≈ **sensitivity** × (peer factor − 100%)

So a student **above** the team average gets a **bonus (+%)**, one **below** gets a
**deduction (−%)**, and one exactly average gets **0%**. It's capped at the maximum
you set (default **±15%**). Two optional weights can only *shrink* the adjustment,
never flip its sign:

- **Agreement (A)** — if evaluators strongly *disagree* about a student, their
  adjustment is softened (they got mixed signals).
- **Comment support (Q)** — if the written comments about a student don't back up
  the dollars, the adjustment is softened. With no comments, Q is neutral (no
  effect).

**4. Feedback points** — each student also earns points (default up to **5**) for
the **quality of the feedback *they* wrote** about teammates — length, specifics,
and whether it looks copy-pasted. This rewards taking the evaluation seriously.

**5. Dimension letter grades** — the four rating statements (team player, quantity,
quality, effect) each average to a 0–100% score → a letter grade, shown to the
student so they know *where* they're strong or weak.

**6. Performance (High / Adequate / Low)** — from the **forced ranking** when the
survey collected it; otherwise from the pay grade (above/below the team average by
a set band).

**7. Self-evaluation** — the student's own ratings appear next to their peers' on
their feedback sheet, so they can see the gap.
"""


def _mailto(to, subject, body_html):
    """A mailto: URL that opens the computer's default mail app pre-filled.
    Body is converted to plain text (mailto can't carry HTML or attachments)."""
    text = re.sub(r"<br\s*/?>", "\n", body_html or "")
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text).replace("&nbsp;", " ")
    q = urllib.parse.urlencode({"subject": subject or "", "body": text},
                               quote_via=urllib.parse.quote)
    return f"mailto:{to}?{q}"


def _mail_app_links(recipients, subject_t, body_t, course, eval_no, limit=80):
    """Render clickable 'open in your mail app' links for link-only emails."""
    st.caption("Opens your computer's default email app (Outlook, Apple Mail, …) with the "
               "message pre-filled — you just press Send. No attachments this way; for the "
               "results emails with PDFs, use the .eml pack.")
    shown = 0
    for r in recipients:
        if not r.get("email"):
            continue
        ctx = {"first_name": (r.get("name", "").split(" ") or [""])[0],
               "last_name": r.get("name", "").split(" ")[-1], "name": r.get("name", ""),
               "team": r.get("team", ""), "eval_no": eval_no, "class": course,
               "link": r.get("link", "")}
        url = _mailto(r["email"], mail.render_template(subject_t, ctx),
                      mail.render_template(body_t, ctx))
        st.markdown(f"- [✉️ {r['name']} &lt;{r['email']}&gt;]({url})")
        shown += 1
        if shown >= limit:
            st.caption(f"Showing the first {limit}.")
            break
    if shown == 0:
        st.info("No recipients with an email address.")


def _email_body(default_html, key, height=220):
    """WYSIWYG email-body editor (Quill) that returns HTML. Falls back to a plain
    HTML text box if the streamlit-quill component isn't available."""
    st.caption("Formatting toolbar below. Placeholders like {first_name}, {team}, "
               "{eval_no}, {class} and {link} are filled in per student when sent.")
    try:
        from streamlit_quill import st_quill
        html = st_quill(value=st.session_state.get(f"{key}_html", default_html),
                        html=True, key=key)
        html = html if html else default_html
        st.session_state[f"{key}_html"] = html
        return html
    except Exception:
        return st.text_area("Body (HTML)", default_html, height=height, key=f"{key}_ta")


EMAIL_METHODS = [("graph", "Microsoft 365 (Graph)"), ("smtp", "SMTP server (Gmail, NAU, …)")]


def _mailer_for(method):
    """Build a mailer for the chosen method, overriding the secrets default."""
    c = dataclasses.replace(cfg, email=dataclasses.replace(cfg.email, mode=method))
    return _make_mailer(c)


def _method_selector(key):
    """A 'Send via' picker; defaults to the secrets email.mode."""
    keys = [k for k, _ in EMAIL_METHODS]
    labels = {k: v for k, v in EMAIL_METHODS}
    default = cfg.email.mode if cfg.email.mode in labels else "graph"
    m = st.selectbox("Send via", keys, index=keys.index(default),
                     format_func=lambda k: labels[k], key=key)
    if m == "smtp":
        st.caption("SMTP reads host/port/username/password from your secrets "
                   "([email] mode/smtp_*). Nothing is typed here.")
    return m


def _deliver(messages, method, drafts_only, ok_label="Done"):
    """Shared send routine for invite/reminder emails (reuses the app mailer)."""
    if not messages:
        st.warning("No recipients with an email address.")
        return
    st.write(f"Prepared {len(messages)} message(s).")
    mailer = _mailer_for(method)
    if mailer is None:
        return
    prog = st.progress(0.0)
    log = st.empty()
    lines = []

    def _cb(i, total, status):
        prog.progress(i / total)
        lines.append(status)
        log.code("\n".join(lines[-12:]))

    result = mail.batch_send(messages, mailer, drafts_only=drafts_only, progress=_cb)
    st.success(f"{ok_label} — sent {result['sent']}, drafted {result['drafted']}, "
               f"failed {len(result['failed'])} of {result['total']}.")
    if result["failed"]:
        st.dataframe(pd.DataFrame(result["failed"]))


st.info("**Follow the tabs left → right:** ① Set up the survey → ② Collect responses "
        "→ ③ Grading rules → ④ Results & reports → ⑤ Send feedback. "
        "*Compare* and *Vault* are optional.")

tabs = st.tabs([
    "① Set up survey", "② Collect responses", "③ Grading rules",
    "④ Results & reports", "⑤ Send feedback", "📈 Compare", "🔒 Vault",
])

# =========================================================================== #
# TAB 1 — Survey setup (upload contact list, configure, send links)
# =========================================================================== #
with tabs[0]:
    st.subheader("Set up & send the survey")
    st.caption("Step 1 of 5. Upload the **contact list** (names, emails, teams) — the only "
               "input needed. PeerParley builds a personal evaluation link for every "
               "student; their answers become the grading input directly. When you've "
               "sent the links, move to **② Collect responses**.")
    slug = survey.slugify(course, eval_no)
    st.caption(f"Course **{course or '—'}**, Eval **{eval_no}** → survey id `{slug}`")

    contact_file = st.file_uploader("Contact list (CSV/XLSX)",
                                    type=["csv", "xlsx", "xls"], key="survey_roster")
    if contact_file is not None:
        S["survey_contact_df"] = ingest.read_table(contact_file, contact_file.name)
    cdf = S.get("survey_contact_df")

    if cdf is not None:
        teams_preview = survey.build_teams(cdf)
        nstud = sum(len(v) for v in teams_preview.values())
        st.success(f"{len(teams_preview)} team(s), {nstud} student(s) with teammates.")
        with st.expander("Teams preview"):
            for t, members in teams_preview.items():
                st.write(f"**Team {t}** — " + ", ".join(m["name"] for m in members))
        no_email = [m["name"] for members in teams_preview.values()
                    for m in members if not m.get("email")]
        if no_email:
            st.warning(f"{len(no_email)} student(s) have no email and can't be sent a "
                       "link: " + ", ".join(no_email[:8]) + ("…" if len(no_email) > 8 else ""))

    # ---- Already collected in Qualtrics? Upload the raw export instead -------
    with st.expander("Already have responses? Upload a Qualtrics / raw export"):
        st.caption("Use this if you ran the evaluation in Qualtrics instead of the built-in "
                   "survey. Upload the raw export (CSV or XLSX) — PeerParley reads the "
                   "standard peer-evaluation layout (ratings, ranking, $ allocation, "
                   "comments) and grades it exactly like collected responses. No column "
                   "mapping needed.")
        qfile = st.file_uploader("Qualtrics raw export (CSV/XLSX)",
                                 type=["csv", "xlsx", "xls"], key="qualtrics_upload")
        if qfile is not None:
            try:
                q_long, q_self, q_roster, q_rep = ingest.parse_qualtrics_export(
                    qfile, qfile.name)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Couldn't read that export: {exc}")
                q_long = None
            if q_long is not None and not q_long.empty:
                S["long_df"] = q_long
                S["self_evals"] = q_self
                S["roster"] = q_roster
                m1, m2, m3 = st.columns(3)
                m1.metric("Evaluation rows", q_rep["rows"])
                m2.metric("Respondents", q_rep["evaluators"])
                m3.metric("Teams", q_rep["teams"])
                st.success("Parsed the export. Open **④ Results & reports** to see the "
                           "grades, then **⑤ Send feedback**.")
                with st.expander("Preview parsed rows"):
                    st.dataframe(q_long.head(30), use_container_width=True)
            elif q_long is not None:
                st.warning("No evaluation rows found. Check that this is the raw data "
                           "export (with the Q-code header row).")

    cur = {**survey.DEFAULT_SURVEY, **(S.get("survey_cfg") or {})}

    with st.expander("Questions — turn each on or off"):
        st.caption("Modelled on the MGT 301 peer evaluation. Switch any block off and "
                   "students won't see it.")
        cur["ask_ratings"] = st.checkbox("Rating matrix — 4 statements, 7-point agree/disagree",
                                         value=cur["ask_ratings"])
        cur["ask_improve"] = st.checkbox("Qualitative — how to increase their contribution",
                                         value=cur["ask_improve"])
        cur["ask_contribution"] = st.checkbox("Qualitative — their most significant contributions",
                                              value=cur["ask_contribution"])
        cur["ask_ranking"] = st.checkbox("Forced ranking — High / Adequate / Low performer",
                                         value=cur["ask_ranking"])
        cur["ask_allocation"] = st.checkbox("Pay allocation — split $100 across the team",
                                            value=cur["ask_allocation"])
        cur["ask_self_contribution"] = st.checkbox("Your own contribution — free text",
                                                   value=cur["ask_self_contribution"])
        cur["ask_confidential"] = st.checkbox("Confidential note to the instructor",
                                              value=cur["ask_confidential"])
        cur["show_header"] = st.checkbox("Header — name, class, section, team-member list",
                                         value=cur["show_header"])
        if not cur["ask_allocation"]:
            st.warning("With pay allocation off, the $ peer-ratio can't be computed — grade "
                       "adjustments will be flat. Leave it on unless you have a reason.")
        if not (cur["ask_improve"] or cur["ask_contribution"]):
            st.warning("With both qualitative questions off, the written-comment score Q is "
                       "empty and won't affect grades.")

    with st.expander("Wording"):
        cur["title"] = st.text_input("Title", cur["title"])
        cur["intro"] = st.text_area("Intro (markdown)", cur["intro"], height=80)
        if cur["ask_ratings"]:
            cur["ratings_prompt"] = st.text_input("Rating-matrix prompt", cur["ratings_prompt"])
            _sts = list(cur.get("rating_statements") or survey.RATING_STATEMENTS)
            _sts += [""] * (4 - len(_sts))
            for _k in range(4):
                _sts[_k] = st.text_input(f"Statement {_k + 1}", _sts[_k], key=f"stmt_{_k}")
            cur["rating_statements"] = [s for s in _sts if s.strip()]
        if cur["ask_improve"]:
            cur["improve_prompt"] = st.text_input(
                "‘Increase contribution’ prompt (use {member} for the name)", cur["improve_prompt"])
        if cur["ask_contribution"]:
            cur["contribution_prompt"] = st.text_input(
                "‘Most significant contributions’ prompt (use {member})", cur["contribution_prompt"])
        if cur["ask_ranking"]:
            cur["ranking_prompt"] = st.text_input("Forced-ranking prompt", cur["ranking_prompt"])
        if cur["ask_allocation"]:
            cur["allocation_prompt"] = st.text_input("Pay-allocation prompt", cur["allocation_prompt"])
            cur["points_total"] = int(st.number_input("Dollars to allocate", 10, 1000,
                                                       int(cur["points_total"]), 10))
        if cur["ask_self_contribution"]:
            cur["self_contribution_prompt"] = st.text_input("Your-contribution prompt",
                                                           cur["self_contribution_prompt"])
        if cur["ask_confidential"]:
            cur["confidential_prompt"] = st.text_input("Confidential-note prompt",
                                                       cur["confidential_prompt"])

    cur.setdefault("report", dict(getattr(survey, "REPORT_DEFAULTS", {
        "performance": True, "grade_adjustment": True, "dimensions": True,
        "pay_grade": True, "valued": True, "focus": True, "response_quality": True})))
    with st.expander("What students see in their feedback report"):
        st.caption("Choose which sections appear on each student's feedback PDF. "
                   "Grading is unaffected — this only changes what's shown to students.")
        rp = dict(cur["report"])
        rp["performance"] = st.checkbox("Performance label (High / Adequate / Low)",
                                        value=rp.get("performance", True))
        rp["grade_adjustment"] = st.checkbox("Grade-adjustment meter (± vs team score)",
                                             value=rp.get("grade_adjustment", True))
        rp["dimensions"] = st.checkbox("Rating meters + dimension letter grades",
                                       value=rp.get("dimensions", True))
        rp["pay_grade"] = st.checkbox("Pay grade", value=rp.get("pay_grade", True))
        rp["valued"] = st.checkbox("“What your teammates valued” (contributions)",
                                   value=rp.get("valued", True))
        rp["focus"] = st.checkbox("“Where to focus next” (improvements)",
                                  value=rp.get("focus", True))
        rp["response_quality"] = st.checkbox("“The feedback you gave” (your Q + points)",
                                             value=rp.get("response_quality", True))
        cur["report"] = rp

    cur["is_open"] = st.toggle("Accept submissions (master switch)", value=cur["is_open"],
                               help="Turn off to close the survey immediately, regardless "
                                    "of the dates below.")

    with st.expander("Schedule — open / close dates (optional)"):
        st.caption(f"App server clock right now: **{dt.datetime.now():%b %d, %Y %I:%M %p}**. "
                   "Dates below use this clock — set them relative to it.")
        use_open = st.checkbox("Set an OPEN date", value=bool(cur.get("opens_at")))
        if use_open:
            _o = survey.parse_dt(cur.get("opens_at")) or dt.datetime.now()
            co1, co2 = st.columns(2)
            od = co1.date_input("Opens on", _o.date(), key="open_d")
            ot = co2.time_input("Opens at", _o.time().replace(microsecond=0), key="open_t")
            cur["opens_at"] = dt.datetime.combine(od, ot).isoformat()
        else:
            cur["opens_at"] = ""
        use_close = st.checkbox("Set a CLOSE date", value=bool(cur.get("closes_at")))
        if use_close:
            _c = survey.parse_dt(cur.get("closes_at")) or (dt.datetime.now() + dt.timedelta(days=7))
            cc1, cc2 = st.columns(2)
            cd = cc1.date_input("Closes on", _c.date(), key="close_d")
            ct = cc2.time_input("Closes at", _c.time().replace(microsecond=0), key="close_t")
            cur["closes_at"] = dt.datetime.combine(cd, ct).isoformat()
        else:
            cur["closes_at"] = ""
    S["survey_cfg"] = cur

    _state = survey.window_state(cur)
    _badge = {"open": "🟢 Open", "not_yet": "🟡 Scheduled — not open yet",
              "closed": "🔴 Closed (past the close date)",
              "disabled": "🔴 Closed (master switch off)"}[_state]
    st.caption(f"Current status for students: **{_badge}** · {survey.window_message(cur)}")

    _own = survey.survey_owner(Vault(), slug)
    if _own not in (None, "") and is_admin is False and not survey.can_access(_own, user):
        st.warning(f"A survey with this course/eval already exists and belongs to "
                   f"`{_own}`. Choose a different course or evaluation number.")
    if st.button("💾 Save survey + roster to vault", type="primary", disabled=cdf is None):
        if _own not in (None, "") and not survey.can_access(_own, user):
            st.error(f"Can't save — this course/eval belongs to `{_own}`.")
        else:
            try:
                stamp = user["user"] if _own in (None, "") else None
                sl, teams = survey.save_setup(course, eval_no, cdf, cur, owner=stamp)
                st.success(f"Saved survey `{sl}` — {len(teams)} team(s), owner "
                           f"`{survey.survey_owner(Vault(), sl) or user['user']}`. Links "
                           "are now live." + ("" if cfg.vault.backend != "local" else
                           " (Backend is 'local' — responses live only in this container; "
                           "use m365/dropbox/pcloud for real collection.)"))
            except Exception as exc:  # noqa: BLE001
                st.error(f"Save failed: {exc}")

    st.divider()
    st.markdown("##### Send personal links")
    base_url = st.text_input("Public app URL", getattr(cfg, "public_url", "") or "",
                             help="The deployed address, e.g. https://yourapp.streamlit.app. "
                                  "Each student's link is this plus their signed token.")
    if cdf is not None:
        links = survey.student_links(base_url, slug, survey.build_teams(cdf),
                                     survey.token_secret(cfg))
        links_df = pd.DataFrame(links)
        with st.expander(f"Preview {len(links)} links"):
            st.dataframe(links_df, use_container_width=True, height=280)
            st.download_button("⬇ links.csv", links_df.to_csv(index=False),
                               f"{slug}_links.csv", "text/csv")
        st.caption("No mail server? Skip this and just use the **links.csv** above — "
                   "it has every student's link for your own mail merge.")
        subj = st.text_input("Email subject",
                             "Your peer evaluation for {class} (Eval {eval_no})", key="inv_subj")
        body = _email_body(DEFAULT_INVITE_BODY, key="inv_body")
        inv_method = _method_selector("inv_method")
        drafts_only = st.checkbox("Create Outlook drafts only (no send)",
                                  value=(inv_method == "graph"), key="inv_drafts",
                                  disabled=(inv_method != "graph"))
        if st.button("Build & deliver links", key="inv_go"):
            if not base_url.strip():
                st.error("Enter the public app URL first, or the links won't point anywhere.")
            else:
                msgs = []
                for r in links:
                    if not r["email"]:
                        continue
                    ctx = {"first_name": r["name"].split(" ")[0], "last_name": r["name"].split(" ")[-1],
                           "name": r["name"], "team": r["team"], "eval_no": eval_no,
                           "class": course, "link": r["link"]}
                    msgs.append(mail.Message(
                        to_email=r["email"], to_name=r["name"], team=r["team"],
                        subject=mail.render_template(subj, ctx),
                        body=mail.render_template(body, ctx), attachments=[]))
                _deliver(msgs, inv_method, drafts_only and inv_method == "graph",
                         ok_label="Links delivered")

        st.markdown("**Or download an invitation email pack (.eml folder)**")
        st.caption("Ready-to-send .eml files (one per student) to open in Outlook / "
                   "Apple Mail — no mail server needed.")
        if st.button("Build invitation email pack (zip)", key="inv_pack"):
            if not base_url.strip():
                st.error("Enter the public app URL first, or the links won't point anywhere.")
            else:
                items = emailpack.invite_items(links, subj, body, course, eval_no)
                S["invite_pack"] = emailpack.zip_folders({"Invitations": items})
        if S.get("invite_pack"):
            st.download_button("⬇ Download invitation emails (zip)", S["invite_pack"],
                               f"{slug}_invitations.zip", "application/zip")

        with st.expander("Or open each in your computer's mail app (mailto)"):
            _mail_app_links(links, subj, body, course, eval_no)

# =========================================================================== #
# TAB 2 — Responses (monitor + load into grading)
# =========================================================================== #
with tabs[1]:
    st.subheader("Collect responses")
    st.caption("Step 2 of 5. Watch who has responded, send reminders to those who "
               "haven't, and when you're ready click **Load responses into grading** at "
               "the bottom — then go to **④ Results & reports**.")
    slug = survey.slugify(course, eval_no)
    vault = Vault()
    _owner = survey.survey_owner(vault, slug)
    _blocked = _owner is not None and not survey.can_access(_owner, user)
    status = [] if _blocked else survey.response_status(vault, slug)
    if _blocked:
        st.error(f"This section belongs to another instructor (`{_owner or 'unowned'}`). "
                 "Only its owner or an administrator can see its responses.")
    elif not status:
        st.info("No saved survey for this course/eval yet — set one up in step 1.")
    else:
        got = sum(1 for r in status if r["responded"])
        m1, m2 = st.columns(2)
        m1.metric("Responded", f"{got} / {len(status)}")
        m2.metric("Outstanding", len(status) - got)

        _svy = survey.load_survey(vault, slug)
        _st = survey.window_state(_svy)
        _badge = {"open": "🟢 Open", "not_yet": "🟡 Not open yet",
                  "closed": "🔴 Closed", "disabled": "🔴 Closed (switch off)"}[_st]
        st.caption(f"Survey status: **{_badge}** · {survey.window_message(_svy)}")

        sdf = pd.DataFrame(status)
        by_team = (sdf.groupby("team")["responded"]
                   .agg(["sum", "count"]).reset_index()
                   .rename(columns={"sum": "responded", "count": "students"}))
        st.dataframe(by_team, use_container_width=True)
        with st.expander("Per-student status"):
            st.dataframe(sdf, use_container_width=True, height=320)

        nonresp = [r for r in status if not r["responded"]]
        if nonresp:
            st.download_button("⬇ non-responders.csv",
                               pd.DataFrame(nonresp).to_csv(index=False),
                               f"{slug}_nonresponders.csv", "text/csv")
            with st.expander("Send a reminder to non-responders"):
                base_url = st.text_input("Public app URL", getattr(cfg, "public_url", "") or "", key="rem_url")
                subj = st.text_input("Subject",
                                     "Reminder: your peer evaluation for {class}", key="rem_subj")
                body = _email_body(DEFAULT_INVITE_BODY, key="rem_body", height=150)
                rem_method = _method_selector("rem_method")
                drafts_only = st.checkbox("Drafts only", value=(rem_method == "graph"),
                                          key="rem_drafts", disabled=(rem_method != "graph"))
                if st.button("Send reminders", key="rem_go"):
                    snap = survey.load_roster_snapshot(vault, slug) or {"teams": {}}
                    teams = snap.get("teams", {})
                    secret = survey.token_secret(cfg)
                    msgs = []
                    for r in nonresp:
                        if not r["email"]:
                            continue
                        from peerparley.tokens import make_token
                        tok = make_token({"s": slug, "t": r["team"], "p": r["pos"]}, secret)
                        sep = "&" if "?" in base_url else "?"
                        link = f"{base_url}{sep}t={tok}" if base_url else f"?t={tok}"
                        ctx = {"first_name": r["name"].split(" ")[0], "name": r["name"],
                               "team": r["team"], "eval_no": eval_no, "class": course, "link": link}
                        msgs.append(mail.Message(
                            to_email=r["email"], to_name=r["name"], team=r["team"],
                            subject=mail.render_template(subj, ctx),
                            body=mail.render_template(body, ctx), attachments=[]))
                    _deliver(msgs, rem_method, drafts_only and rem_method == "graph",
                             ok_label="Reminders delivered")

                st.markdown("**Or download a reminder email pack (.eml folder)**")
                if st.button("Build reminder email pack (zip)", key="rem_pack"):
                    from peerparley.tokens import make_token
                    secret = survey.token_secret(cfg)
                    recips = []
                    for r in nonresp:
                        if not r["email"]:
                            continue
                        tok = make_token({"s": slug, "t": r["team"], "p": r["pos"]}, secret)
                        sep = "&" if "?" in base_url else "?"
                        link = f"{base_url}{sep}t={tok}" if base_url else f"?t={tok}"
                        recips.append({"name": r["name"], "email": r["email"],
                                       "team": r["team"], "link": link})
                    items = emailpack.invite_items(recips, subj, body, course, eval_no)
                    S["reminder_pack"] = emailpack.zip_folders({"Reminders": items})
                if S.get("reminder_pack"):
                    st.download_button("⬇ Download reminder emails (zip)", S["reminder_pack"],
                                       f"{slug}_reminders.zip", "application/zip")

                with st.expander("Or open each in your computer's mail app (mailto)"):
                    from peerparley.tokens import make_token
                    _secret = survey.token_secret(cfg)
                    _recips = []
                    for r in nonresp:
                        if not r["email"]:
                            continue
                        _tok = make_token({"s": slug, "t": r["team"], "p": r["pos"]}, _secret)
                        _sep = "&" if "?" in base_url else "?"
                        _recips.append({"name": r["name"], "email": r["email"],
                                        "team": r["team"],
                                        "link": f"{base_url}{_sep}t={_tok}" if base_url
                                                else f"?t={_tok}"})
                    _mail_app_links(_recips, subj, body, course, eval_no)

        st.divider()
        st.markdown("##### Grade the collected responses")
        st.caption("Pulls every submission, turns it into the grading input, and hands "
                   "it to the Review / PDF / Email tabs — no Qualtrics upload needed.")
        if st.button("⬇ Load responses into grading", type="primary"):
            long_df = survey.responses_to_long(vault, slug)
            if long_df.empty:
                st.warning("No responses collected yet.")
            else:
                S["long_df"] = long_df
                S["self_evals"] = survey.self_evaluations(vault, slug)
                snap = survey.load_roster_snapshot(vault, slug)
                S["roster"] = survey.roster_for_matching(snap)
                st.success(f"Loaded {len(long_df)} evaluation rows from {got} submission(s). "
                           "Open **④ Results & reports** to grade, then **⑤ Send feedback**.")

# =========================================================================== #
# TAB 3 — Configure grading
# =========================================================================== #
with tabs[2]:
    st.subheader("Grading settings")
    st.caption("Step 3 of 5. The defaults are sensible — you can skip straight to "
               "**Results** without changing anything. The panel below explains exactly "
               "how each grade is produced.")

    with st.expander("📖 How grading works (plain English)", expanded=True):
        st.markdown(GRADING_EXPLAINER)

    st.markdown("##### Peer-adjustment method")
    adj_source = st.selectbox(
        "How to measure each student's peer contribution",
        ["allocation", "rating", "ranking", "combined"],
        format_func=lambda x: {
            "allocation": "Points shared — the $100 allocation (WebPA / CATME family) · default",
            "rating": "Average of the four rating statements (CATME-style)",
            "ranking": "Forced ranking tiers — High / Adequate / Low",
            "combined": "Combined — average of the points and rating measures",
        }[x],
        help="Each is a normalized peer-assessment factor: a student's peer score ÷ the "
             "team average, so 100% = average. The grade adjustment then applies the same "
             "way. See the explainer above and GRADING.md for the research behind each.")

    st.markdown("##### Main settings")
    c1, c2, c3 = st.columns(3)
    with c1:
        B = st.slider("Sensitivity (how strongly peers move the grade)",
                      0.0, 1.5, 0.5, 0.05,
                      help="0 = peers don't move the grade at all; 1 = full effect. This is "
                           "the WebPA/SPARK 'weighting fraction'.")
    with c2:
        max_adj = st.slider("Maximum adjustment (± %)", 0, 30, 15, 1,
                            help="Caps how far above/below the team score any grade can move.")
    with c3:
        maxpts = st.number_input("Points for feedback quality", 1, 20, 5,
                                 help="Points a student earns for the quality of the "
                                      "feedback THEY wrote about teammates.")

    with st.expander("Advanced options (most instructors leave these alone)"):
        team_default = st.number_input("Default team score (before peer adjustment)",
                                       0.0, 200.0, 100.0)
        normalize = st.checkbox("Correct for rater leniency (z-score each evaluator)",
                                value=False,
                                help="Removes systematic easy-grader / hard-grader "
                                     "differences before comparing students, by z-scoring "
                                     "each evaluator's points. Applies to the points ($100) "
                                     "and combined methods.")
        guard = st.checkbox("Agreement guard — soften a forced 'Low' when evaluators "
                            "strongly disagree", value=True)
        perf_method = st.selectbox(
            "Performance label when there's NO forced ranking",
            ["allocation_ratio", "rank_linear", "rank_one_mean"],
            format_func=lambda x: {
                "allocation_ratio": "Above / below the team average (± band)",
                "rank_linear": "Top third High · bottom third Low",
                "rank_one_mean": "Only the single top contributor is High",
            }[x],
            help="Forced ranking is used automatically when the survey collected it; "
                 "this only applies otherwise.")
        band = st.slider("Performance band ± (for the average-based label)",
                         0.0, 0.25, 0.08, 0.01)
        rstep = st.selectbox("Round adjustments to", [1, 5], index=0,
                             format_func=lambda x: f"{x}%")
        rmode = st.selectbox("Rounding direction", ["nearest", "up", "down"])

    S["settings"] = GradeSettings(
        team_score_default=team_default, sensitivity_B=B,
        min_multiplier=1 - max_adj / 100.0, max_multiplier=1 + max_adj / 100.0,
        max_comment_points=int(maxpts), rounding_step=int(rstep), rounding_mode=rmode,
        performance_method=perf_method, performance_band=band, agreement_guard=guard,
        adjustment_source=adj_source, normalize_raters=normalize,
    )
    st.success(f"Method: **{adj_source}** · grades move at most **±{max_adj}%** from the "
               "team score, centred on the team average (above average → bonus, below → "
               "deduction).")

# =========================================================================== #
# TAB 4 — Review + PDFs
# =========================================================================== #
with tabs[3]:
    st.subheader("Results & reports")
    st.caption("Step 4 of 5. Review the grades, download the instructor summary and the "
               "student feedback PDFs, then move to **⑤ Send feedback**.")
    if "long_df" not in S:
        st.info("No responses loaded yet. Go to **② Collect responses** and click "
                "*Load responses into grading* — or upload a Qualtrics export on the "
                "**① Set up survey** tab.")
    else:
        settings = S.get("settings", GradeSettings())
        teams = compute(S["long_df"], settings, self_evals=S.get("self_evals"))
        S["teams"] = teams
        if not teams:
            st.warning("No teams computed — check the team column mapping.")
        else:
            frame = results_to_frame(teams)
            st.dataframe(frame, use_container_width=True, height=380)
            st.download_button("⬇ Results CSV", frame.to_csv(index=False),
                               "peerparley_results.csv", "text/csv")

            report = survey.load_survey(Vault(), survey.slugify(course, eval_no)).get("report") or {}

            st.markdown("##### Instructor reports")
            st.caption("The section summary lists every student's dimension grades, pay "
                       "grade, grade adjustment, the points they earned for the quality of "
                       "the feedback they wrote, and the confidential comments.")
            _summary_pdf = pdfgen.build_section_summary_pdf(teams, course, eval_no)
            _conf_pdf = pdfgen.build_confidential_pdf(teams, course, eval_no)
            r1, r2 = st.columns(2)
            r1.download_button(
                "⬇ Instructor summary (PDF)", _summary_pdf,
                f"peerparley_{course or 'section'}_eval{eval_no}_summary.pdf",
                "application/pdf", type="primary")
            r2.download_button(
                "⬇ Confidential feedback (PDF)", _conf_pdf,
                f"peerparley_{course or 'section'}_eval{eval_no}_confidential.pdf",
                "application/pdf")
            with st.expander("Preview instructor summary"):
                _render_pdf(_summary_pdf)
            with st.expander("Preview confidential feedback"):
                _render_pdf(_conf_pdf)

            st.markdown("##### Student & team PDFs")
            colA, colB = st.columns(2)
            if colA.button("Build all PDFs (zip)"):
                zbuf = io.BytesIO()
                with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
                    z.writestr("section_summary.pdf",
                               pdfgen.build_section_summary_pdf(teams, course, eval_no))
                    z.writestr("confidential_feedback.pdf",
                               pdfgen.build_confidential_pdf(teams, course, eval_no))
                    for t in teams:
                        z.writestr(f"team_{t.team}/team_contribution.pdf",
                                   pdfgen.build_team_contribution_pdf(t, eval_no))
                        for m in t.members:
                            safe = m.name.replace(" ", "_").replace("/", "-")
                            z.writestr(
                                f"team_{t.team}/{safe}_feedback.pdf",
                                pdfgen.build_individual_pdf(m, eval_no, course, report=report))
                S["pdf_zip"] = zbuf.getvalue()
                st.success("PDFs built.")
            if S.get("pdf_zip"):
                colB.download_button("⬇ Download PDF bundle (zip)", S["pdf_zip"],
                                     f"peerparley_{course or 'section'}_eval{eval_no}.zip",
                                     "application/zip")

            with st.expander("Preview a student PDF"):
                names = [f"{t.team} · {m.name}" for t in teams for m in t.members]
                pick = st.selectbox("Student", names) if names else None
                if pick:
                    ti, ni = pick.split(" · ", 1)
                    m = next(m for t in teams for m in t.members
                             if t.team == ti and m.name == ni)
                    pdf = pdfgen.build_individual_pdf(m, eval_no, course, report=report)
                    _render_pdf(pdf)

# =========================================================================== #
# TAB 5 — Email
# =========================================================================== #
with tabs[4]:
    st.subheader("Send feedback")
    st.caption("Step 5 of 5. Email each student their feedback PDF, or download a ready-to-"
               "send email pack. Prefer to send yourself? Use the CSV or the email pack.")
    if "teams" not in S:
        st.info("No results yet — open **④ Results & reports** first so the grades are "
                "computed.")
    else:
        default_subject = "Your peer evaluation feedback — Eval {eval_no}"
        default_body = (
            "Hi {first_name},<br><br>"
            "Your peer feedback for {class} (Evaluation {eval_no}) is attached. "
            "It reflects anonymous input from your teammates.<br><br>"
            "Best,<br>The teaching team"
        )
        subject_t = st.text_input("Subject template", default_subject)
        body_t = _email_body(default_body, key="results_body", height=200)
        attach_team = st.checkbox("Attach team-contribution PDF", value=True)

        # live preview
        sample = S["teams"][0].members[0]
        ctx = _ctx(sample, course, eval_no)
        with st.expander("Live preview"):
            st.write("**Subject:** " + mail.render_template(subject_t, ctx))
            st.markdown(mail.render_template(body_t, ctx), unsafe_allow_html=True)

        # recipients CSV — for your own mail merge (parallels the invites links.csv)
        _roster = S.get("roster")
        _rows = []
        for _t in S["teams"]:
            for _m in _t.members:
                _cx = _ctx(_m, course, eval_no)
                _rec = _roster.match(_m.name) if _roster else None
                _rows.append({"Team": _t.team, "Name": _m.name,
                              "Email": (_rec or {}).get("email", ""),
                              "Subject": mail.render_template(subject_t, _cx),
                              "Body": mail.render_template(body_t, _cx),
                              "Individual PDF": f"{_m.name.replace(' ', '_')}_feedback.pdf"})
        st.download_button("⬇ recipients.csv (name · email · rendered subject/body)",
                           pd.DataFrame(_rows).to_csv(index=False),
                           f"peerparley_results_recipients_eval{eval_no}.csv", "text/csv")
        st.caption("Prefer not to email from the app? Download **recipients.csv** here and "
                   "the **PDF bundle** on the Results tab, and send them yourself.")

        method = _method_selector("results_method")
        colx, coly = st.columns(2)
        drafts_only = colx.checkbox("Create Outlook drafts only (no send)",
                                    value=(method == "graph"), disabled=(method != "graph"))
        go = coly.button("Build messages & deliver", type="primary")

        if go:
            roster = S.get("roster")
            report = survey.load_survey(Vault(), survey.slugify(course, eval_no)).get("report") or {}
            messages = _build_messages(S["teams"], roster, subject_t, body_t,
                                       attach_team, course, eval_no, report=report)
            st.write(f"Prepared {len(messages)} messages.")
            mailer = _mailer_for(method)
            if mailer is None:
                st.stop()
            drafts_only = drafts_only and method == "graph"
            prog = st.progress(0.0)
            log = st.empty()
            lines = []

            def _cb(i, total, status):
                prog.progress(i / total)
                lines.append(status)
                log.code("\n".join(lines[-12:]))

            result = mail.batch_send(messages, mailer, drafts_only=drafts_only,
                                     progress=_cb)
            st.success(f"Done — sent {result['sent']}, drafted {result['drafted']}, "
                       f"failed {len(result['failed'])} of {result['total']}.")
            if result["failed"]:
                st.dataframe(pd.DataFrame(result["failed"]))

        st.markdown("**Or download a results email pack (.eml folder) — for your desktop mail app**")
        st.caption("Ready-to-send .eml files, one per student, each with their feedback PDF "
                   "attached — open in Outlook / Apple Mail / Thunderbird and press Send. "
                   "No mail server needed. (This is the way to send results through your "
                   "computer's mail app — a mailto link can't carry the PDF attachment.)")
        if st.button("Build results email pack (zip)", key="res_pack"):
            rep = survey.load_survey(Vault(), survey.slugify(course, eval_no)).get("report") or {}
            items = emailpack.results_items(S["teams"], S.get("roster"), subject_t, body_t,
                                            attach_team, course, eval_no, report=rep)
            S["results_pack"] = emailpack.zip_folders({"Results": items})
        if S.get("results_pack"):
            st.download_button("⬇ Download results emails (zip)", S["results_pack"],
                               f"{survey.slugify(course, eval_no)}_results_emails.zip",
                               "application/zip")

# =========================================================================== #
# TAB 6 — Compare rounds (eval 1 vs 2 vs 3 for the same students)
# =========================================================================== #
with tabs[5]:
    st.subheader("Compare evaluations")
    st.caption("See how the same students' peer-evaluation results change across "
               "evaluation rounds for this course.")
    if not course:
        st.info("Pick or enter a course in the sidebar first.")
    else:
        vault = Vault()
        _mine = [s for s in survey.visible_surveys(survey.list_surveys(vault), user)
                 if s["course"] == course]
        eval_nos = sorted({str(s["eval_no"]) for s in _mine}, key=lambda x: (len(x), x))
        if not eval_nos:
            st.info("No saved evaluations for this course yet.")
        else:
            picks = st.multiselect("Evaluations to compare", eval_nos, default=eval_nos)
            settings = S.get("settings", GradeSettings())
            evals_data = []
            for en in picks:
                ld = survey.responses_to_long(vault, survey.slugify(course, en))
                if ld.empty:
                    continue
                se = survey.self_evaluations(vault, survey.slugify(course, en))
                evals_data.append((en, compute(ld, settings, self_evals=se)))
            if not evals_data:
                st.warning("None of the selected evaluations have responses yet.")
            else:
                by_eval, team_of, names = {}, {}, set()
                for en, tr in evals_data:
                    by_eval[en] = {m.name: m for t in tr for m in t.members}
                    for t in tr:
                        for m in t.members:
                            names.add(m.name); team_of[m.name] = t.team
                rows = []
                for nm in sorted(names, key=lambda n: (str(team_of.get(n, "")), n.lower())):
                    row = {"Team": team_of.get(nm, ""), "Student": nm}
                    for en, _ in evals_data:
                        m = by_eval[en].get(nm)
                        row[f"E{en} Score"] = m.individual_score if m else None
                        row[f"E{en} Δ%"] = m.signed_pct if m else None
                        row[f"E{en} Perf"] = m.performance if m else ""
                    rows.append(row)
                cmp_df = pd.DataFrame(rows)
                st.dataframe(cmp_df, use_container_width=True, height=360)
                st.download_button("⬇ comparison.csv", cmp_df.to_csv(index=False),
                                   f"peerparley_{course}_comparison.csv", "text/csv")

                teams_list = sorted({team_of[n] for n in names})
                if teams_list:
                    tsel = st.selectbox("Chart a team's individual score across rounds",
                                        teams_list)
                    chart = []
                    for en, _ in evals_data:
                        d = {"Eval": f"Eval {en}"}
                        for nm in names:
                            if team_of.get(nm) == tsel:
                                m = by_eval[en].get(nm)
                                d[nm] = m.individual_score if m else None
                        chart.append(d)
                    st.line_chart(pd.DataFrame(chart).set_index("Eval"))

                st.download_button(
                    "⬇ Comparison report (PDF)",
                    pdfgen.build_comparison_pdf(course, evals_data),
                    f"peerparley_{course}_comparison.pdf", "application/pdf", type="primary")


# =========================================================================== #
# TAB 7 — Vault (encrypted save / load behind the firewall)
# =========================================================================== #
with tabs[6]:
    st.subheader("Encrypted vault — behind the firewall")
    st.caption("Bundles are AES-encrypted locally, then written to "
               f"**{cfg.vault.backend}**. The cloud host never stores plaintext PII.")
    vault = Vault()

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Save current dataset**")
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
        default_key = f"{(course or 'section').replace(' ', '')}_eval{eval_no}_{stamp}.ppx"
        key = st.text_input("Bundle name", default_key)
        if st.button("🔒 Encrypt & save to vault"):
            if "long_df" not in S:
                st.warning("Nothing to save yet.")
            else:
                try:
                    vault.put_bytes(key, encrypt_dataframe(S["long_df"]))
                    st.success(f"Saved encrypted bundle `{key}` to {cfg.vault.backend}.")
                except Exception as exc:
                    st.error(f"Save failed: {exc}")
    with c2:
        st.markdown("**Load a saved bundle**")
        try:
            items = [x for x in vault.list() if x.endswith(".ppx")]
        except Exception as exc:
            items = []
            st.error(f"Vault list failed: {exc}")
        pick = st.selectbox("Bundle", items) if items else None
        if pick and st.button("🔓 Load & decrypt"):
            try:
                df = decrypt_dataframe(vault.get_bytes(pick))
                S["long_df"] = df
                st.success(f"Loaded `{pick}` ({len(df)} rows). Open **④ Results & reports** "
                           "to recompute.")
            except Exception as exc:
                st.error(f"Load failed: {exc}")

    st.divider()
    st.markdown("**Danger zone**")
    if items:
        dele = st.selectbox("Delete bundle", ["(choose)"] + items)
        if dele != "(choose)" and st.button("Delete permanently"):
            vault.delete(dele)
            st.warning(f"Deleted `{dele}`.")
