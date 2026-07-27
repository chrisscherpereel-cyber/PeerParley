"""Generate ready-to-send email files and auto-send packs.

Two flavours, both downloadable as a zip:

* **.eml pack** — one .eml per student; each opens in Outlook / Apple Mail /
  Thunderbird as a pre-filled draft (with the feedback PDF attached for results).
  You review and press Send.

* **Auto-send pack** — the attachment PDFs plus two ready-to-run scripts,
  `send_all_windows.ps1` and `send_all_mac.applescript`, with every message baked
  in. Unzip, run the one for your OS, and your installed Outlook / Apple Mail
  sends the whole batch automatically (the OS will ask permission the first time).
  A web app can't do this itself — the browser sandbox forbids driving your
  desktop mail app — so the work is handed to a script that runs on your machine.
"""
from __future__ import annotations

import base64
import io
import re
import zipfile
from email.message import EmailMessage
from typing import Dict, List, Optional, Tuple

from . import email_delivery as mail
from . import pdfgen


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _safe(s) -> str:
    return re.sub(r"[^\w]+", "_", str(s)).strip("_") or "student"


def _strip_html(h: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", h or "")
    t = re.sub(r"</p>", "\n\n", t)
    return re.sub(r"<[^>]+>", "", t).replace("&nbsp;", " ").strip()


def _b64(s: str) -> str:
    return base64.b64encode((s or "").encode("utf-8")).decode("ascii")


def _ctx(name: str, team: str, eval_no: str, course: str, link: str = "") -> dict:
    parts = (name or "").split()
    return {"first_name": parts[0] if parts else "", "team": team,
            "last_name": parts[-1] if len(parts) > 1 else "", "name": name or "",
            "eval_no": eval_no, "class": course, "link": link}


# --------------------------------------------------------------------------- #
# Message parts (transport-neutral: feed both .eml and the scripts)
#   part = {"to", "name", "subject", "body" (html), "attachments":[(filename,bytes)]}
# --------------------------------------------------------------------------- #
def invite_parts(recipients, subject_t: str, body_t: str, course: str, eval_no: str) -> List[dict]:
    out = []
    for r in recipients:
        if not r.get("email"):
            continue
        ctx = _ctx(r.get("name", ""), r.get("team", ""), eval_no, course, r.get("link", ""))
        out.append({"to": r["email"], "name": r.get("name", ""),
                    "subject": mail.render_template(subject_t, ctx),
                    "body": mail.render_template(body_t, ctx), "attachments": []})
    return out


def results_parts(teams, roster, subject_t: str, body_t: str, attach_team: bool,
                  course: str, eval_no: str, report: dict = None) -> List[dict]:
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
            out.append({"to": email, "name": m.name,
                        "subject": mail.render_template(subject_t, ctx),
                        "body": mail.render_template(body_t, ctx), "attachments": atts})
    return out


# --------------------------------------------------------------------------- #
# .eml pack (manual: open + Send)
# --------------------------------------------------------------------------- #
def _eml(part: dict) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = part["subject"]
    msg["To"] = part["to"] or ""
    msg.set_content(_strip_html(part["body"]) or " ")
    msg.add_alternative(part["body"] or "", subtype="html")
    for fname, data in part["attachments"]:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=fname)
    return msg.as_bytes()


def invite_items(recipients, subject_t, body_t, course, eval_no, from_addr="") -> List[Tuple[str, bytes]]:
    return [(f"{_safe(p['name'])}.eml", _eml(p))
            for p in invite_parts(recipients, subject_t, body_t, course, eval_no)]


def results_items(teams, roster, subject_t, body_t, attach_team, course, eval_no,
                  report=None, from_addr="") -> List[Tuple[str, bytes]]:
    return [(f"{_safe(p['name'])}.eml", _eml(p))
            for p in results_parts(teams, roster, subject_t, body_t, attach_team,
                                   course, eval_no, report)]


def zip_folders(folders: dict) -> bytes:
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


# --------------------------------------------------------------------------- #
# Auto-send pack (scripts with the data baked in + attachment files)
# --------------------------------------------------------------------------- #
def _ps_sq(s: str) -> str:
    return "'" + (s or "").replace("'", "''") + "'"


