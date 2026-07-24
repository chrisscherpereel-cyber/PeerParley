"""Grading engine.

Peer-allocation method: each student distributes 100 points across teammates.
Individual score = Team Score x [1 + B * A * Q * (peer_ratio - 1)], capped to
configurable min/max multipliers.

  peer_ratio  : points a student RECEIVED / expected fair share
  B           : global sensitivity (how much peer input moves the grade)
  A           : agreement weight from SD of received points (evaluator consensus)
  Q           : comment support score for that student (0..1), from comments.py

Performance column: selectable method; default = "Allocation ratio"
(avg allocation received / team average, banded +-8%).
An agreement guard softens forced "Low" ratings when evaluator disagreement
is high.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .comments import score_comment


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
    performance_method: str = "allocation_ratio"  # allocation_ratio|composite|rank_linear|rank_one_mean
    performance_band: float = 0.08  # +-8%
    agreement_guard: bool = True


# Agreement weight A: banded by SD of received points as % of expected share
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
    performance: str            # High / Expected / Low (or method label)
    comment_points: int
    submitted_self_eval: bool
    public_comments: List[str] = field(default_factory=list)
    confidential_comments: List[str] = field(default_factory=list)
    received_breakdown: List[float] = field(default_factory=list)


@dataclass
class TeamResult:
    team: str
    members: List[StudentResult]
    team_score: float


def _performance_label(
    method: str, received_avg: float, team_avg: float, band: float,
    rank: int, n: int,
) -> str:
    if method == "rank_linear":
        if n <= 1:
            return "Expected"
        top = rank <= max(1, n // 3)
        bottom = rank > n - max(1, n // 3)
        return "High" if top else ("Low" if bottom else "Expected")
    if method == "rank_one_mean":
        return "High" if rank == 1 else "Expected"
    # allocation_ratio / composite default
    if team_avg <= 0:
        return "Expected"
    ratio = received_avg / team_avg
    if ratio >= 1 + band:
        return "High"
    if ratio <= 1 - band:
        return "Low"
    return "Expected"


# --------------------------------------------------------------------------- #
# Core computation
# --------------------------------------------------------------------------- #
def compute(
    long_df: pd.DataFrame,
    settings: GradeSettings,
    team_scores: Optional[Dict[str, float]] = None,
) -> List[TeamResult]:
    """Compute per-team, per-student results from tidy long-format data."""
    team_scores = team_scores or {}
    if long_df.empty:
        return []

    results: List[TeamResult] = []
    # infer team per student (evaluatee) via evaluator_team of their teammates
    ev_team = (
        long_df.dropna(subset=["evaluator_team"])
        .groupby("evaluator_key")["evaluator_team"].first().to_dict()
    )

    # attach team to each evaluatee (their evaluators share the team)
    def team_of(key: str) -> str:
        return ev_team.get(key, "")

    long_df = long_df.copy()
    long_df["team"] = long_df["evaluator_key"].map(team_of)

    for team, tdf in long_df.groupby("team"):
        if team == "":
            continue
        member_keys = sorted(set(tdf["evaluatee_key"]) | set(tdf["evaluator_key"]))
        member_keys = [k for k in member_keys if k]
        n = len(member_keys)
        expected_share = 100.0 / (n - 1) if n > 1 else 100.0

        ts = float(team_scores.get(team, settings.team_score_default))
        submitters = set(tdf["evaluator_key"])

        # received points per member
        received: Dict[str, List[float]] = {k: [] for k in member_keys}
        pub: Dict[str, List[str]] = {k: [] for k in member_keys}
        conf: Dict[str, List[str]] = {k: [] for k in member_keys}
        display_name: Dict[str, str] = {}

        for _, r in tdf.iterrows():
            k = r["evaluatee_key"]
            if k not in received:
                received[k] = []
                pub[k] = []; conf[k] = []
            if r["points"] == r["points"]:
                received[k].append(float(r["points"]))
            if r.get("public_comment"):
                pub[k].append(str(r["public_comment"]).strip())
            if r.get("confidential_comment"):
                conf[k].append(str(r["confidential_comment"]).strip())
            display_name.setdefault(k, r["evaluatee"])
        for _, r in tdf.iterrows():
            display_name.setdefault(r["evaluator_key"], r["evaluator"])

        received_avgs = {
            k: (float(np.mean(v)) if v else 0.0) for k, v in received.items()
        }
        team_avg = float(np.mean([v for v in received_avgs.values()])) if received_avgs else 0.0
        ranking = sorted(member_keys, key=lambda k: received_avgs.get(k, 0), reverse=True)
        rank_of = {k: i + 1 for i, k in enumerate(ranking)}

        members: List[StudentResult] = []
        for k in member_keys:
            all_pub = pub.get(k, [])
            # comment support Q from best public comment vs others in team
            others = [c for kk, cl in pub.items() if kk != k for c in cl]
            best_q, best_pts = 0.0, 0
            for c in all_pub:
                cs = score_comment(c, others, settings.max_comment_points)
                if cs.q >= best_q:
                    best_q, best_pts = cs.q, cs.points
            Q = best_q if all_pub else 0.5  # neutral when no comment provided

            rec = received.get(k, [])
            received_total = float(np.sum(rec)) if rec else 0.0
            ravg = received_avgs.get(k, 0.0)
            peer_ratio = (ravg / expected_share) if expected_share > 0 else 1.0
            A = agreement_weight(rec, expected_share)

            perf = _performance_label(
                settings.performance_method, ravg, team_avg,
                settings.performance_band, rank_of.get(k, n), n,
            )

            # Agreement guard: soften forced "Low" when evaluators disagree a lot
            if settings.agreement_guard and perf == "Low" and A <= 0.5:
                perf = "Expected"

            raw_mult = 1 + settings.sensitivity_B * A * Q * (peer_ratio - 1)
            mult = max(settings.min_multiplier, min(settings.max_multiplier, raw_mult))
            individual = ts * mult
            signed = _round_pct(individual - ts, settings.rounding_step,
                                settings.rounding_mode)

            submitted = k in submitters
            members.append(StudentResult(
                name=display_name.get(k, k),
                key=k,
                team=team,
                team_score=ts,
                received_total=round(received_total, 2),
                expected_share=round(expected_share, 2),
                peer_ratio=round(peer_ratio, 3),
                A=A, Q=round(Q, 3),
                multiplier=round(mult, 4),
                individual_score=round(individual, 2),
                signed_pct=signed,
                performance=perf,
                comment_points=best_pts,
                submitted_self_eval=submitted,
                public_comments=all_pub,
                confidential_comments=conf.get(k, []),
                received_breakdown=[round(x, 1) for x in rec],
            ))

        members.sort(key=lambda m: m.name.lower())
        results.append(TeamResult(team=team, members=members, team_score=ts))

    results.sort(key=lambda t: t.team.lower())
    return results


def results_to_frame(teams: List[TeamResult]) -> pd.DataFrame:
    rows = []
    for t in teams:
        for m in t.members:
            rows.append({
                "Team": t.team,
                "Name": m.name,
                "Team Score": m.team_score if m.submitted_self_eval else m.team_score,
                "Received (avg vs share)": f"{m.received_total} / {m.expected_share}",
                "Peer Ratio": m.peer_ratio,
                "A (agreement)": m.A,
                "Q (comment)": m.Q,
                "Points": m.comment_points,
                "Multiplier": m.multiplier,
                "Individual Score": m.individual_score,
                "Grade Δ": f"{'+' if m.signed_pct >= 0 else ''}{m.signed_pct:g}%",
                "Performance": m.performance,
                "Self-eval?": "Yes" if m.submitted_self_eval else "No",
            })
    return pd.DataFrame(rows)
