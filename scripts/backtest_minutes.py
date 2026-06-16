"""Backtest the two-model minutes projection (issue 2.2).

Projects a past season's games / MPG / minutes using ONLY data available before
that season, then compares to what actually happened. Reports:

1. Accuracy: MAE / RMSE on games, MPG, and total minutes.
2. Salary guard (Russ's condition for keeping salary in the MPG model): does the
   salary term systematically push projected minutes TOO HIGH for the
   overpaid-but-low-impact cohort? Reported as the mean signed minutes error
   with the salary term ON vs. neutralized.
3. Durability guard: signed games error for the injury-prone cohort, so the
   games-missed term isn't over- or under-weighting either.

Data sources (all pre-target, no leakage):
- GP / MPG / NET_RATING history: nba_api LeagueDashPlayerStats, prior seasons.
- Age: known before the season (deterministic), taken from the target row.
- Impact proxy: latest prior-season NET_RATING (EPM/DPM isn't available for past
  seasons here; NET_RATING is the available pre-target impact signal).
- Salary share: current contract snapshot as a proxy for the player's pay tier
  (historical salaries aren't in the pipeline). Multi-year veteran deals make
  this a reasonable RELATIVE "expensive vs. cheap" axis, which is all the salary
  guard needs. Documented as a proxy limitation.

Run: ``uv run python scripts/backtest_minutes.py``
"""

from __future__ import annotations

import math
import statistics

from nba_trade_analyzer.data.darko import fetch_darko_data, normalize_name
from nba_trade_analyzer.data.epm import fetch_epm_data
from nba_trade_analyzer.data.players import fetch_player_stats
from nba_trade_analyzer.data.salaries import fetch_all_salaries
from nba_trade_analyzer.engine.constants import (
    MPG_CEILING,
    MPG_FLOOR,
    MPG_IMPACT_COEF,
    MPG_IMPACT_REF,
    MPG_SALARY_COEF,
    MPG_SALARY_REF,
    SALARY_CAP,
)
from nba_trade_analyzer.engine.minutes import (
    project_games,
    project_mpg,
    recency_weighted_games_missed,
    recency_weighted_mpg,
)

HISTORY_SEASONS = ["2021-22", "2022-23", "2023-24"]
TARGET_SEASON = "2024-25"
# Only score players with a real prior-season role and a real target role, so we
# measure the model, not noise from 5-minute end-of-bench cameos.
MIN_HISTORY_MPG = 8.0
MIN_TARGET_GP = 1


def _safe(value, default=float("nan")):
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(f) else f


def _index_by_id(df):
    out = {}
    for rec in df.to_dict(orient="records"):
        pid = rec.get("nba_player_id")
        if pid is None or (isinstance(pid, float) and math.isnan(pid)):
            continue
        out[int(pid)] = rec
    return out


def _salary_share_by_name():
    sal = fetch_all_salaries()
    shares = {}
    for rec in sal.to_dict(orient="records"):
        name = rec.get("player_name")
        salary = _safe(rec.get("salary"), 0.0)
        if isinstance(name, str) and name:
            shares[normalize_name(name)] = salary / SALARY_CAP
    return shares


def _prod_impact_by_name():
    """Production impact (EPM, else DARKO DPM) keyed by normalized name.

    This is the SAME impact source the page prices WAR on, and it mirrors the
    engine's year-1 precedence (EPM first, DARKO as the fallback). Used both as
    the MPG model's impact input and as the cohort classifier, so the salary
    guard runs on the metric production actually trusts — not NET_RATING, which
    mislabels high-usage stars on bad teams as low-impact. Returns
    ``name -> (source, value)``.
    """
    out: dict[str, tuple[str, float]] = {}
    for rec in fetch_epm_data().to_dict(orient="records"):
        key = rec.get("player_name_normalized")
        val = _safe(rec.get("epm"))
        if key and not math.isnan(val):
            out[key] = ("epm", val)
    for rec in fetch_darko_data().to_dict(orient="records"):
        key = rec.get("player_name_normalized")
        if not key or key in out:  # EPM preferred when both exist
            continue
        val = _safe(rec.get("dpm"))
        if not math.isnan(val):
            out[key] = ("darko", val)
    return out


def _metrics(errors):
    errors = [e for e in errors if not math.isnan(e)]
    mae = statistics.fmean(abs(e) for e in errors)
    rmse = math.sqrt(statistics.fmean(e * e for e in errors))
    bias = statistics.fmean(errors)  # signed: + means projection too HIGH
    return mae, rmse, bias


