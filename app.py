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
import zipfile

import pandas as pd
import streamlit as st

from peerparley import __version__
from peerparley import accounts
from peerparley.auth import logout, require_login
from peerparley.config import load_config
from peerparley import ingest
from peerparley import survey
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


tabs = st.tabs([
    "1 · Survey setup", "2 · Responses",
    "3 · Upload & map", "4 · Configure", "5 · Review & PDFs",
    "6 · Email", "7 · Compare rounds", "8 · Vault",
])

# =========================================================================== #
# TAB 1 — Survey setup (upload contact list, configure, send links)
# =========================================================================== #
with tabs[0]:
    st.subheader("Set up & send the survey")
    st.caption("Upload the **contact list** (names, emails, teams) — the only input "
               "needed. PeerParley builds a personal evaluation link for every "
               "student; their answers become the grading input directly.")
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

    cur.setdefault("report", dict(survey.REPORT_DEFAULTS))
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
        body = st.text_area("Email body (HTML)", DEFAULT_INVITE_BODY, height=180, key="inv_body")
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

# =========================================================================== #
# TAB 2 — Responses (monitor + load into grading)
# =========================================================================== #
with tabs[1]:
    st.subheader("Responses")
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
                body = st.text_area("Body (HTML)", DEFAULT_INVITE_BODY, height=150, key="rem_body")
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
                           "Open **5 · Review & PDFs** to grade, then **6 · Email**.")

# =========================================================================== #
# TAB 3 — Upload & map  (optional Qualtrics fallback)
# =========================================================================== #
with tabs[2]:
    st.subheader("Upload Qualtrics export + roster")
    st.caption("Optional — only if you collected responses in Qualtrics instead of the "
               "built-in survey. Otherwise use tabs 1–2.")
    c1, c2 = st.columns(2)
    with c1:
        eval_file = st.file_uploader("Peer evaluation export (CSV/XLSX)",
                                     type=["csv", "xlsx", "xls"])
    with c2:
        roster_file = st.file_uploader("Contact roster (CSV/XLSX)",
                                       type=["csv", "xlsx", "xls"])

    if eval_file is not None:
        df = ingest.read_table(eval_file, eval_file.name)
        S["raw_df"] = df
        roster = None
        if roster_file is not None:
            rdf = ingest.read_table(roster_file, roster_file.name)
            roster = ingest.Roster.from_df(rdf)
            S["roster"] = roster
        cm = ingest.detect_columns(df)
        st.success(f"Loaded {len(df)} rows · "
                   f"{'auto-detected' if cm.detected else 'needs mapping'}")

        with st.expander("Column mapping", expanded=not cm.detected):
            cols = ["(none)"] + list(df.columns)
            def _sel(label, cur):
                idx = cols.index(cur) if cur in cols else 0
                v = st.selectbox(label, cols, index=idx, key="map_" + label)
                return None if v == "(none)" else v
            cm.respondent_name = _sel("Respondent name", cm.respondent_name)
            cm.respondent_email = _sel("Respondent email", cm.respondent_email)
            cm.respondent_team = _sel("Respondent team", cm.respondent_team)
            cm.public_comment_col = _sel("Public comment (Q24.1)", cm.public_comment_col)
            cm.confidential_comment_col = _sel("Confidential comment (Q24.2)",
                                               cm.confidential_comment_col)
            st.caption(f"Teammate-name slots: {cm.teammate_name_cols or '—'}")
            st.caption(f"Points slots: {cm.points_cols or '—'}")
        S["colmap"] = cm

        long_df = ingest.to_long(df, cm, S.get("roster"))
        S["long_df"] = long_df
        S["self_evals"] = {}          # Qualtrics export carries no self-rating block here

        st.markdown("##### Data-quality check")
        qa = ingest.data_quality_report(long_df, S.get("roster"))
        mm1, mm2, mm3, mm4 = st.columns(4)
        mm1.metric("Eval rows", qa.get("rows", 0))
        mm2.metric("Evaluators", qa.get("evaluators", 0))
        mm3.metric("Teams", qa.get("teams", 0))
        mm4.metric("Unmatched names", qa.get("unmatched_names", 0))
        for issue in qa.get("issues", []):
            st.write("• " + issue)
        with st.expander("Preview tidy rows"):
            st.dataframe(long_df.head(50), use_container_width=True)

