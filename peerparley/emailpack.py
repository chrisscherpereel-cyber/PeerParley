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


def _as_str(s: str) -> str:
    """An AppleScript double-quoted string literal (handles quotes and newlines)."""
    return '"' + ((s or "").replace("\\", "\\\\").replace('"', '\\"')
                  .replace("\r", "").replace("\n", "\\n")) + '"'


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
    has_att = any(p["attachments"] for p in parts)
    lines = [
        '-- PeerParley — send every email via your Mac mail app.',
        '-- Open this file in Script Editor (double-click) and click Run (the ▶ button).',
        '-- You are asked which mail app to use — Microsoft Outlook by default.',
        '-- Approve the one-time prompt that lets it control the app. Sends from your account.',
        '',
    ]
    if has_att:
        lines.append('set folderPath to (POSIX path of (choose folder with prompt '
                     '"Select the unzipped folder that holds this script and the PDF files"))')
    else:
        lines.append('set folderPath to ""')
    lines.append('set msgs to {}')
    for p in parts:
        atts = "{" + ", ".join(_as_str(a[0]) for a in p["attachments"]) + "}"
        lines.append("set end of msgs to {|to|:%s, |subj|:%s, |body|:%s, |atts|:%s}" % (
            _as_str(p["to"]), _as_str(p["subject"]), _as_str(_strip_html(p["body"])), atts))
    lines += [
        'set appPick to (choose from list {"Microsoft Outlook", "Apple Mail"} '
        'with prompt "Send all of these using which mail app?" '
        'default items {"Microsoft Outlook"})',
        'if appPick is false then return',
        'set appPick to item 1 of appPick',
        'set sentCount to 0',
        'if appPick is "Microsoft Outlook" then',
        '  tell application "Microsoft Outlook"',
        '    repeat with m in msgs',
        '      if (|to| of m) is not "" then',
        '        set newMsg to make new outgoing message with properties '
        '{subject:(|subj| of m), plain text content:(|body| of m)}',
        '        make new recipient at newMsg with properties '
        '{email address:{address:(|to| of m)}}',
        '        repeat with a in (|atts| of m)',
        '          try',
        '            make new attachment at newMsg with properties '
        '{file:(POSIX file (folderPath & (a as text)))}',
        '          end try',
        '        end repeat',
        '        send newMsg',
        '        set sentCount to sentCount + 1',
        '      end if',
        '    end repeat',
        '  end tell',
        'else',
        '  tell application "Mail"',
        '    repeat with m in msgs',
        '      if (|to| of m) is not "" then',
        '        set newMsg to make new outgoing message with properties '
        '{subject:(|subj| of m), content:(|body| of m), visible:false}',
        '        tell newMsg',
        '          make new to recipient at end of to recipients with properties '
        '{address:(|to| of m)}',
        '          repeat with a in (|atts| of m)',
        '            try',
        '              make new attachment with properties '
        '{file name:(POSIX file (folderPath & (a as text)))} at after the last paragraph',
        '            end try',
        '          end repeat',
        '        end tell',
        '        send newMsg',
        '        set sentCount to sentCount + 1',
        '      end if',
        '    end repeat',
        '  end tell',
        'end if',
        'display dialog "Sent " & sentCount & " message(s)." buttons {"OK"} default button "OK"',
    ]
    return "\n".join(lines) + "\n"


_README = (
    "PeerParley — auto-send pack\n"
    "===========================\n\n"
    "This folder sends every email for you through the mail app already on your\n"
    "computer. UNZIP it first (don't run anything from inside the .zip), then:\n\n"
    "  MAC (Outlook or Apple Mail):  open 'send_all_mac.applescript' in Script Editor\n"
    "                                (double-click it), click Run. Pick your mail app\n"
    "                                when asked (Microsoft Outlook is the default).\n"
    "  WINDOWS (Outlook):            right-click 'send_all_windows.ps1' -> Run with\n"
    "                                PowerShell.\n\n"
    "The first time, your computer asks permission to let it control the mail app —\n"
    "approve it. Messages are sent from your own account, with the PDFs in this\n"
    "folder attached. Prefer to review first? The same emails are also available as\n"
    ".eml files, or use SMTP inside PeerParley for fully automatic sending.\n"
)


def send_all_pack(parts: List[dict], folder_label: str = "Emails") -> bytes:
    """Zip with attachment PDFs + a Windows (Outlook) and a Mac (Outlook/Mail) script."""
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