def main() -> None:
    print(f"Fetching {len(HISTORY_SEASONS)} history seasons + target {TARGET_SEASON}...")
    history = {s: _index_by_id(fetch_player_stats(s)) for s in HISTORY_SEASONS}
    target = _index_by_id(fetch_player_stats(TARGET_SEASON))
    salary_share = _salary_share_by_name()
    prod_impact = _prod_impact_by_name()

    rows = []
    for pid, trow in target.items():
        actual_gp = _safe(trow.get("GP"))
        actual_mpg = _safe(trow.get("MPG"))
        if math.isnan(actual_gp) or math.isnan(actual_mpg) or actual_gp < MIN_TARGET_GP:
            continue

        gp_hist, mpg_hist = [], []
        last_net = float("nan")
        for s in HISTORY_SEASONS:  # oldest -> latest
            h = history[s].get(pid)
            if not h:
                continue
            gp = _safe(h.get("GP"))
            mpg = _safe(h.get("MPG"))
            if math.isnan(gp) or math.isnan(mpg):
                continue
            gp_hist.append(gp)
            mpg_hist.append(mpg)
            last_net = _safe(h.get("NET_RATING"), last_net)
        if len(mpg_hist) == 0 or statistics.fmean(mpg_hist) < MIN_HISTORY_MPG:
            continue

        age = int(_safe(trow.get("age"), 27))
        name = trow.get("player_name") or ""
        nkey = normalize_name(name)
        share = salary_share.get(nkey, 0.0)
        prior_mpg = recency_weighted_mpg(mpg_hist) or 0.0

        # Impact INPUT to the MPG model is now EPM/DARKO (production parity);
        # NET_RATING is retained only for the labeled artifact-comparison cohort.
        prod = prod_impact.get(nkey)
        impact_prod = prod[1] if prod else 0.0
        impact_net = last_net if not math.isnan(last_net) else 0.0

        proj_games = project_games(gp_hist, age)
        proj_mpg = project_mpg(prior_mpg, impact_prod, share)
        proj_mpg_no_salary = project_mpg(prior_mpg, impact_prod, MPG_SALARY_REF)

        rows.append(
            {
                "name": name,
                "share": share,
                "impact_prod": impact_prod,
                "impact_prod_source": prod[0] if prod else None,
                "has_prod": prod is not None,
                "impact_net": impact_net,
                "prior_mpg": prior_mpg,
                "missed": recency_weighted_games_missed(gp_hist) or 0.0,
                "proj_games": proj_games,
                "proj_minutes": proj_games * proj_mpg,
                "proj_minutes_no_salary": proj_games * proj_mpg_no_salary,
                "actual_games": actual_gp,
                "actual_minutes": actual_gp * actual_mpg,
                "err_games": proj_games - actual_gp,
                "err_mpg": proj_mpg - actual_mpg,
                "err_minutes": proj_games * proj_mpg - actual_gp * actual_mpg,
            }
        )

    n = len(rows)
    print(f"\nScored {n} players (>= {MIN_HISTORY_MPG} prior MPG, played in target).\n")

    g_mae, g_rmse, g_bias = _metrics([r["err_games"] for r in rows])
    m_mae, m_rmse, m_bias = _metrics([r["err_mpg"] for r in rows])
    t_mae, t_rmse, t_bias = _metrics([r["err_minutes"] for r in rows])
    n_prod = sum(1 for r in rows if r["has_prod"])
    print(f"(MPG model impact input = EPM/DARKO; {n_prod}/{n} matched an EPM/DARKO row)")
    print("ACCURACY (projected - actual; + = projection too high)")
    print(f"  games:   MAE {g_mae:5.1f}  RMSE {g_rmse:5.1f}  bias {g_bias:+5.1f}")
    print(f"  mpg:     MAE {m_mae:5.1f}  RMSE {m_rmse:5.1f}  bias {m_bias:+5.1f}")
    print(f"  minutes: MAE {t_mae:5.0f}  RMSE {t_rmse:5.0f}  bias {t_bias:+5.0f}")

    # --- Salary guard: overpaid-but-low-impact cohort -----------------------
    # "Expensive" uses an ABSOLUTE cap-share cut (>= 15% ~ $23M) so the cohort is
    # credibly overpaid. We run it two ways:
    #   PRIMARY  — "low impact" by EPM/DARKO (what production prices on). Threshold
    #              is MPG_IMPACT_REF (0.0): at/below the model's impact reference
    #              the impact term contributes nothing, so any minutes uplift for
    #              these expensive players is the SALARY term's doing — exactly the
    #              circularity we're testing for.
    #   ARTIFACT — the original NET_RATING cohort, kept for contrast. NET_RATING
    #              mislabels high-usage stars on bad teams as low-impact.
    EXPENSIVE_SHARE = 0.15
    WATCH = {"cade cunningham", "trae young", "lamelo ball"}

    def _report_cohort(label: str, members: list[dict], threshold_desc: str) -> float:
        print(f"\nSALARY GUARD [{label}] — {threshold_desc}: {len(members)} players")
        if not members:
            print("  (empty cohort)")
            return 0.0
        with_salary = statistics.fmean(
            r["proj_minutes"] - r["actual_minutes"] for r in members
        )
        no_salary = statistics.fmean(
            r["proj_minutes_no_salary"] - r["actual_minutes"] for r in members
        )
        contribution = with_salary - no_salary
        print(f"  mean minutes bias WITH salary term:       {with_salary:+6.0f}")
        print(f"  mean minutes bias WITHOUT salary term:    {no_salary:+6.0f}")
        print(f"  salary term's contribution to the bias:   {contribution:+6.0f}")
        verdict = (
            "OVERWEIGHTS — recommend capping the salary coefficient"
            if contribution > 60  # > ~2 MPG over a season
            else "not materially overweighting this cohort (stays in)"
        )
        print(f"  verdict: salary {verdict}")
        watched = sorted(m["name"] for m in members if normalize_name(m["name"]) in WATCH)
        print(f"  watch-list stars in cohort: {watched or 'none'}")
        for r in sorted(members, key=lambda x: -x["share"])[:8]:
            imp = (
                f"epm/darko {r['impact_prod']:+5.1f}"
                if "prod" in label.lower()
                else f"net {r['impact_net']:+5.1f}"
            )
            print(
                f"    {r['name']:<22} share {r['share']:.0%}  {imp}  "
                f"salary-term +{r['proj_minutes'] - r['proj_minutes_no_salary']:4.0f} min"
            )
        return contribution

    epm_cohort = [
        r
        for r in rows
        if r["has_prod"]
        and r["share"] >= EXPENSIVE_SHARE
        and r["impact_prod"] <= MPG_IMPACT_REF
    ]
    _report_cohort(
        "EPM/DARKO (primary)",
        epm_cohort,
        f"share >= {EXPENSIVE_SHARE:.0%} of cap AND EPM/DARKO impact <= {MPG_IMPACT_REF:+.1f}",
    )

    # Coefficient sensitivity on the corrected cohort: how the salary-attributable
    # minutes bias scales with MPG_SALARY_COEF, to ground a cap recommendation.
    def _mpg_at(prior: float, impact: float, share: float, coef: float) -> float:
        val = (
            prior
            + MPG_IMPACT_COEF * (impact - MPG_IMPACT_REF)
            + coef * (share - MPG_SALARY_REF)
        )
        return min(MPG_CEILING, max(MPG_FLOOR, val))

    if epm_cohort:
        print(
            f"\n  salary-coefficient sensitivity on this cohort "
            f"(current MPG_SALARY_COEF = {MPG_SALARY_COEF:g}):"
        )
        for coef in (12.0, 10.0, 8.0, 6.0, 5.0, 4.0):
            contrib = statistics.fmean(
                r["proj_games"]
                * (
                    _mpg_at(r["prior_mpg"], r["impact_prod"], r["share"], coef)
                    - _mpg_at(r["prior_mpg"], r["impact_prod"], MPG_SALARY_REF, coef)
                )
                for r in epm_cohort
            )
            flag = "  <- fires (>60)" if contrib > 60 else ""
            print(f"    coef {coef:4.1f} -> salary-attributable bias {contrib:+6.0f} min{flag}")

    net_cohort = [
        r for r in rows if r["share"] >= EXPENSIVE_SHARE and r["impact_net"] <= -2.0
    ]
    _report_cohort(
        "NET_RATING (artifact comparison)",
        net_cohort,
        f"share >= {EXPENSIVE_SHARE:.0%} of cap AND NET_RATING <= -2.0",
    )

    # --- Durability guard: injury-prone cohort ------------------------------
    missed_sorted = sorted(r["missed"] for r in rows)
    hi_missed = missed_sorted[int(len(missed_sorted) * 2 / 3)]
    injury_prone = [r for r in rows if r["missed"] >= hi_missed]
    ip_bias = statistics.fmean(r["err_games"] for r in injury_prone)
    durable = [r for r in rows if r["missed"] < missed_sorted[int(len(missed_sorted) / 3)]]
    du_bias = statistics.fmean(r["err_games"] for r in durable)
    print(
        f"\nDURABILITY GUARD — games bias (projected - actual):\n"
        f"  injury-prone third (missed >= {hi_missed:.0%}): {ip_bias:+5.1f} games\n"
        f"  durable third:                          {du_bias:+5.1f} games"
    )

    print("\nLargest projected-minutes drops vs. a flat-72 assumption:")
    movers = sorted(rows, key=lambda r: r["proj_games"])[:8]
    for r in movers:
        print(f"  {r['name']:<24} proj_games {r['proj_games']:5.1f}  (missed {r['missed']:.0%})")


if __name__ == "__main__":
    main()