# =========================================================================== #
# TAB 4 — Configure grading
# =========================================================================== #
with tabs[3]:
    st.subheader("Grading settings")
    c1, c2, c3 = st.columns(3)
    with c1:
        team_default = st.number_input("Default team score", 0.0, 200.0, 100.0)
        B = st.slider("Sensitivity B", 0.0, 1.5, 0.5, 0.05,
                      help="How much peer input moves the individual grade.")
        maxpts = st.number_input("Max comment points", 1, 20, 5)
    with c2:
        lo = st.slider("Min multiplier", 0.5, 1.0, 0.85, 0.01)
        hi = st.slider("Max multiplier", 1.0, 1.5, 1.15, 0.01)
        guard = st.checkbox("Agreement guard", value=True,
                            help="Soften forced 'Low' when evaluators disagree.")
    with c3:
        method = st.selectbox(
            "Performance method",
            ["allocation_ratio", "composite", "rank_linear", "rank_one_mean"],
            format_func=lambda x: {
                "allocation_ratio": "Allocation ratio (avg received ÷ team avg)",
                "composite": "Composite",
                "rank_linear": "Rank — linear tiers",
                "rank_one_mean": "Rank — #1 vs mean",
            }[x])
        band = st.slider("Performance band ±", 0.0, 0.25, 0.08, 0.01)
        rstep = st.selectbox("Rounding step", [1, 5], index=0)
        rmode = st.selectbox("Rounding mode", ["nearest", "up", "down"])

    descriptions = {
        "allocation_ratio": "Compares each student's average received allocation "
                            "to the team average; ±band sets High/Low cutoffs.",
        "composite": "Blends allocation ratio with comment support Q.",
        "rank_linear": "Top third = High, bottom third = Low, rest Expected.",
        "rank_one_mean": "Only the single top contributor is flagged High.",
    }
    st.info(descriptions[method])

    S["settings"] = GradeSettings(
        team_score_default=team_default, sensitivity_B=B,
        min_multiplier=lo, max_multiplier=hi, max_comment_points=int(maxpts),
        rounding_step=int(rstep), rounding_mode=rmode,
        performance_method=method, performance_band=band, agreement_guard=guard,
    )

# =========================================================================== #
# TAB 5 — Review + PDFs
# =========================================================================== #
with tabs[4]:
    st.subheader("Results & deliverables")
    if "long_df" not in S:
        st.info("Load responses in step 2 (or upload in step 3) first.")
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
# TAB 6 — Email
# =========================================================================== #
with tabs[5]:
    st.subheader("Email delivery")
    if "teams" not in S:
        st.info("Compute results in step 5 first.")
    else:
        default_subject = "Your peer evaluation feedback — Eval {eval_no}"
        default_body = (
            "Hi {first_name},<br><br>"
            "Your peer feedback for {class} (Evaluation {eval_no}) is attached. "
            "It reflects anonymous input from your teammates.<br><br>"
            "Best,<br>The teaching team"
        )
        subject_t = st.text_input("Subject template", default_subject)
        body_t = st.text_area("Body template (HTML)", default_body, height=160)
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
                   "the **PDF bundle** on the Review & PDFs tab, and send them yourself.")
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

# =========================================================================== #
# TAB 7 — Compare rounds (eval 1 vs 2 vs 3 for the same students)
# =========================================================================== #
with tabs[6]:
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
# TAB 8 — Vault (encrypted save / load behind the firewall)
# =========================================================================== #
with tabs[7]:
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
                st.success(f"Loaded `{pick}` ({len(df)} rows). Go to step 5 to recompute.")
            except Exception as exc:
                st.error(f"Load failed: {exc}")

    st.divider()
    st.markdown("**Danger zone**")
    if items:
        dele = st.selectbox("Delete bundle", ["(choose)"] + items)
        if dele != "(choose)" and st.button("Delete permanently"):
            vault.delete(dele)
            st.warning(f"Deleted `{dele}`.")
