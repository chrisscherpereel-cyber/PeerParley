"""Data ingestion: Qualtrics peer-eval exports + contact roster.

Auto-detects Qualtrics column codes with a fixed-layout fallback, normalizes
names for roster matching, and supports variable team sizes.

Expected Qualtrics question layout (typical PeerParley survey):
  Q22.1_x  -> teammate name (x = 1..N slots)
  Q23.1_x  -> points allocated to teammate x (out of 100 total)
  Q24.1    -> free-text contribution comment (public/anonymous feedback)
  Q24.2    -> free-text confidential comment (instructor only)
Plus respondent identity columns (name / email / team).
"""
from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd


# --------------------------------------------------------------------------- #
# Name normalization
# --------------------------------------------------------------------------- #
def normalize_name(name: str) -> str:
    if not isinstance(name, str):
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9,\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    # "Last, First" -> "first last"
    if "," in s:
        parts = [p.strip() for p in s.split(",", 1)]
        if len(parts) == 2:
            s = f"{parts[1]} {parts[0]}".strip()
    return re.sub(r"\s+", " ", s).strip()


def name_key(name: str) -> str:
    """Order-independent key so 'First Last' == 'Last First'."""
    return " ".join(sorted(normalize_name(name).split()))


# --------------------------------------------------------------------------- #
# File readers
# --------------------------------------------------------------------------- #
def read_table(file, filename: str = "") -> pd.DataFrame:
    name = (filename or getattr(file, "name", "")).lower()
    raw = file.read() if hasattr(file, "read") else file
    buf = io.BytesIO(raw) if isinstance(raw, (bytes, bytearray)) else raw
    if name.endswith((".xlsx", ".xls")):
        return pd.read_excel(buf)
    # Qualtrics CSVs often have 2 extra header rows (labels + importids)
    buf2 = io.BytesIO(raw) if isinstance(raw, (bytes, bytearray)) else buf
    try:
        df = pd.read_csv(buf2)
        if _looks_like_qualtrics_meta(df):
            buf3 = io.BytesIO(raw) if isinstance(raw, (bytes, bytearray)) else buf2
            df = pd.read_csv(buf3, skiprows=[1, 2])
        return df
    except Exception:
        buf.seek(0) if hasattr(buf, "seek") else None
        return pd.read_csv(io.BytesIO(raw))


def _looks_like_qualtrics_meta(df: pd.DataFrame) -> bool:
    if df.empty:
        return False
    first = df.iloc[0].astype(str).str.contains("ImportId", case=False, na=False)
    return bool(first.any())


# --------------------------------------------------------------------------- #
# Column detection
# --------------------------------------------------------------------------- #
@dataclass
class ColumnMap:
    respondent_name: Optional[str] = None
    respondent_email: Optional[str] = None
    respondent_team: Optional[str] = None
    teammate_name_cols: List[str] = field(default_factory=list)   # Q22.1_x
    points_cols: List[str] = field(default_factory=list)          # Q23.1_x
    public_comment_col: Optional[str] = None                      # Q24.1
    confidential_comment_col: Optional[str] = None                # Q24.2
    detected: bool = False
    notes: List[str] = field(default_factory=list)


_NAME_PAT = re.compile(r"^Q22\.1_(\d+)$", re.I)
_PTS_PAT = re.compile(r"^Q23\.1_(\d+)$", re.I)


def detect_columns(df: pd.DataFrame) -> ColumnMap:
    cm = ColumnMap()
    cols = list(df.columns)

    name_slots: Dict[int, str] = {}
    pts_slots: Dict[int, str] = {}
    for c in cols:
        cs = str(c).strip()
        m = _NAME_PAT.match(cs)
        if m:
            name_slots[int(m.group(1))] = c
            continue
        m = _PTS_PAT.match(cs)
        if m:
            pts_slots[int(m.group(1))] = c
            continue
        cl = cs.lower()
        if cs == "Q24.1" or cl in {"q24.1"}:
            cm.public_comment_col = c
        elif cs == "Q24.2" or cl in {"q24.2"}:
            cm.confidential_comment_col = c

    cm.teammate_name_cols = [name_slots[k] for k in sorted(name_slots)]
    cm.points_cols = [pts_slots[k] for k in sorted(pts_slots)]

    # Respondent identity — fuzzy match common Qualtrics / roster headers
    cm.respondent_name = _find(cols, ["recipientname", "respondent", "your name",
                                      "full name", "name", "student"])
    cm.respondent_email = _find(cols, ["recipientemail", "email"])
    cm.respondent_team = _find(cols, ["team", "group", "project team"])

    cm.detected = bool(cm.teammate_name_cols and cm.points_cols)
    if not cm.detected:
        cm.notes.append(
            "Qualtrics codes not found; use the manual column mapper or the "
            "fixed-layout fallback."
        )
    if len(cm.teammate_name_cols) != len(cm.points_cols):
        cm.notes.append(
            f"Name slots ({len(cm.teammate_name_cols)}) != point slots "
            f"({len(cm.points_cols)}); extra slots ignored."
        )
    return cm


