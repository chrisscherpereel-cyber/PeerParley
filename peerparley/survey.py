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
SCALE_LABELS = ["Strongly agree", "Agree", "Somewhat agree",
                "Neither agree nor disagree", "Somewhat disagree",
                "Disagree", "Strongly disagree"]  # 7 → 1

RATING_STATEMENTS = [
    "I consider him/her a team player.",
    "He/she did his/her share of the work on the team.",
    "He/she contributed high quality of work to the team.",
    "The team performed well because of this individual.",
]

RANK_CATEGORIES = ["High Performer", "Adequate Performer", "Low Performer"]

DEFAULT_SURVEY: Dict = {
    "title": "Peer Evaluation",
    "intro": ("You will evaluate each member of your team — including yourself. "
              "For each person, rate the statements, add brief anonymous feedback, "
              "rank their contribution, and split $100 across the team."),

    # ---- Introduction / header ----
    "show_header": True,

    # ---- Rating matrix (4 statements, 7-point agree/disagree) ----
    "ask_ratings": True,
    "ratings_prompt": ("For each team member (including yourself) please indicate the "
                       "degree to which you disagree or agree with the following statements."),
    "rating_statements": list(RATING_STATEMENTS),
    "scale_labels": list(SCALE_LABELS),

    # ---- Qualitative feedback (two questions per member) ----
    "ask_improve": True,
    "improve_prompt": ("If you were {member}, what specific things would you do to "
                       "increase your contribution to the team? (Shared anonymously.)"),
    "ask_contribution": True,
    "contribution_prompt": ("If you were {member}, what would you identify as your most "
                            "significant contributions to the team? (Shared anonymously.)"),

    # ---- Forced ranking ----
    "ask_ranking": True,
    "ranking_prompt": ("Rank each team member's contribution to the team's performance, "
                       "including yourself. Use every category at least once."),
    "ranking_categories": list(RANK_CATEGORIES),

    # ---- Pay allocation ----
    "ask_allocation": True,
    "allocation_prompt": ("You have been given $100 to pay your team for their contribution "
                          "to the project. Allocate a portion to each member (consider both "
                          "performance and effort). It must total exactly $100."),
    "points_total": 100,

    # ---- Your own contribution (released anonymously) ----
    "ask_self_contribution": True,
    "self_contribution_prompt": ("Briefly describe YOUR significant contributions to the "
                                 "team's performance. (Shared anonymously.)"),

    # ---- Confidential note to the instructor ----
    "ask_confidential": True,
    "confidential_prompt": ("Any additional comments for the instructor about your team's "
                            "performance? (Confidential — not released.)"),

    # ---- Scheduling ----
    "is_open": True,          # master switch — off = closed regardless of dates
    "opens_at": "",           # ISO datetime; blank = open as soon as is_open is on
    "closes_at": "",          # ISO datetime; blank = no automatic close
    "closed_note": "This peer evaluation is now closed. Thank you.",
}


