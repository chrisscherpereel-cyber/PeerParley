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

with st.sidebar:
    st.caption(f"v{__version__}")
    ok, msg = Vault().healthcheck()
    (st.success if ok else st.warning)(msg)
    st.caption(f"Storage backend: **{cfg.vault.backend}** · folder `{cfg.vault.folder}`")
    if st.button("Sign out"):
        logout(); st.rerun()

course = st.sidebar.text_input("Course / section", S.get("course", ""))
eval_no = st.sidebar.text_input("Evaluation #", S.get("eval_no", "1"))
S["course"], S["eval_no"] = course, eval_no

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
    "6 · Email", "7 · Vault",
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

    cur = S.get("survey_cfg") or dict(survey.DEFAULT_SURVEY)
    with st.expander("Survey wording & options"):
        cur["title"] = st.text_input("Title", cur["title"])
        cur["intro"] = st.text_area("Intro (markdown)", cur["intro"], height=90)
        cur["points_total"] = int(st.number_input("Points each student allocates",
                                                   10, 1000, int(cur["points_total"]), 10))
        cur["ask_public_comment"] = st.checkbox("Ask for a comment on each teammate",
                                                value=cur["ask_public_comment"])
        cur["public_comment_prompt"] = st.text_input("Comment prompt",
                                                     cur["public_comment_prompt"])
        cur["ask_confidential"] = st.checkbox("Ask for a confidential note to the instructor",
                                              value=cur["ask_confidential"])
        cur["confidential_prompt"] = st.text_input("Confidential prompt",
                                                   cur["confidential_prompt"])
        cur["is_open"] = st.toggle("Accept submissions (master switch)",
                                   value=cur["is_open"],
                                   help="Turn off to close the survey immediately, "
                                        "regardless of the dates below.")

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

    if st.button("💾 Save survey + roster to vault", type="primary", disabled=cdf is None):
        try:
            sl, teams = survey.save_setup(course, eval_no, cdf, cur)
            st.success(f"Saved survey `{sl}` — {len(teams)} team(s). Students' links are "
                       "now live." + ("" if cfg.vault.backend != "local" else
                       " (Backend is 'local' — fine for testing, but responses live only "
                       "in this container. Use m365/dropbox/pcloud for real collection.)"))
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
    status = survey.response_status(vault, slug)
    if not status:
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
        teams = compute(S["long_df"], settings)
        S["teams"] = teams
        if not teams:
            st.warning("No teams computed — check the team column mapping.")
        else:
            frame = results_to_frame(teams)
            st.dataframe(frame, use_container_width=True, height=380)
            st.download_button("⬇ Results CSV", frame.to_csv(index=False),
                               "peerparley_results.csv", "text/csv")

            st.markdown("##### Generate PDFs")
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
                                pdfgen.build_individual_pdf(m, eval_no, course))
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
                    pdf = pdfgen.build_individual_pdf(m, eval_no, course)
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

        st.caption("Prefer not to email from the app? Build the PDF bundle on the "
                   "**Review & PDFs** tab and send the files yourself.")
        method = _method_selector("results_method")
        colx, coly = st.columns(2)
        drafts_only = colx.checkbox("Create Outlook drafts only (no send)",
                                    value=(method == "graph"), disabled=(method != "graph"))
        go = coly.button("Build messages & deliver", type="primary")

        if go:
            roster = S.get("roster")
            messages = _build_messages(S["teams"], roster, subject_t, body_t,
                                       attach_team, course, eval_no)
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
