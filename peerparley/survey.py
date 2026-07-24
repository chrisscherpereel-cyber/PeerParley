"""Built-in survey: set up, distribute links, collect responses.

This is the "setup / administer" half of PeerParley. Instead of exporting a
Qualtrics survey and re-uploading the results, the instructor uploads only the
**contact list** (names, emails, teams); PeerParley then serves each student a
personal evaluation form and stores the sealed responses in the same
firewall-side vault the rest of the app uses. Collected responses are turned
into the exact tidy `long_df` the grading engine already consumes, so the
Review / PDF / Email tabs work unchanged.

Storage layout (all encrypted by the Vault before leaving the process):

    survey__<slug>.ppj     the survey wording/config for one course+eval
    roster__<slug>.ppj     the contact list snapshot (teams + members, ordered)
    resp__<slug>__<team>__p<pos>.ppj   one student's submission

`<slug>` is derived from the course label + evaluation number, so two sections
or two rounds never collide. Positions are the student's index within their
team in the stored snapshot — that's what a link points at, so the roster must
not be reshuffled after links go out.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd

from .config import load_config
from .ingest import Roster, name_key
from .tokens import make_token, read_token
from .vault import Vault


# --------------------------------------------------------------------------- #
# Survey definition
# --------------------------------------------------------------------------- #
DEFAULT_SURVEY: Dict = {
    "title": "Peer Evaluation",
    "intro": ("Rate your teammates by splitting **100 points** across them — give "
              "more points to those who contributed more. Add a short comment for "
              "each teammate, and an optional confidential note to the instructor."),
    "points_total": 100,
    "ask_public_comment": True,
    "public_comment_prompt": "What did this teammate do well, or where could they improve?",
    "ask_confidential": True,
    "confidential_prompt": "Anything you'd like to share privately with the instructor? (optional)",
    "is_open": True,
    "closed_note": "This peer evaluation is now closed. Thank you.",
}


# --------------------------------------------------------------------------- #
# Slugs, secrets, storage keys
# --------------------------------------------------------------------------- #
def slugify(course: str, eval_no: str) -> str:
    base = f"{course or 'section'}_eval{eval_no or '1'}"
    return re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-") or "section-eval1"


def token_secret(cfg=None) -> str:
    """The HMAC secret for links. Prefer an explicit token_secret; otherwise
    derive one deterministically from the Fernet key so links work out of the
    box wherever the app is already configured to encrypt."""
    cfg = cfg or load_config()
    explicit = (getattr(cfg, "token_secret", "") or "").strip()
    if explicit:
        return explicit
    import hashlib
    seed = (getattr(cfg, "fernet_key", "") or "peerparley").encode("utf-8")
    return hashlib.sha256(b"peerparley-links:" + seed).hexdigest()


def _safe(s) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", str(s)).strip("-") or "x"


def key_survey(slug: str) -> str:
    return f"survey__{slug}.ppj"


def key_roster(slug: str) -> str:
    return f"roster__{slug}.ppj"


def key_response(slug: str, team: str, pos: int) -> str:
    return f"resp__{slug}__{_safe(team)}__p{pos}.ppj"


def _resp_prefix(slug: str) -> str:
    return f"resp__{slug}__"


# --------------------------------------------------------------------------- #
# Encrypted JSON helpers (Vault already encrypts on write / decrypts on read)
# --------------------------------------------------------------------------- #
def _save_json(vault: Vault, name: str, obj) -> None:
    vault.put_bytes(name, json.dumps(obj, default=str, ensure_ascii=False).encode("utf-8"))


def _load_json(vault: Vault, name: str):
    return json.loads(vault.get_bytes(name).decode("utf-8"))


# --------------------------------------------------------------------------- #
# Roster snapshot (teams + ordered members) from the contact list
# --------------------------------------------------------------------------- #
def _contact_columns(df: pd.DataFrame):
    """Resolve name/first/last/email/team columns from a contact list.

    Deliberately stricter than ingest.Roster's fuzzy matcher: a standalone
    full-name column is matched exactly (so a "First Name" column is not
    mistaken for the full name), while first/last are matched on their own
    unambiguous substrings.
    """
    low = {str(c).lower().strip(): c for c in df.columns}

    def exact(cands):
        for cand in cands:
            if cand in low:
                return low[cand]
        return None

    def contains(cands):
        for cand in cands:
            for k, orig in low.items():
                if cand in k:
                    return orig
        return None

    full = exact(["name", "full name", "fullname", "student name", "student"])
    first = contains(["first name", "firstname", "first"])
    last = contains(["last name", "lastname", "last"])
    email = contains(["primaryemail", "email", "e-mail"])
    team = contains(["team", "group"])
    return full, first, last, email, team


def build_teams(contact_df: pd.DataFrame) -> Dict[str, List[dict]]:
    """Group the contact list into {team: [ordered member records]}.

    Positions are the index in each team's list, sorted by name for stability.
    A member record is {name, first, last, email}. Teams with fewer than two
    members are dropped — a student with no teammates has nothing to evaluate.
    """
    full_c, first_c, last_c, email_c, team_c = _contact_columns(contact_df)
    teams: Dict[str, List[dict]] = {}
    seen = set()
    for _, row in contact_df.iterrows():
        first = str(row.get(first_c, "")).strip() if first_c else ""
        last = str(row.get(last_c, "")).strip() if last_c else ""
        if full_c and pd.notna(row.get(full_c)) and str(row.get(full_c)).strip():
            name = str(row[full_c]).strip()
        else:
            name = f"{first} {last}".strip()
        if not name:
            continue
        team = str(row.get(team_c, "")).strip() if team_c else ""
        if not team:
            continue
        key = (team, name_key(name))
        if key in seen:
            continue
        seen.add(key)
        teams.setdefault(team, []).append({
            "name": name,
            "first": first or name.split(" ")[0],
            "last": last or name.split(" ")[-1],
            "email": str(row.get(email_c, "")).strip() if email_c else "",
        })
    ordered = {}
    for team, members in teams.items():
        members.sort(key=lambda m: name_key(m["name"]))
        if len(members) >= 2:
            ordered[team] = members
    return dict(sorted(ordered.items(), key=lambda kv: str(kv[0])))


def save_setup(course: str, eval_no: str, contact_df: pd.DataFrame,
               survey_cfg: Dict, vault: Optional[Vault] = None):
    """Persist the roster snapshot + survey config for this course/eval."""
    vault = vault or Vault()
    slug = slugify(course, eval_no)
    teams = build_teams(contact_df)
    _save_json(vault, key_roster(slug),
               {"course": course, "eval_no": eval_no, "teams": teams})
    _save_json(vault, key_survey(slug), survey_cfg)
    return slug, teams


def load_roster_snapshot(vault: Vault, slug: str) -> Optional[dict]:
    try:
        return _load_json(vault, key_roster(slug))
    except Exception:
        return None


def load_survey(vault: Vault, slug: str) -> Dict:
    try:
        return {**DEFAULT_SURVEY, **_load_json(vault, key_survey(slug))}
    except Exception:
        return dict(DEFAULT_SURVEY)


def roster_for_matching(snapshot: dict) -> Roster:
    """A Roster (name_key -> record) rebuilt from the snapshot, for email match."""
    r = Roster()
    for team, members in (snapshot or {}).get("teams", {}).items():
        for m in members:
            r.by_key[name_key(m["name"])] = {
                "name": m["name"], "first": m.get("first", ""),
                "last": m.get("last", ""), "email": m.get("email", ""),
                "team": team,
            }
    return r


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #
def student_links(base_url: str, slug: str, teams: Dict[str, List[dict]],
                  secret: str) -> List[dict]:
    base = (base_url or "").strip()
    sep = "&" if "?" in base else "?"
    out = []
    for team, members in teams.items():
        for pos, m in enumerate(members):
            tok = make_token({"s": slug, "t": team, "p": pos}, secret)
            link = f"{base}{sep}t={tok}" if base else f"?t={tok}"
            out.append({"team": team, "pos": pos, "name": m["name"],
                        "email": m.get("email", ""), "link": link})
    return out


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #
def save_response(vault: Vault, slug: str, team: str, pos: int, payload: dict) -> None:
    _save_json(vault, key_response(slug, team, pos), payload)


def load_response(vault: Vault, slug: str, team: str, pos: int) -> Optional[dict]:
    try:
        return _load_json(vault, key_response(slug, team, pos))
    except Exception:
        return None


def all_responses(vault: Vault, slug: str) -> List[dict]:
    prefix = _resp_prefix(slug)
    try:
        names = [n for n in vault.list() if str(n).startswith(prefix)]
    except Exception:
        names = []
    out = []
    for n in names:
        try:
            out.append(_load_json(vault, n))
        except Exception:
            pass
    return out


def response_status(vault: Vault, slug: str) -> List[dict]:
    """Per-student responded/not-responded rows, for the progress panel."""
    snap = load_roster_snapshot(vault, slug)
    if not snap:
        return []
    got = {(r.get("team"), int(r.get("pos", -1))) for r in all_responses(vault, slug)}
    rows = []
    for team, members in snap.get("teams", {}).items():
        for pos, m in enumerate(members):
            rows.append({"team": team, "pos": pos, "name": m["name"],
                         "email": m.get("email", ""),
                         "responded": (team, pos) in got})
    return rows


def responses_to_long(vault: Vault, slug: str) -> pd.DataFrame:
    """Turn collected submissions into the tidy long-format the grader consumes:
    one row per (evaluator -> evaluatee) with points + comments."""
    snap = load_roster_snapshot(vault, slug)
    if not snap:
        return pd.DataFrame()
    teams = snap.get("teams", {})
    rows = []
    for resp in all_responses(vault, slug):
        team = resp.get("team", "")
        members = teams.get(team, [])
        pos = int(resp.get("pos", -1))
        if pos < 0 or pos >= len(members):
            continue
        evaluator = members[pos]["name"]
        ev_key = name_key(evaluator)
        alloc = resp.get("alloc", {}) or {}
        comments = resp.get("comments", {}) or {}
        conf = resp.get("confidential", "") or ""
        first = True
        for tpos_str, pts in alloc.items():
            tpos = int(tpos_str)
            if tpos == pos or tpos >= len(members):
                continue
            tname = members[tpos]["name"]
            try:
                pval = float(pts)
            except (TypeError, ValueError):
                pval = float("nan")
            rows.append({
                "evaluator": evaluator,
                "evaluator_key": ev_key,
                "evaluator_team": team,
                "evaluatee": tname,
                "evaluatee_key": name_key(tname),
                "points": pval,
                "public_comment": str(comments.get(tpos_str, "") or ""),
                "confidential_comment": conf if first else "",
            })
            first = False
    return pd.DataFrame.from_records(rows)


# --------------------------------------------------------------------------- #
# Public student form (served by app.py when a ?t=<token> is present)
# --------------------------------------------------------------------------- #
def render_student_app(token: str) -> None:
    """Render one student's evaluation form and record their submission.

    This runs BEFORE the instructor password gate, so it is the only public
    surface. It can read the roster/survey and write a sealed response, but it
    is otherwise the same encrypted vault the console uses.
    """
    import streamlit as st

    cfg = load_config()
    payload = read_token(token, token_secret(cfg))
    if not payload:
        st.error("This link is invalid or has expired. Please contact your instructor.")
        return

    slug = payload.get("s", "")
    team = payload.get("t", "")
    pos = int(payload.get("p", -1))

    vault = Vault()
    snap = load_roster_snapshot(vault, slug)
    survey = load_survey(vault, slug)
    if not snap:
        st.error("This evaluation isn't available right now. Please try again later.")
        return

    members = snap.get("teams", {}).get(team, [])
    if pos < 0 or pos >= len(members):
        st.error("This link doesn't match the current roster. Contact your instructor.")
        return
    me = members[pos]

    st.header(survey.get("title", "Peer Evaluation"))
    st.caption(f"{snap.get('course', '')} · Evaluation {snap.get('eval_no', '')}")

    if not survey.get("is_open", True):
        st.warning(survey.get("closed_note", "This peer evaluation is closed."))
        return

    st.markdown(survey.get("intro", ""))
    total = int(survey.get("points_total", 100))
    st.info(f"You are **{me['name']}** — Team {team}. Split **{total} points** across "
            "your teammates (not yourself).")

    teammates = [(i, m) for i, m in enumerate(members) if i != pos]
    prior = load_response(vault, slug, team, pos) or {}
    ex_alloc = prior.get("alloc", {}) or {}
    ex_comments = prior.get("comments", {}) or {}

    with st.form("student_survey"):
        allocs: Dict[str, int] = {}
        comments: Dict[str, str] = {}
        for i, m in teammates:
            st.markdown(f"**{m['name']}**")
            allocs[str(i)] = st.number_input(
                f"Points to {m['name']}", min_value=0, max_value=total,
                value=int(ex_alloc.get(str(i), 0)), step=1, key=f"alloc_{i}")
            if survey.get("ask_public_comment", True):
                comments[str(i)] = st.text_area(
                    survey.get("public_comment_prompt", "Comment"),
                    value=str(ex_comments.get(str(i), "")), key=f"cmt_{i}")
            st.divider()
        conf = ""
        if survey.get("ask_confidential", True):
            conf = st.text_area(survey.get("confidential_prompt",
                                           "Confidential note to the instructor"),
                                value=str(prior.get("confidential", "")))
        running = sum(int(v) for v in allocs.values())
        st.caption(f"Allocated so far: **{running} / {total}**")
        submitted = st.form_submit_button("Submit evaluation", type="primary")

    if submitted:
        running = sum(int(v) for v in allocs.values())
        if running != total:
            st.error(f"Your points add up to {running}, but must total exactly {total}. "
                     "Please adjust and submit again.")
            return
        record = {
            "slug": slug, "team": team, "pos": pos,
            "evaluator": me["name"], "evaluator_key": name_key(me["name"]),
            "submitted": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "alloc": allocs, "comments": comments, "confidential": conf,
        }
        try:
            save_response(vault, slug, team, pos, record)
            st.success("Thank you — your evaluation has been recorded. You may reopen "
                       "this link to revise it until the evaluation closes.")
            st.balloons()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Sorry, we couldn't save your response: {exc}")