# --------------------------------------------------------------------------- #
# Open / close scheduling
# --------------------------------------------------------------------------- #
def parse_dt(value):
    """Parse an ISO datetime string into a naive datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def window_state(survey_cfg: Dict):
    """Return one of 'open', 'not_yet', 'closed', 'disabled' for the survey now.

    Times are compared against the app server's clock (datetime.now()). The
    master `is_open` switch, when off, closes the survey regardless of dates.
    """
    if not survey_cfg.get("is_open", True):
        return "disabled"
    now = datetime.now()
    opens = parse_dt(survey_cfg.get("opens_at"))
    closes = parse_dt(survey_cfg.get("closes_at"))
    if opens and now < opens:
        return "not_yet"
    if closes and now > closes:
        return "closed"
    return "open"


def window_message(survey_cfg: Dict) -> str:
    """A student-facing sentence describing the current window."""
    state = window_state(survey_cfg)
    if state == "not_yet":
        opens = parse_dt(survey_cfg.get("opens_at"))
        return (f"This peer evaluation opens on {opens:%b %d, %Y at %I:%M %p}. "
                "Please come back then.") if opens else "This peer evaluation isn't open yet."
    if state in ("closed", "disabled"):
        return survey_cfg.get("closed_note", "This peer evaluation is now closed. Thank you.")
    closes = parse_dt(survey_cfg.get("closes_at"))
    return (f"Open now — closes {closes:%b %d, %Y at %I:%M %p}." if closes
            else "Open now.")


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
    klass = exact(["class", "course", "class name"]) or contains(["class", "course"])
    section = exact(["section", "sec"]) or contains(["section"])
    return full, first, last, email, team, klass, section


def build_teams(contact_df: pd.DataFrame) -> Dict[str, List[dict]]:
    """Group the contact list into {team: [ordered member records]}.

    Positions are the index in each team's list, sorted by name for stability.
    A member record is {name, first, last, email}. Teams with fewer than two
    members are dropped — a student with no teammates has nothing to evaluate.
    """
    full_c, first_c, last_c, email_c, team_c, class_c, section_c = _contact_columns(contact_df)
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
            "class": str(row.get(class_c, "")).strip() if class_c else "",
            "section": str(row.get(section_c, "")).strip() if section_c else "",
        })
    ordered = {}
    for team, members in teams.items():
        members.sort(key=lambda m: name_key(m["name"]))
        if len(members) >= 2:
            ordered[team] = members
    return dict(sorted(ordered.items(), key=lambda kv: str(kv[0])))


def save_setup(course: str, eval_no: str, contact_df: pd.DataFrame,
               survey_cfg: Dict, vault: Optional[Vault] = None,
               owner: Optional[str] = None):
    """Persist the roster snapshot + survey config for this course/eval.

    `owner` stamps who the survey belongs to; if omitted, any existing owner is
    preserved so a re-save doesn't orphan a section.
    """
    vault = vault or Vault()
    slug = slugify(course, eval_no)
    teams = build_teams(contact_df)
    existing = load_roster_snapshot(vault, slug)
    snap_owner = owner or (existing.get("owner") if existing else "") or ""
    _save_json(vault, key_roster(slug),
               {"course": course, "eval_no": eval_no, "teams": teams, "owner": snap_owner})
    _save_json(vault, key_survey(slug), survey_cfg)
    return slug, teams


def survey_owner(vault: Vault, slug: str) -> Optional[str]:
    """The owner username stamped on a survey, '' if unowned, None if missing."""
    snap = load_roster_snapshot(vault, slug)
    if snap is None:
        return None
    return snap.get("owner", "") or ""


def list_surveys(vault: Optional[Vault] = None):
    """Every survey in the vault, with owner + counts, for the picker."""
    vault = vault or Vault()
    try:
        names = [n for n in vault.list()
                 if str(n).startswith("roster__") and str(n).endswith(".ppj")]
    except Exception:
        names = []
    out = []
    for n in names:
        try:
            snap = _load_json(vault, n)
        except Exception:
            continue
        teams = snap.get("teams", {})
        out.append({"slug": n[len("roster__"):-len(".ppj")],
                    "course": snap.get("course", ""), "eval_no": snap.get("eval_no", ""),
                    "owner": snap.get("owner", "") or "", "teams": len(teams),
                    "students": sum(len(v) for v in teams.values())})
    return sorted(out, key=lambda s: (s["course"], str(s["eval_no"])))


def can_access(owner: Optional[str], user: Optional[dict]) -> bool:
    """Admins reach everything; instructors reach only what they own."""
    if user and user.get("role") == "admin":
        return True
    return bool(user) and bool(owner) and owner == user.get("user")


def visible_surveys(all_surveys, user: Optional[dict]):
    if user and user.get("role") == "admin":
        return all_surveys
    me = (user or {}).get("user")
    return [s for s in all_surveys if s.get("owner") == me]


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


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def _rank_num(category) -> float:
    """Forced-ranking category -> 1 (High) / 2 (Adequate) / 3 (Low)."""
    if not category:
        return float("nan")
    c = str(category).lower()
    if "high" in c:
        return 1.0
    if "low" in c:
        return 3.0
    if "adequate" in c or "expected" in c or "medium" in c:
        return 2.0
    return float("nan")


def self_evaluations(vault: Vault, slug: str) -> Dict:
    """Map (team, name_key) -> the student's self-ratings + self rank, for the
    self-evaluation section of the grade/feedback."""
    from .ingest import name_key as _nk
    snap = load_roster_snapshot(vault, slug)
    if not snap:
        return {}
    teams = snap.get("teams", {})
    out: Dict = {}
    for resp in all_responses(vault, slug):
        team = resp.get("team", "")
        members = teams.get(team, [])
        pos = int(resp.get("pos", -1))
        if pos < 0 or pos >= len(members):
            continue
        self_ans = (resp.get("members") or {}).get(str(pos))
        if not self_ans:
            continue
        out[(team, _nk(members[pos]["name"]))] = {
            "ratings": self_ans.get("ratings"),
            "rank": _rank_num(self_ans.get("rank")),
            "self_contribution": resp.get("self_contribution", ""),
        }
    return out


def responses_to_long(vault: Vault, slug: str) -> pd.DataFrame:
    """Turn collected submissions into the tidy long-format the grader consumes:
    one row per (evaluator -> evaluatee). Points come from the $100 allocation and
    the public comment from the anonymous 'contribution' + 'improve' feedback.
    Handles both the full survey payload (`members`) and the older flat payload."""
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
        conf = resp.get("confidential", "") or ""
        member_ans = resp.get("members")
        first = True

        if member_ans:  # full survey design
            for tpos_str, ans in member_ans.items():
                tpos = int(tpos_str)
                if tpos == pos or tpos >= len(members):
                    continue
                improve = str((ans or {}).get("improve", "") or "").strip()
                contribution = str((ans or {}).get("contribution", "") or "").strip()
                pub = " ".join(t for t in (contribution, improve) if t).strip()
                ratings = (ans or {}).get("ratings") or []
                row = {
                    "evaluator": evaluator, "evaluator_key": ev_key,
                    "evaluator_team": team, "evaluatee": members[tpos]["name"],
                    "evaluatee_key": name_key(members[tpos]["name"]),
                    "points": _num((ans or {}).get("alloc")),
                    "public_comment": pub,
                    "confidential_comment": conf if first else "",
                    "rank": _rank_num((ans or {}).get("rank")),
                }
                for d in range(4):
                    row[f"r{d}"] = _num(ratings[d]) if d < len(ratings) else float("nan")
                rows.append(row)
                first = False
        else:  # legacy flat payload (alloc/comments)
            alloc = resp.get("alloc", {}) or {}
            comments = resp.get("comments", {}) or {}
            for tpos_str, pts in alloc.items():
                tpos = int(tpos_str)
                if tpos == pos or tpos >= len(members):
                    continue
                rows.append({
                    "evaluator": evaluator, "evaluator_key": ev_key,
                    "evaluator_team": team, "evaluatee": members[tpos]["name"],
                    "evaluatee_key": name_key(members[tpos]["name"]),
                    "points": _num(pts),
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

    # ---- Introduction / header --------------------------------------------
    if survey.get("show_header", True):
        klass = me.get("class") or snap.get("course", "")
        section = me.get("section", "")
        bits = [f"**{me['name']}**"]
        if klass:
            bits.append(f"Class: {klass}")
        if section:
            bits.append(f"Section: {section}")
        bits.append(f"Team {team} · Evaluation {snap.get('eval_no', '')}")
        st.caption("  ·  ".join(bits))
        st.caption("You will evaluate the following team members (and yourself): "
                   + ", ".join(m["name"] for m in members))
    else:
        st.caption(f"{snap.get('course', '')} · Evaluation {snap.get('eval_no', '')}")

    state = window_state(survey)
    if state != "open":
        (st.info if state == "not_yet" else st.warning)(window_message(survey))
        return
    if parse_dt(survey.get("closes_at")):
        st.caption(window_message(survey))
    if survey.get("intro"):
        st.markdown(survey["intro"])

    scale = survey.get("scale_labels") or SCALE_LABELS
    statements = survey.get("rating_statements") or RATING_STATEMENTS
    cats = survey.get("ranking_categories") or RANK_CATEGORIES
    total = int(survey.get("points_total", 100))
    prior = load_response(vault, slug, team, pos) or {}
    pmembers = prior.get("members", {}) or {}

    with st.form("student_survey"):
        answers: Dict[str, dict] = {}

        # ---- per-member: ratings + qualitative ----------------------------
        for i, m in enumerate(members):
            who = m["name"] + ("  (yourself)" if i == pos else "")
            st.markdown(f"### {who}")
            pa = pmembers.get(str(i), {}) or {}
            entry: Dict = {}

            if survey.get("ask_ratings", True):
                if i == 0 or survey.get("ratings_prompt"):
                    st.caption(survey.get("ratings_prompt", ""))
                ex_r = pa.get("ratings") or [None] * len(statements)
                rvals = []
                for si, stmt in enumerate(statements):
                    default_idx = None
                    if si < len(ex_r) and ex_r[si]:
                        try:
                            default_idx = 7 - int(ex_r[si])
                        except Exception:
                            default_idx = None
                    choice = st.selectbox(stmt, scale, index=default_idx,
                                          placeholder="Select…", key=f"r_{i}_{si}")
                    rvals.append((7 - scale.index(choice)) if choice in scale else None)
                entry["ratings"] = rvals

            if survey.get("ask_improve", True):
                entry["improve"] = st.text_area(
                    (survey.get("improve_prompt", "") or "").replace("{member}", m["name"]),
                    value=str(pa.get("improve", "")), key=f"imp_{i}")
            if survey.get("ask_contribution", True):
                entry["contribution"] = st.text_area(
                    (survey.get("contribution_prompt", "") or "").replace("{member}", m["name"]),
                    value=str(pa.get("contribution", "")), key=f"con_{i}")
            answers[str(i)] = entry
            st.divider()

        # ---- forced ranking ----------------------------------------------
        if survey.get("ask_ranking", True):
            st.markdown("### Forced ranking")
            st.caption(survey.get("ranking_prompt", ""))
            for i, m in enumerate(members):
                pa = pmembers.get(str(i), {}) or {}
                exr = pa.get("rank")
                who = m["name"] + ("  (yourself)" if i == pos else "")
                choice = st.selectbox(who, cats,
                                      index=(cats.index(exr) if exr in cats else None),
                                      placeholder="Choose…", key=f"rank_{i}")
                answers[str(i)]["rank"] = choice if choice in cats else None
            st.divider()

        # ---- pay allocation ----------------------------------------------
        allocs: Dict[str, int] = {}
        if survey.get("ask_allocation", True):
            st.markdown("### Pay students")
            st.caption(survey.get("allocation_prompt", ""))
            for i, m in enumerate(members):
                pa = pmembers.get(str(i), {}) or {}
                who = m["name"] + ("  (yourself)" if i == pos else "")
                allocs[str(i)] = st.number_input(who, min_value=0, max_value=total,
                                                 value=int(pa.get("alloc", 0) or 0), step=1,
                                                 key=f"alloc_{i}")
                answers[str(i)]["alloc"] = allocs[str(i)]
            st.caption(f"Total allocated: **${sum(int(v) for v in allocs.values())} / ${total}**")
            st.divider()

        # ---- your own contribution ---------------------------------------
        self_contribution = ""
        if survey.get("ask_self_contribution", True):
            st.markdown("### Your contribution")
            self_contribution = st.text_area(survey.get("self_contribution_prompt", ""),
                                             value=str(prior.get("self_contribution", "")),
                                             key="self_contrib")

        # ---- confidential note -------------------------------------------
        conf = ""
        if survey.get("ask_confidential", True):
            st.markdown("### Confidential note to the instructor")
            conf = st.text_area(survey.get("confidential_prompt", ""),
                                value=str(prior.get("confidential", "")), key="conf_note")

        submitted = st.form_submit_button("Submit evaluation", type="primary")

    if submitted:
        if window_state(survey) != "open":
            st.warning(window_message(survey))
            return
        if survey.get("ask_allocation", True):
            running = sum(int(v) for v in allocs.values())
            if running != total:
                st.error(f"Your $ allocations total ${running}, but must total exactly "
                         f"${total}. Please adjust and submit again.")
                return
        if survey.get("ask_ranking", True):
            ranks = [answers[str(i)].get("rank") for i in range(len(members))]
            if any(r is None for r in ranks):
                st.error("Please place every team member in a ranking category.")
                return
            if len(members) >= len(cats) and len(set(ranks)) < len(cats):
                st.error("Use every ranking category at least once.")
                return
        record = {
            "slug": slug, "team": team, "pos": pos,
            "evaluator": me["name"], "evaluator_key": name_key(me["name"]),
            "submitted": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "members": answers, "self_contribution": self_contribution,
            "confidential": conf,
        }
        try:
            save_response(vault, slug, team, pos, record)
            st.success("Thank you — your evaluation has been recorded. You may reopen "
                       "this link to revise it until the evaluation closes.")
            st.balloons()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Sorry, we couldn't save your response: {exc}")
