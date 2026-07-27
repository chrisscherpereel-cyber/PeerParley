# How PeerParley grades — every algorithm explained

All grading lives in `peerparley/grading.py` (`compute`). Defaults are chosen so an
instructor can grade without changing anything. Every input below is optional: with
only the $100 allocation and comments (e.g. a plain Qualtrics export) the engine
still works — the rating/ranking fields are simply left blank.

## Notation
- **n** — team size.
- **received** — the dollars a student was given by *each* teammate.
- **received_avg** — the mean of those.
- **team_avg** — the mean of every member's `received_avg` on that team.
- **team_score** — the grade the team starts at (default **100**).

## 1. Pay grade — the core signal
```
pay_grade = received_avg / team_avg          # 100% = exactly the team average
```
Above 100% → teammates valued this student more than average; below → less. Because
it is measured against the team's **own** average, self-allocations or blank answers
can never push a whole team below 100%.

## 2. Grade adjustment (the ± %)
```
multiplier      = 1 + B · A · Q · (pay_grade − 1)      # clamped to [1−cap, 1+cap]
individual      = team_score · multiplier
grade_Δ (signed_pct) = individual − team_score         # rounded to your step
```
- **Direction is guaranteed:** `pay_grade > 1` → **bonus (+)**, `< 1` → **deduction
  (−)**, `= 1` → **0**. `A` and `Q` are both ≤ 1, so they can only *shrink* the
  adjustment — never flip its sign.
- **B (sensitivity, default 0.5)** — how strongly peer input moves the grade.
- **cap (default ±15%)** — the most any grade can move (set as *Maximum adjustment*).

*Example:* team average $40; a student averaging $70 received → pay grade 175% → a
bonus; one averaging $20 → 50% → a deduction; one at $40 → 100% → no change.

## 3. Agreement weight A (evaluator consensus)
```
A = 1.00 if SD(received)/expected_share ≤ 10%
    0.75 if ≤ 20%
    0.50 if ≤ 30%
    0.25 otherwise         (expected_share = 100/(n−1))
```
If evaluators strongly **disagree** about a student, the signal is noisier, so their
adjustment is softened. (`A = 1` with fewer than two data points.)

## 4. Comment support Q (do the words back up the dollars?)
From `comments.py`, a 0–1 score for the comments *written about* a student, based on
word count ≥ 25, characters ≥ 150, ≥ 2 sentences, unique-word ratio ≥ 0.45, ≥ 2
contribution keywords, with penalties for repetition and copy-paste across
teammates. **With no comments, Q is neutral (1.0)** so the adjustment isn't
dampened for a reason outside the student's control.

## 5. Feedback points (response quality)
The same comment scorer is applied to the feedback a student **wrote** about their
teammates; `points = round(Q_author × max_points)` (default max **5**). This rewards
writing serious, specific feedback — separate from the grade adjustment.

## 6. Dimension letter grades
Each of the four rating statements (Team Player, Quantity, Quality, Effect) is
averaged and divided by 7 → a 0–1 score → a letter via `GRADE_SCALE`
(F … A). Shown to the student so they see *where* they're strong or weak.

## 7. Performance (High / Adequate / Low)
- **Forced ranking** when the survey collected it: mean of the 1/2/3 ranks a student
  received → `(3 − mean)/2` → **High ≥ 0.8**, **Adequate ≥ 0.4**, else **Low**.
- **Otherwise** the average-based label: above/below the team average by a set band
  (default ±8%). The *agreement guard* softens a forced "Low" to "Expected" when
  evaluators disagreed a lot (`A ≤ 0.5`).

## 8. Self-evaluation
The student's own ratings of themselves are graded the same way and shown next to
the peer grades on their feedback sheet, so they can see the gap.

---
The instructor controls the two settings most people touch — **sensitivity B** and
the **maximum adjustment ±%** — on the *Grading rules* tab, with everything else
under *Advanced options*. What each student *sees* on their PDF is controlled
separately under *Set up survey → What students see in their feedback report*.
