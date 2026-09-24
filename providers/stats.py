"""Pure-Python statistics for kernel A/B benchmark comparison.

No numpy/scipy dependency. Implements Welch's t-test with a
pure-Python Student's t CDF via the regularized incomplete beta
function (Lentz continued-fraction form).
"""

from __future__ import annotations

import math
from typing import Any


def describe(values: list[float]) -> dict[str, Any]:
    """Compute descriptive statistics for a sample."""
    n = len(values)
    if n == 0:
        return {
            "n": 0,
            "mean": None,
            "stddev": None,
            "cv_pct": None,
            "min": None,
            "max": None,
        }
    mean = sum(values) / n
    if n < 2:
        return {
            "n": n,
            "mean": mean,
            "stddev": None,
            "cv_pct": None,
            "min": min(values),
            "max": max(values),
        }
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    stddev = math.sqrt(variance)
    cv_pct = (stddev / abs(mean) * 100) if mean != 0 else None
    return {
        "n": n,
        "mean": mean,
        "stddev": stddev,
        "cv_pct": cv_pct,
        "min": min(values),
        "max": max(values),
    }


def _log_gamma(x: float) -> float:
    """Stirling approximation of ln(Gamma(x)) for x > 0."""
    return math.lgamma(x)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for incomplete beta (Numerical Recipes)."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, 201):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break
    return h


def _regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """I_x(a, b) via continued fraction."""
    if x < 0 or x > 1:
        raise ValueError(f"x must be in [0, 1], got {x}")
    if x == 0:
        return 0.0
    if x == 1:
        return 1.0
    if x > (a + 1) / (a + b + 2):
        return 1.0 - _regularized_incomplete_beta(b, a, 1.0 - x)
    ln_prefix = (
        _log_gamma(a + b)
        - _log_gamma(a)
        - _log_gamma(b)
        + a * math.log(x)
        + b * math.log(1.0 - x)
    )
    return math.exp(ln_prefix) * _betacf(a, b, x) / a


def student_t_cdf(t: float, df: float) -> float:
    """CDF of Student's t distribution at t with df degrees of freedom."""
    x = df / (df + t * t)
    ibeta = _regularized_incomplete_beta(df / 2.0, 0.5, x)
    if t >= 0:
        return 1.0 - 0.5 * ibeta
    return 0.5 * ibeta


def student_t_ppf(p: float, df: float) -> float:
    """Percent-point function (inverse CDF) of Student's t via bisection."""
    if p <= 0 or p >= 1:
        raise ValueError(f"p must be in (0, 1), got {p}")
    lo, hi = -100.0, 100.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if student_t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def welch(a: list[float], b: list[float]) -> dict[str, float]:
    """Welch's t-test for two independent samples."""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return {"t": float("nan"), "df": float("nan"), "p": float("nan")}

    mean_a = sum(a) / na
    mean_b = sum(b) / nb
    var_a = sum((x - mean_a) ** 2 for x in a) / (na - 1)
    var_b = sum((x - mean_b) ** 2 for x in b) / (nb - 1)

    se2_a = var_a / na
    se2_b = var_b / nb
    denom = se2_a + se2_b
    if denom == 0:
        if mean_a == mean_b:
            return {"t": 0.0, "df": float(na + nb - 2), "p": 1.0}
        return {
            "t": float("inf") if mean_a > mean_b else float("-inf"),
            "df": float(na + nb - 2),
            "p": 0.0,
        }

    t_stat = (mean_a - mean_b) / math.sqrt(denom)
    df = denom**2 / (se2_a**2 / (na - 1) + se2_b**2 / (nb - 1))
    p = 2.0 * (1.0 - student_t_cdf(abs(t_stat), df))
    return {"t": t_stat, "df": df, "p": p}


def compare_samples(
    baseline: list[float],
    candidate: list[float],
    direction: str | None,
    equivalence_pct: float = 2.0,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Compare two sample sets and classify the outcome.

    direction: "higher" means higher is better, "lower" means lower
    is better, None means unknown.

    Returns outcome, delta, pct_change, ci95_pct, t, df, p, significant, reason.
    """
    n_base = len(baseline)
    n_cand = len(candidate)

    if n_base < 2 or n_cand < 2:
        return {
            "outcome": "inconclusive",
            "reason": "insufficient_samples",
            "n_baseline": n_base,
            "n_candidate": n_cand,
        }

    base_desc = describe(baseline)
    cand_desc = describe(candidate)
    w = welch(baseline, candidate)

    mean_b = base_desc["mean"]
    mean_c = cand_desc["mean"]
    delta = mean_c - mean_b

    if mean_b == 0:
        pct_change = None
        ci95_pct = None
    else:
        pct_change = (delta / abs(mean_b)) * 100
        se = math.sqrt(
            (base_desc["stddev"] ** 2 / n_base) + (cand_desc["stddev"] ** 2 / n_cand)
        )
        t_crit = student_t_ppf(1.0 - alpha / 2.0, w["df"])
        margin = t_crit * se / abs(mean_b) * 100
        ci95_pct = (pct_change - margin, pct_change + margin)

    significant = w["p"] < alpha

    if ci95_pct is not None:
        ci_lo, ci_hi = ci95_pct
        if -equivalence_pct <= ci_lo and ci_hi <= equivalence_pct:
            outcome = "unchanged"
            reason = "equivalence"
        elif significant and ci_lo > 0:
            if direction == "higher":
                outcome = "improved"
            elif direction == "lower":
                outcome = "regressed"
            elif direction is None:
                outcome = "changed"
            else:
                outcome = "changed"
            reason = "significant_increase"
        elif significant and ci_hi < 0:
            if direction == "lower":
                outcome = "improved"
            elif direction == "higher":
                outcome = "regressed"
            elif direction is None:
                outcome = "changed"
            else:
                outcome = "changed"
            reason = "significant_decrease"
        else:
            outcome = "inconclusive"
            reason = "underpowered"
    else:
        outcome = "inconclusive"
        reason = "zero_baseline"

    return {
        "outcome": outcome,
        "reason": reason,
        "delta": delta,
        "pct_change": pct_change,
        "ci95_pct": ci95_pct,
        "t": w["t"],
        "df": w["df"],
        "p": w["p"],
        "significant": significant,
        "baseline": base_desc,
        "candidate": cand_desc,
    }


def holm_adjust(p_values: list[float], alpha: float = 0.05) -> list[bool]:
    """Holm-Bonferroni correction. Returns per-test significance."""
    n = len(p_values)
    if n == 0:
        return []
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    results = [False] * n
    for rank, (orig_idx, p) in enumerate(indexed):
        adjusted_alpha = alpha / (n - rank)
        if p >= adjusted_alpha:
            break
        results[orig_idx] = True
    return results
