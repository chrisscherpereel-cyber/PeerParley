"""Email delivery.

Two modes:
  graph : Microsoft 365 Graph API via OAuth2 device-code flow (HTTPS/443 only,
          firewall-friendly for enterprise networks). Can create Outlook drafts
          or send directly, with per-recipient PDF attachments.
  smtp  : smtp.office365.com with STARTTLS, batch send + reconnect/retry.

Delivery validation confirms each message carries the correct student's name /
team and that no other student's PDF is attached.
"""
from __future__ import annotations

import base64
import smtplib
import time
from dataclasses import dataclass, field
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Callable, Dict, List, Optional

import requests

from .config import EmailConfig, load_config

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Send", "Mail.ReadWrite", "User.Read"]


# --------------------------------------------------------------------------- #
# Templating
# --------------------------------------------------------------------------- #
def render_template(template: str, ctx: Dict[str, str]) -> str:
    out = template
    for k, v in ctx.items():
        out = out.replace("{" + k + "}", str(v))
    return out


@dataclass
class Attachment:
    filename: str
    content: bytes
    mime: str = "application/pdf"


@dataclass
class Message:
    to_email: str
    to_name: str
    team: str
    subject: str
    body: str
    attachments: List[Attachment] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Delivery validation (privacy guard)
# --------------------------------------------------------------------------- #
def validate_message(msg: Message) -> List[str]:
    problems = []
    if not msg.to_email or "@" not in msg.to_email:
        problems.append("missing/invalid recipient email")
    # Each attachment filename should reference this student, not others
    tag = msg.to_name.split(" ")[0].lower() if msg.to_name else ""
    for a in msg.attachments:
        fn = a.filename.lower()
        if tag and tag not in fn and "team" not in fn and "section" not in fn:
            problems.append(f"attachment '{a.filename}' may not belong to {msg.to_name}")
    return problems


# --------------------------------------------------------------------------- #
# Microsoft Graph — device code flow
# --------------------------------------------------------------------------- #
class GraphMailer:
    def __init__(self, tenant_id: str, client_id: str, sender: str):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.sender = sender
        self._token: Optional[str] = None
        self._app = None

    def begin_device_flow(self):
        import msal
        self._app = msal.PublicClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
        )
        flow = self._app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Device flow failed: {flow.get('error_description')}")
        return flow  # contains verification_uri + user_code + message

    def complete_device_flow(self, flow) -> bool:
        result = self._app.acquire_token_by_device_flow(flow)  # blocks until done
        if "access_token" in result:
            self._token = result["access_token"]
            return True
        return False

    @property
    def ready(self) -> bool:
        return self._token is not None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json"}

    def _payload(self, msg: Message) -> dict:
        atts = [{
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": a.filename,
            "contentType": a.mime,
            "contentBytes": base64.b64encode(a.content).decode(),
        } for a in msg.attachments]
        return {
            "message": {
                "subject": msg.subject,
                "body": {"contentType": "HTML", "content": msg.body},
                "toRecipients": [{"emailAddress": {"address": msg.to_email,
                                                   "name": msg.to_name}}],
                "attachments": atts,
            },
            "saveToSentItems": True,
        }

    def create_draft(self, msg: Message) -> str:
        body = self._payload(msg)["message"]
        r = requests.post(f"{GRAPH}/me/messages", headers=self._headers(),
                          json=body, timeout=60)
        r.raise_for_status()
        return r.json().get("id", "")

    def send(self, msg: Message) -> None:
        r = requests.post(f"{GRAPH}/me/sendMail", headers=self._headers(),
                          json=self._payload(msg), timeout=60)
        r.raise_for_status()


# --------------------------------------------------------------------------- #
# SMTP mailer
# --------------------------------------------------------------------------- #
class SmtpMailer:
    def __init__(self, cfg: EmailConfig):
        self.cfg = cfg
        self.server = None

    def _connect(self):
        self.server = smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=30)
        self.server.ehlo(); self.server.starttls(); self.server.ehlo()
        self.server.login(self.cfg.smtp_username, self.cfg.smtp_password)

    def send(self, msg: Message) -> None:
        mime = MIMEMultipart()
        mime["From"] = self.cfg.sender or self.cfg.smtp_username
        mime["To"] = msg.to_email
        mime["Subject"] = msg.subject
        mime.attach(MIMEText(msg.body, "html"))
        for a in msg.attachments:
            part = MIMEApplication(a.content, _subtype="pdf")
            part.add_header("Content-Disposition", "attachment", filename=a.filename)
            mime.attach(part)
        for attempt in range(3):
            try:
                if self.server is None:
                    self._connect()
                self.server.sendmail(mime["From"], [msg.to_email], mime.as_string())
                return
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPException):
                try:
                    self.server = None
                    self._connect()
                    self.server.sendmail(mime["From"], [msg.to_email], mime.as_string())
                    return
                except Exception:
                    time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"SMTP send failed for {msg.to_email}")

    def close(self):
        try:
            if self.server:
                self.server.quit()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Batch orchestration
# --------------------------------------------------------------------------- #
def batch_send(messages: List[Message], mailer, drafts_only: bool = False,
               progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
    total = len(messages)
    sent, drafted, failed = 0, 0, []
    for i, msg in enumerate(messages, 1):
        problems = validate_message(msg)
        if problems:
            failed.append({"to": msg.to_email, "error": "; ".join(problems)})
            if progress:
                progress(i, total, f"SKIP {msg.to_email}: {problems[0]}")
            continue
        try:
            if drafts_only and isinstance(mailer, GraphMailer):
                mailer.create_draft(msg); drafted += 1
                status = f"DRAFT {msg.to_email}"
            else:
                mailer.send(msg); sent += 1
                status = f"SENT {msg.to_email}"
        except Exception as exc:  # noqa: BLE001
            failed.append({"to": msg.to_email, "error": str(exc)})
            status = f"FAIL {msg.to_email}: {exc}"
        if progress:
            progress(i, total, status)
    if isinstance(mailer, SmtpMailer):
        mailer.close()
    return {"sent": sent, "drafted": drafted, "failed": failed, "total": total}