def _as_dq(s: str) -> str:
    return '"' + (s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _powershell(parts: List[dict]) -> str:
    rows = []
    for p in parts:
        atts = ";".join(a[0] for a in p["attachments"])
        rows.append("  @{ to=%s; subject=%s; body64=%s; atts=%s }" % (
            _ps_sq(p["to"]), _ps_sq(p["subject"]), _ps_sq(_b64(p["body"])), _ps_sq(atts)))
    return (
        "# PeerParley — send everything via Windows Outlook.\n"
        "# 1) Unzip this folder.  2) Right-click this file -> Run with PowerShell.\n"
        "# Outlook must be installed and signed in. It will send from your account.\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$dir = Split-Path -Parent $MyInvocation.MyCommand.Path\n"
        "$outlook = New-Object -ComObject Outlook.Application\n"
        "$msgs = @(\n" + ",\n".join(rows) + "\n)\n"
        "$sent = 0\n"
        "foreach ($m in $msgs) {\n"
        "  if (-not $m.to) { continue }\n"
        "  $mail = $outlook.CreateItem(0)\n"
        "  $mail.To = $m.to\n"
        "  $mail.Subject = $m.subject\n"
        "  $mail.HTMLBody = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($m.body64))\n"
        "  if ($m.atts) { foreach ($a in ($m.atts -split ';')) { $p = Join-Path $dir $a;"
        " if (Test-Path $p) { [void]$mail.Attachments.Add($p) } } }\n"
        "  $mail.Send(); $sent++\n"
        "}\n"
        "Write-Host \"Sent $sent message(s) via Outlook.\"\n")


def _applescript(parts: List[dict]) -> str:
    rows = []
    for p in parts:
        atts = "{" + ", ".join(_as_dq(a[0]) for a in p["attachments"]) + "}"
        rows.append("{|to|:%s, |subj|:%s, |body64|:%s, |atts|:%s}" % (
            _as_dq(p["to"]), _as_dq(p["subject"]), _as_dq(_b64(p["body"])), atts))
    return (
        '-- PeerParley — send everything via Apple Mail on a Mac.\n'
        '-- 1) Unzip this folder.  2) Open this file in Script Editor and click Run\n'
        '--    (or: right-click -> Open With -> Script Editor). Approve the prompt\n'
        '--    that lets it control Mail. It sends from your Mail account.\n'
        'set myPath to POSIX path of (path to me)\n'
        'set AppleScript\'s text item delimiters to "/"\n'
        'set folderPath to ((text items 1 thru -2 of myPath) as text) & "/"\n'
        'set msgs to {\n' + ",\n".join(rows) + '\n}\n'
        'set sentCount to 0\n'
        'tell application "Mail"\n'
        '  repeat with m in msgs\n'
        '    set theTo to (|to| of m)\n'
        '    if theTo is not "" then\n'
        '      set theBody to (do shell script "echo " & quoted form of (|body64| of m) & " | base64 --decode")\n'
        '      set newMsg to make new outgoing message with properties {subject:(|subj| of m), content:theBody, visible:false}\n'
        '      tell newMsg\n'
        '        make new to recipient at end of to recipients with properties {address:theTo}\n'
        '        repeat with a in (|atts| of m)\n'
        '          try\n'
        '            make new attachment with properties {file name:(POSIX file (folderPath & (a as text)))} at after the last paragraph\n'
        '          end try\n'
        '        end repeat\n'
        '      end tell\n'
        '      send newMsg\n'
        '      set sentCount to sentCount + 1\n'
        '    end if\n'
        '  end repeat\n'
        'end tell\n'
        'display dialog "Sent " & sentCount & " message(s) via Apple Mail." buttons {"OK"} default button "OK"\n')


_README = (
    "PeerParley — auto-send pack\n"
    "===========================\n\n"
    "This folder can send every email for you through the mail app already on your\n"
    "computer. Unzip it first, then:\n\n"
    "  WINDOWS (Outlook):  right-click 'send_all_windows.ps1' -> Run with PowerShell.\n"
    "  MAC (Apple Mail):   open 'send_all_mac.applescript' in Script Editor, click Run.\n\n"
    "The first time, your computer will ask permission to let the script control the\n"
    "mail app — approve it. Messages are sent from your own mail account, with the\n"
    "PDF attachments in this folder. Prefer to review first? The same folder also\n"
    "works as .eml files, or use SMTP inside PeerParley for fully automatic sending.\n"
)


def send_all_pack(parts: List[dict], folder_label: str = "Emails") -> bytes:
    """Zip with attachment PDFs + a Windows and a Mac send-all script (data baked in)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        seen = set()
        for p in parts:
            for fname, data in p["attachments"]:
                if fname not in seen:
                    z.writestr(fname, data)
                    seen.add(fname)
        z.writestr("send_all_windows.ps1", _powershell(parts))
        z.writestr("send_all_mac.applescript", _applescript(parts))
        z.writestr("READ ME FIRST.txt", _README)
    return buf.getvalue()
