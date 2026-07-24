"""UI-side helpers shared by app.py (kept out of app.py to avoid ordering issues)."""
from __future__ import annotations

import base64
from typing import Dict, List, Optional

import streamlit as st

from . import email_delivery as mail
from . import pdfgen
from .config import AppConfig
from .grading import StudentResult, TeamResult
from .ingest import Roster


def render_pdf(pdf_bytes: bytes) -> None:
    """Inline PDF preview. Tries PyMuPDF page images (iframes are blocked by
    browsers); falls back to a download button."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page in doc:
            pix = page.get_pixmap(dpi=120)
            st.image(pix.tobytes("png"), use_container_width=True)
        return
    except Exception:
        pass
    st.download_button("⬇ Download PDF", pdf_bytes, "preview.pdf", "application/pdf")


def student_ctx(m: StudentResult, course: str, eval_no: str) -> Dict[str, str]:
    first = m.name.split(" ")[0]
    last = m.name.split(" ")[-1]
    return {
        "first_name": first, "last_name": last, "name": m.name,
        "team": m.team, "eval_no": eval_no, "class": course,
    }


def _email_for(m: StudentResult, roster: Optional[Roster]) -> str:
    if roster:
        rec = roster.match(m.name)
        if rec and rec.get("email"):
            return rec["email"]
    return ""


def build_messages(teams: List[TeamResult], roster: Optional[Roster],
                   subject_t: str, body_t: str, attach_team: bool,
                   course: str, eval_no: str) -> List[mail.Message]:
    messages: List[mail.Message] = []
    team_pdf_cache: Dict[str, bytes] = {}
    for t in teams:
        if attach_team and t.team not in team_pdf_cache:
            team_pdf_cache[t.team] = pdfgen.build_team_contribution_pdf(t, eval_no)
        for m in t.members:
            ctx = student_ctx(m, course, eval_no)
            safe = m.name.replace(" ", "_").replace("/", "-")
            atts = [mail.Attachment(
                f"{safe}_feedback.pdf",
                pdfgen.build_individual_pdf(m, eval_no, course))]
            if attach_team:
                atts.append(mail.Attachment(
                    f"team_{t.team}_contribution.pdf", team_pdf_cache[t.team]))
            messages.append(mail.Message(
                to_email=_email_for(m, roster),
                to_name=m.name, team=t.team,
                subject=mail.render_template(subject_t, ctx),
                body=mail.render_template(body_t, ctx),
                attachments=atts,
            ))
    return messages


def make_mailer(cfg: AppConfig):
    """Return a ready mailer, driving the Graph device-code flow in the UI."""
    if cfg.email.mode == "smtp":
        if not cfg.email.smtp_password:
            st.error("SMTP selected but no smtp_password in secrets.")
            return None
        return mail.SmtpMailer(cfg.email)

    # Graph device-code flow
    m365 = cfg.m365
    tenant = m365.get("tenant_id", "")
    client = m365.get("client_id", "")
    if not tenant or not client:
        st.error("Graph mode needs vault.m365.tenant_id and client_id in secrets.")
        return None

    if "graph_mailer" not in st.session_state:
        st.session_state["graph_mailer"] = mail.GraphMailer(
            tenant, client, cfg.email.sender)
    gm: mail.GraphMailer = st.session_state["graph_mailer"]
    if gm.ready:
        return gm

    if "graph_flow" not in st.session_state:
        st.session_state["graph_flow"] = gm.begin_device_flow()
    flow = st.session_state["graph_flow"]
    st.info(flow.get("message", "Complete sign-in in your browser."))
    st.code(f"{flow.get('verification_uri')}\nCode: {flow.get('user_code')}")
    if st.button("I've completed sign-in"):
        if gm.complete_device_flow(flow):
            st.success("Microsoft 365 authenticated.")
            del st.session_state["graph_flow"]
            return gm
        st.error("Sign-in not completed yet — try again.")
    return None
