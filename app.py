"""PeerParley — single instructor/administrator application.

Runs on Streamlit Cloud. All student PII is encrypted at rest and persisted
only to the university-controlled vault (M365 / Dropbox / pCloud). The public
host never stores plaintext PII.

Workflow (one app, five steps):
    1. Sign in            5. Deliver / draft emails
    2. Upload & map       ← plus: save/load encrypted vault bundles
    3. Configure grading
    4. Review + PDFs
"""
from __future__ import annotations

import datetime as dt
import io
import zipfile

import pandas as pd
import streamlit as st

from peerparley import __version__
from peerparley.auth import logout, require_login
from peerparley.config import load_config
from peerparley import ingest
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

tabs = st.tabs([
    "1 · Upload & map", "2 · Configure", "3 · Review & PDFs",
    "4 · Email", "5 · Vault",
])

# =========================================================================== #
# TAB 1 — Upload & map
# =========================================================================== #
with tabs[0]:
    st.subheader("Upload Qualtrics export + roster")
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
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Eval rows", qa.get("rows", 0))
        m2.metric("Evaluators", qa.get("evaluators", 0))
        m3.metric("Teams", qa.get("teams", 0))
        m4.metric("Unmatched names", qa.get("unmatched_names", 0))
        for issue in qa.get("issues", []):
            st.write("• " + issue)
        with st.expander("Preview tidy rows"):
            st.dataframe(long_df.head(50), use_container_width=True)

# =========================================================================== #
# TAB 2 — Configure grading
# =========================================================================== #
with tabs[1]:
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
# TAB 3 — Review + PDFs
# =========================================================================== #
with tabs[2]:
    st.subheader("Results & deliverables")
    if "long_df" not in S:
        st.info("Upload data in step 1 first.")
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
# TAB 4 — Email
# =========================================================================== #
with tabs[3]:
    st.subheader("Email delivery")
    if "teams" not in S:
        st.info("Compute results in step 3 first.")
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

        mode = cfg.email.mode
        st.caption(f"Delivery mode from secrets: **{mode}**")
        colx, coly = st.columns(2)
        drafts_only = colx.checkbox("Create Outlook drafts only (no send)",
                                    value=(mode == "graph"))
        go = coly.button("Build messages & deliver", type="primary")

        if go:
            roster = S.get("roster")
            messages = _build_messages(S["teams"], roster, subject_t, body_t,
                                       attach_team, course, eval_no)
            st.write(f"Prepared {len(messages)} messages.")
            mailer = _make_mailer(cfg)
            if mailer is None:
                st.stop()
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
# TAB 5 — Vault (encrypted save / load behind the firewall)
# =========================================================================== #
with tabs[4]:
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
                st.success(f"Loaded `{pick}` ({len(df)} rows). Go to step 3 to recompute.")
            except Exception as exc:
                st.error(f"Load failed: {exc}")

    st.divider()
    st.markdown("**Danger zone**")
    if items:
        dele = st.selectbox("Delete bundle", ["(choose)"] + items)
        if dele != "(choose)" and st.button("Delete permanently"):
            vault.delete(dele)
            st.warning(f"Deleted `{dele}`.")
