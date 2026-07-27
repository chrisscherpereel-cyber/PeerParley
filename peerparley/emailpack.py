"""Generate ready-to-send email files (.eml) as downloadable folders.

Produces a zip with one .eml per student for each stage of the cycle:
  Invitations/   the initial personal-link email
  Reminders/     the same, for students who haven't responded
  Results/       the feedback email with the student's PDF (and team PDF) attached

An .eml file opens directly in Outlook / Apple Mail / Thunderbird as a
pre-filled draft, so an instructor with no API set up can still send everything.
"""
from __future__ import annotations

import io
import re
import zipfile
from email.message import EmailMessage
from typing import List, Optional, Tuple

from . import email_delivery as mail
from . import pdfgen


def _safe(s) -> str:
    return re.sub(r"[^\w]+", "_", str(s)).strip("_") or "student"


def _strip_html(h: str) -> str:
    return re.sub(r"<[^>]+>", " ", h or "").replace("&nbsp;", " ").strip()


def _ctx(name: str, team: str, eval_no: str, course: str, link: str = "") -> dict:
    parts = (name or "").split()
    return {"first_name": parts[0] if parts else "", "team": team,
            "last_name": parts[-1] if len(parts) > 1 else "", "name": name or "",
            "eval_no": eval_no, "class": course, "link": link}


def eml(to: str, subject: str, body_html: str,
        attachments: Optional[List[Tuple[str, bytes]]] = None, from_addr: str = "") -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    if from_addr:
        msg["From"] = from_addr
    msg["To"] = to or ""
    msg.set_content(_strip_html(body_html) or " ")
    msg.add_alternative(body_html or "", subtype="html")
    for fname, data in (attachments or []):
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=fname)
    return msg.as_bytes()


def invite_items(recipients, subject_t: str, body_t: str, course: str, eval_no: str,
                 from_addr: str = "") -> List[Tuple[str, bytes]]:
    """recipients: iterable of dicts {name, email, team, link}."""
    out = []
    for r in recipients:
        if not r.get("email"):
            continue
        ctx = _ctx(r.get("name", ""), r.get("team", ""), eval_no, course, r.get("link", ""))
        out.append((f"{_safe(r.get('name'))}.eml",
                    eml(r["email"], mail.render_template(subject_t, ctx),
                        mail.render_template(body_t, ctx), from_addr=from_addr)))
    return out


def results_items(teams, roster, subject_t: str, body_t: str, attach_team: bool,
                  course: str, eval_no: str, report: dict = None,
                  from_addr: str = "") -> List[Tuple[str, bytes]]:
    out = []
    team_pdf = {}
    for t in teams:
        if attach_team and t.team not in team_pdf:
            team_pdf[t.team] = pdfgen.build_team_contribution_pdf(t, eval_no)
        for m in t.members:
            email = ""
            if roster is not None:
                rec = roster.match(m.name)
                email = rec["email"] if rec else ""
            ctx = _ctx(m.name, m.team, eval_no, course)
            atts = [(f"{_safe(m.name)}_feedback.pdf",
                     pdfgen.build_individual_pdf(m, eval_no, course, report=report))]
            if attach_team:
                atts.append((f"team_{t.team}_contribution.pdf", team_pdf[t.team]))
            out.append((f"{_safe(m.name)}.eml",
                        eml(email, mail.render_template(subject_t, ctx),
                            mail.render_template(body_t, ctx), atts, from_addr)))
    return out


def zip_folders(folders: dict) -> bytes:
    """folders: {folder_name: [(filename, bytes), ...]} -> a single zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        wrote = False
        for folder, items in folders.items():
            for fname, data in items:
                z.writestr(f"{folder}/{fname}", data)
                wrote = True
        if not wrote:
            z.writestr("README.txt", "No emails to generate for the current selection.")
    return buf.getvalue()