def _find(cols, needles) -> Optional[str]:
    low = {str(c).lower().replace("_", " ").strip(): c for c in cols}
    for n in needles:
        for k, orig in low.items():
            if n in k:
                return orig
    return None


# --------------------------------------------------------------------------- #
# Long-format extraction: one row per (evaluator -> evaluatee)
# --------------------------------------------------------------------------- #
@dataclass
class Roster:
    """Maps normalized name key -> contact record."""
    by_key: Dict[str, dict] = field(default_factory=dict)

    @classmethod
    def from_df(cls, df: pd.DataFrame) -> "Roster":
        cols = list(df.columns)
        name_c = _find(cols, ["full name", "name", "student"])
        email_c = _find(cols, ["email"])
        first_c = _find(cols, ["first name", "first"])
        last_c = _find(cols, ["last name", "last"])
        team_c = _find(cols, ["team", "group"])
        r = cls()
        for _, row in df.iterrows():
            if name_c and pd.notna(row.get(name_c)):
                full = str(row[name_c])
            elif first_c and last_c:
                full = f"{row.get(first_c,'')} {row.get(last_c,'')}".strip()
            else:
                continue
            rec = {
                "name": full,
                "first": str(row.get(first_c, "")).strip() if first_c else full.split(" ")[0],
                "last": str(row.get(last_c, "")).strip() if last_c else full.split(" ")[-1],
                "email": str(row.get(email_c, "")).strip() if email_c else "",
                "team": str(row.get(team_c, "")).strip() if team_c else "",
            }
            r.by_key[name_key(full)] = rec
        return r

    def match(self, name: str) -> Optional[dict]:
        return self.by_key.get(name_key(name))


def to_long(df: pd.DataFrame, cm: ColumnMap, roster: Optional[Roster] = None) -> pd.DataFrame:
    """Return tidy rows: evaluator, evaluator_team, evaluatee, points,
    public_comment, confidential_comment."""
    records = []
    n_slots = min(len(cm.teammate_name_cols), len(cm.points_cols))
    for _, row in df.iterrows():
        evaluator = str(row.get(cm.respondent_name, "")).strip() if cm.respondent_name else ""
        team = str(row.get(cm.respondent_team, "")).strip() if cm.respondent_team else ""
        if roster and evaluator:
            m = roster.match(evaluator)
            if m and m.get("team"):
                team = team or m["team"]
        pub = str(row.get(cm.public_comment_col, "")) if cm.public_comment_col else ""
        conf = str(row.get(cm.confidential_comment_col, "")) if cm.confidential_comment_col else ""
        for i in range(n_slots):
            tname = row.get(cm.teammate_name_cols[i])
            pts = row.get(cm.points_cols[i])
            if pd.isna(tname) or str(tname).strip() == "":
                continue
            try:
                pval = float(pts)
            except (TypeError, ValueError):
                pval = float("nan")
            records.append({
                "evaluator": evaluator,
                "evaluator_key": name_key(evaluator),
                "evaluator_team": team,
                "evaluatee": str(tname).strip(),
                "evaluatee_key": name_key(str(tname)),
                "points": pval,
                "public_comment": pub if i == 0 else "",   # comment attributed once
                "confidential_comment": conf if i == 0 else "",
            })
    long_df = pd.DataFrame.from_records(records)
    return long_df


def data_quality_report(long_df: pd.DataFrame, roster: Optional[Roster]) -> dict:
    """Lightweight QA panel data."""
    if long_df.empty:
        return {"rows": 0, "issues": ["No evaluation rows parsed."]}
    issues = []
    teams = long_df["evaluator_team"].replace("", pd.NA).dropna().nunique()
    missing_pts = int(long_df["points"].isna().sum())
    if missing_pts:
        issues.append(f"{missing_pts} allocation cells are blank/non-numeric.")
    # Sum-to-100 check per evaluator
    sums = long_df.groupby("evaluator_key")["points"].sum()
    off = sums[(sums < 95) | (sums > 105)]
    if len(off):
        issues.append(f"{len(off)} evaluators did not allocate ~100 points total.")
    unmatched = 0
    if roster is not None:
        keys = set(long_df["evaluatee_key"]) | set(long_df["evaluator_key"])
        unmatched = sum(1 for k in keys if k and k not in roster.by_key)
        if unmatched:
            issues.append(f"{unmatched} names did not match the roster.")
    return {
        "rows": int(len(long_df)),
        "evaluators": int(long_df["evaluator_key"].nunique()),
        "evaluatees": int(long_df["evaluatee_key"].nunique()),
        "teams": int(teams),
        "unmatched_names": unmatched,
        "issues": issues or ["No blocking issues detected."],
    }
