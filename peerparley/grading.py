"""Grading engine.

Peer-allocation method: each student distributes 100 points across teammates.
Individual score = Team Score x [1 + B * A * Q * (pay_grade - 1)], capped to
configurable min/max multipliers.

  pay_grade   : a student's average received allocation / the TEAM average
                (100% = exactly average). Above average → bonus; below → deduction;
                exactly average → no change. Being relative to the team average, the
                whole team can never be pushed into a deduction by self-allocations.
  B           : global sensitivity (how much peer input moves the grade)
  A           : agreement weight from SD of received points (evaluator consensus, ≤1)
  Q           : comment support score for that student (0..1), from comments.py
                (A and Q only shrink the adjustment; they never flip its sign)

In addition — reproducing the original PeerParley workbook — this engine also
computes, when the survey collected them:

  * Per-dimension letter grades from the 4-statement rating matrix
    (Team Player / Quantity / Quality / Effect), each = mean(rating)/7 → letter.
  * Performance from the FORCED RANKING (High / Adequate / Low) when present,
    otherwise from the allocation ratio (banded ±8%).
  * Pay grade = a student's average received allocation ÷ the team average.
  * Self-evaluation grades from the student's ratings of themselves.

All of the extra inputs are optional: with only allocation + comments (e.g. a
plain Qualtrics export), the engine behaves exactly as before and the extra
fields are simply blank.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .comments import score_comment


# --------------------------------------------------------------------------- #
# Scales
# --------------------------------------------------------------------------- #
GRADE_SCALE: List[Tuple[float, str]] = [
    (0.00, "F"), (0.36, "D"), (0.43, "C-"), (0.50, "C"), (0.57, "C+"),
    (0.64, "B-"), (0.78, "B"), (0.81, "B+"), (0.86, "A-"), (0.93, "A"), (1.00, "A"),
]
DIMENSIONS = ["Team Player", "Quantity", "Quality", "Effect"]


def letter_grade(value) -> str:
    """Map a 0..1 score to a letter grade (blank for missing)."""
    try:
        if value is None or math.isnan(float(value)):
            return ""
    except (TypeError, ValueError):
        return ""
    out = GRADE_SCALE[0][1]
    for thr, lbl in GRADE_SCALE:
        if value >= thr:
            out = lbl
    return out


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass
class GradeSettings:
    team_score_default: float = 100.0
    sensitivity_B: float = 0.5
    min_multiplier: float = 0.85
    max_multiplier: float = 1.15
    max_comment_points: int = 5
    rounding_step: int = 1          # 1 or 5 (percent)
    rounding_mode: str = "nearest"  # nearest | up | down
    performance_method: str = "allocation_ratio"  # allocation_ratio|rank_linear|rank_one_mean
    performance_band: float = 0.08  # +-8%
    agreement_guard: bool = True
    # Which peer measure drives the grade adjustment (the normalized peer factor):
    #   allocation | rating | ranking | combined
    adjustment_source: str = "allocation"
    normalize_raters: bool = False   # z-score each evaluator (corrects for leniency)


def agreement_weight(received: List[float], expected_share: float) -> float:
    vals = [v for v in received if v == v]  # drop NaN
    if len(vals) < 2 or expected_share <= 0:
        return 1.0
    sd = float(np.std(vals, ddof=0))
    frac = sd / expected_share
    if frac <= 0.10:
        return 1.00
    if frac <= 0.20:
        return 0.75
    if frac <= 0.30:
        return 0.50
    return 0.25


def _round_pct(x: float, step: int, mode: str) -> float:
    if step <= 0:
        return round(x, 2)
    q = x / step
    if mode == "up":
        q = np.ceil(q)
    elif mode == "down":
        q = np.floor(q)
    else:
        q = np.round(q)
    return float(q * step)


# --------------------------------------------------------------------------- #
# Result containers
# --------------------------------------------------------------------------- #
@dataclass
class StudentResult:
    name: str
    key: str
    team: str
    team_score: float
    received_total: float
    expected_share: float
    peer_ratio: float
    A: float
    Q: float
    multiplier: float
    individual_score: float
    signed_pct: float           # deviation from team score, signed
    performance: str            # High / Adequate / Expected / Low
    comment_points: int
    submitted_self_eval: bool
    # ---- rating-matrix dimension grades + pay grade (workbook parity) ----
    pay_grade: float = float("nan")
    peer_vals: List[float] = field(default_factory=lambda: [float("nan")] * 4)
    team_player: str = ""
    quantity: str = ""
    quality: str = ""
    effect: str = ""
    forced_rank_mean: float = float("nan")
    # ---- self-evaluation ----
    self_responded: bool = False
    self_vals: List[float] = field(default_factory=lambda: [float("nan")] * 4)
    self_team_player: str = ""
    self_quantity: str = ""
    self_quality: str = ""
    self_effect: str = ""
    # ---- comments received (split for the feedback report) ----
    public_comments: List[str] = field(default_factory=list)
    contributions: List[str] = field(default_factory=list)   # "what teammates valued"
    improvements: List[str] = field(default_factory=list)     # "where to focus next"
    confidential_comments: List[str] = field(default_factory=list)
    received_breakdown: List[float] = field(default_factory=list)
    # ---- feedback the student WROTE (response quality) ----
    response_Q: float = float("nan")        # comment-support of the feedback they gave
    response_points: int = 0                # points earned for their response quality


@dataclass
class TeamResult:
    team: str
    members: List[StudentResult]
    team_score: float


def _performance_label(method, received_avg, team_avg, band, rank, n):
    if method == "rank_linear":
        if n <= 1:
            return "Expected"
        top = rank <= max(1, n // 3)
        bottom = rank > n - max(1, n // 3)
        return "High" if top else ("Low" if bottom else "Expected")
    if method == "rank_one_mean":
        return "High" if rank == 1 else "Expected"
    if team_avg <= 0:
        return "Expected"
    ratio = received_avg / team_avg
    if ratio >= 1 + band:
        return "High"
    if ratio <= 1 - band:
        return "Low"
    return "Expected"


def _forced_performance(mean_rank: float) -> Optional[str]:
    """Forced ranking: 1=High, 2=Adequate, 3=Low → High/Adequate/Low."""
    if mean_rank is None or (isinstance(mean_rank, float) and math.isnan(mean_rank)):
        return None
    v = (3.0 - mean_rank) / 2.0          # rank 1 → 1.0, 2 → 0.5, 3 → 0.0
    if v >= 0.8:
        return "High"
    if v >= 0.4:
        return "Adequate"
    return "Low"


def _num(v):
    try:
        f = float(v)
        return f if not math.isnan(f) else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Core computation
# --------------------------------------------------------------------------- #
def compute(
    long_df: pd.DataFrame,
    settings: GradeSettings,
    team_scores: Optional[Dict[str, float]] = None,
    self_evals: Optional[Dict] = None,
) -> List[TeamResult]:
    """Per-team, per-student results from tidy long-format data.

    Optional long_df columns `r0..r3` (1–7 ratings for the four statements) and
    `rank` (1/2/3 forced ranking) drive the dimension grades and performance when
    present. `self_evals` maps (team, name_key) -> {ratings, rank} for self grades.
    """
    team_scores = team_scores or {}
    self_evals = self_evals or {}
    if long_df.empty:
        return []

    results: List[TeamResult] = []
    ev_team = (
        long_df.dropna(subset=["evaluator_team"])
        .groupby("evaluator_key")["evaluator_team"].first().to_dict()
    )

    def team_of(key: str) -> str:
        return ev_team.get(key, "")

    long_df = long_df.copy()
    long_df["team"] = long_df["evaluator_key"].map(team_of)
    has_ratings = all(f"r{d}" in long_df.columns for d in range(4))
    has_rank = "rank" in long_df.columns

    for team, tdf in long_df.groupby("team"):
        if team == "":
            continue
        member_keys = sorted(set(tdf["evaluatee_key"]) | set(tdf["evaluator_key"]))
        member_keys = [k for k in member_keys if k]
        n = len(member_keys)
        expected_share = 100.0 / (n - 1) if n > 1 else 100.0
        ts = float(team_scores.get(team, settings.team_score_default))
        submitters = set(tdf["evaluator_key"])

        received: Dict[str, List[float]] = {k: [] for k in member_keys}
        pub: Dict[str, List[str]] = {k: [] for k in member_keys}
        contrib: Dict[str, List[str]] = {k: [] for k in member_keys}
        improve: Dict[str, List[str]] = {k: [] for k in member_keys}
        conf: Dict[str, List[str]] = {k: [] for k in member_keys}
        dims: Dict[str, List[List[float]]] = {k: [[], [], [], []] for k in member_keys}
        ranks_recv: Dict[str, List[float]] = {k: [] for k in member_keys}
        authored: Dict[str, List[str]] = {k: [] for k in member_keys}  # comments WRITTEN by k
        display_name: Dict[str, str] = {}
        has_texts = ("contribution_text" in long_df.columns
                     and "improve_text" in long_df.columns)

        for _, r in tdf.iterrows():
            k = r["evaluatee_key"]
            e = r["evaluator_key"]
            if k not in received:
                received[k] = []; pub[k] = []; conf[k] = []
                contrib[k] = []; improve[k] = []
                dims[k] = [[], [], [], []]; ranks_recv[k] = []
            authored.setdefault(e, [])
            if r["points"] == r["points"]:
                received[k].append(float(r["points"]))
            if r.get("public_comment"):
                pub[k].append(str(r["public_comment"]).strip())
                authored[e].append(str(r["public_comment"]).strip())
            if has_texts:
                if r.get("contribution_text"):
                    contrib[k].append(str(r["contribution_text"]).strip())
                if r.get("improve_text"):
                    improve[k].append(str(r["improve_text"]).strip())
            if r.get("confidential_comment"):
                conf[k].append(str(r["confidential_comment"]).strip())
            if has_ratings:
                for d in range(4):
                    v = _num(r.get(f"r{d}"))
                    if v is not None:
                        dims[k][d].append(v)
            if has_rank:
                rk = _num(r.get("rank"))
                if rk is not None:
                    ranks_recv[k].append(rk)
            display_name.setdefault(k, r["evaluatee"])
        for _, r in tdf.iterrows():
            display_name.setdefault(r["evaluator_key"], r["evaluator"])

        received_avgs = {k: (float(np.mean(v)) if v else 0.0) for k, v in received.items()}
        team_avg = float(np.mean([v for v in received_avgs.values()])) if received_avgs else 0.0
        ranking = sorted(member_keys, key=lambda k: received_avgs.get(k, 0), reverse=True)
        rank_of = {k: i + 1 for i, k in enumerate(ranking)}

        # ---- alternative peer-adjustment measures (each a normalized peer factor:
        #      a student's peer score ÷ the team average, so 1.0 = average) --------
        member_rating = {}   # mean of the four 0-1 dimension scores
        for k in member_keys:
            _rv = [float(np.mean(dims[k][d])) / 7.0 for d in range(4) if dims[k][d]]
            member_rating[k] = float(np.mean(_rv)) if _rv else float("nan")
        member_rankscore = {}  # (3 - mean forced rank)/2  → High 1.0 · Adequate 0.5 · Low 0.0
        for k in member_keys:
            member_rankscore[k] = ((3.0 - float(np.mean(ranks_recv[k]))) / 2.0) \
                if ranks_recv[k] else float("nan")

        alloc_source, alloc_team = dict(received_avgs), team_avg
        if settings.normalize_raters:  # z-score each evaluator (removes leniency)
            given: Dict[str, List[float]] = {}
            for _, r in tdf.iterrows():
                v = _num(r.get("points"))
                if v is not None:
                    given.setdefault(r["evaluator_key"], []).append(v)
            zstat = {e: (float(np.mean(g)), float(np.std(g)) if len(g) > 1 else 0.0)
                     for e, g in given.items()}
            zrec: Dict[str, List[float]] = {k: [] for k in member_keys}
            for _, r in tdf.iterrows():
                v = _num(r.get("points"))
                if v is None:
                    continue
                mu, sd = zstat.get(r["evaluator_key"], (0.0, 0.0))
                zrec.setdefault(r["evaluatee_key"], []).append(((v - mu) / sd) if sd > 0 else 0.0)
            alloc_source = {k: (1.0 + float(np.mean(zrec[k]))) if zrec.get(k) else 1.0
                            for k in member_keys}
            alloc_team = 1.0

        _rt = [v for v in member_rating.values() if v == v]
        rating_team = float(np.mean(_rt)) if _rt else float("nan")
        _kt = [v for v in member_rankscore.values() if v == v]
        rank_team = float(np.mean(_kt)) if _kt else float("nan")

        def _factor(x, avg):
            try:
                if x is None or math.isnan(float(x)) or avg is None or math.isnan(float(avg)) \
                        or avg == 0:
                    return None
            except (TypeError, ValueError):
                return None
            return float(x) / float(avg)

        members: List[StudentResult] = []
        for k in member_keys:
            all_pub = pub.get(k, [])
            others = [c for kk, cl in pub.items() if kk != k for c in cl]
            best_q, best_pts = 0.0, 0
            for c in all_pub:
                cs = score_comment(c, others, settings.max_comment_points)
                if cs.q >= best_q:
                    best_q, best_pts = cs.q, cs.points
            Q = best_q if all_pub else 1.0   # neutral when no comments were written about them

            rec = received.get(k, [])
            received_total = float(np.sum(rec)) if rec else 0.0
            ravg = received_avgs.get(k, 0.0)
            A = agreement_weight(rec, expected_share)

            # ---- dimension grades from the rating matrix ----
            peer_vals = [float("nan")] * 4
            for d in range(4):
                if dims[k][d]:
                    peer_vals[d] = float(np.mean(dims[k][d])) / 7.0
            dim_letters = [letter_grade(v) for v in peer_vals]

            # ---- performance: forced ranking if we have it, else allocation ----
            rmean = float(np.mean(ranks_recv[k])) if ranks_recv[k] else float("nan")
            forced = _forced_performance(rmean)
            if forced is not None:
                perf = forced
            else:
                perf = _performance_label(settings.performance_method, ravg, team_avg,
                                          settings.performance_band, rank_of.get(k, n), n)
                if settings.agreement_guard and perf == "Low" and A <= 0.5:
                    perf = "Expected"

            # ---- pay grade = share of the team average ($100 allocation) ----------
            # Always shown to students (100% = an even share). Its ratio also drives
            # the grade when the "allocation" method is chosen.
            pay_grade = (ravg / team_avg) if team_avg > 0 else 1.0

            # ---- rel = the chosen peer factor (1.0 = team average) ----------------
            # A student ABOVE the team average (rel > 1) earns a bonus; BELOW takes a
            # deduction; exactly average gets no change. Being relative to the team
            # average, self-allocations or abstentions can't push a whole team down.
            paf_alloc = _factor(alloc_source.get(k), alloc_team)
            paf_rating = _factor(member_rating.get(k), rating_team)
            paf_rank = _factor(member_rankscore.get(k), rank_team)
            src = settings.adjustment_source
            if src == "rating":
                rel = paf_rating if paf_rating is not None else (paf_alloc or 1.0)
            elif src == "ranking":
                rel = paf_rank if paf_rank is not None else (paf_alloc or 1.0)
            elif src == "combined":
                _parts = [p for p in (paf_alloc, paf_rating) if p is not None]
                rel = (sum(_parts) / len(_parts)) if _parts else 1.0
            else:  # allocation (default)
                rel = paf_alloc if paf_alloc is not None else 1.0
            peer_ratio = rel

            # Grade adjustment centred on the team average, scaled by sensitivity B,
            # then dampened by evaluator agreement A and comment support Q (both ≤ 1,
            # so they only shrink the adjustment — they never flip its sign).
            raw_mult = 1 + settings.sensitivity_B * A * Q * (rel - 1)
            mult = max(settings.min_multiplier, min(settings.max_multiplier, raw_mult))
            individual = ts * mult
            signed = _round_pct(individual - ts, settings.rounding_step, settings.rounding_mode)

            # ---- self evaluation ----
            self_vals = [float("nan")] * 4
            self_responded = False
            se = self_evals.get((team, k))
            if se and se.get("ratings"):
                for d, v in enumerate(se["ratings"][:4]):
                    vv = _num(v)
                    if vv is not None:
                        self_vals[d] = vv / 7.0
                self_responded = any(not math.isnan(x) for x in self_vals)
            self_letters = [letter_grade(v) for v in self_vals]

            # ---- response quality: score the feedback THIS student WROTE ----
            my_written = authored.get(k, [])
            others_written = [c for kk, cl in authored.items() if kk != k for c in cl]
            rq, rpts = 0.0, 0
            for c in my_written:
                cs = score_comment(c, others_written, settings.max_comment_points)
                if cs.q >= rq:
                    rq, rpts = cs.q, cs.points
            response_Q = rq if my_written else float("nan")

            recv_contrib = contrib.get(k, []) if has_texts else list(all_pub)
            recv_improve = improve.get(k, []) if has_texts else []

            members.append(StudentResult(
                name=display_name.get(k, k), key=k, team=team, team_score=ts,
                received_total=round(received_total, 2), expected_share=round(expected_share, 2),
                peer_ratio=round(peer_ratio, 3), A=A, Q=round(Q, 3), multiplier=round(mult, 4),
                individual_score=round(individual, 2), signed_pct=signed, performance=perf,
                comment_points=best_pts, submitted_self_eval=(k in submitters),
                pay_grade=pay_grade, peer_vals=peer_vals,
                team_player=dim_letters[0], quantity=dim_letters[1],
                quality=dim_letters[2], effect=dim_letters[3],
                forced_rank_mean=rmean,
                self_responded=self_responded, self_vals=self_vals,
                self_team_player=self_letters[0], self_quantity=self_letters[1],
                self_quality=self_letters[2], self_effect=self_letters[3],
                public_comments=all_pub, contributions=recv_contrib, improvements=recv_improve,
                confidential_comments=conf.get(k, []),
                received_breakdown=[round(x, 1) for x in rec],
                response_Q=(round(response_Q, 3) if not math.isnan(response_Q) else float("nan")),
                response_points=rpts))
        members.sort(key=lambda m: m.name.lower())
        results.append(TeamResult(team=team, members=members, team_score=ts))

    results.sort(key=lambda t: t.team.lower())
    return results


def _pct(x) -> str:
    try:
        if math.isnan(float(x)):
            return ""
    except (TypeError, ValueError):
        return ""
    return f"{x:.0%}"


def results_to_frame(teams: List[TeamResult]) -> pd.DataFrame:
    rows = []
    for t in teams:
        for m in t.members:
            rows.append({
                "Team": t.team,
                "Name": m.name,
                "Team Player": m.team_player,
                "Quantity": m.quantity,
                "Quality": m.quality,
                "Effect": m.effect,
                "Pay Grade": _pct(m.pay_grade),
                "A (agreement)": m.A,
                "Q (support)": m.Q,
                "Response Pts": m.response_points,
                "Multiplier": m.multiplier,
                "Individual Score": m.individual_score,
                "Grade Δ": f"{'+' if m.signed_pct >= 0 else ''}{m.signed_pct:g}%",
                "Performance": m.performance,
                "Self-eval?": "Yes" if m.submitted_self_eval else "No",
            })
    return pd.DataFrame(rows)
