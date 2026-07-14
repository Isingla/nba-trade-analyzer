"""Tests for the dead-money payload block (Phase 2 Day 2, Part B).

Pure build_dead_money tests over stub charges — the dedupe-by-resolved-player
rule (the Micic name-variant case), totals/rows agreement, the
different-amounts-don't-collapse guard, and the camelCase wire shape.
"""

from __future__ import annotations

from nba_trade_analyzer.export import DeadMoneyCharge, build_dead_money


def _charge(
    team: str,
    season: str,
    name: str,
    amount: int,
    slug: str | None = None,
) -> DeadMoneyCharge:
    return DeadMoneyCharge(
        team=team, season=season, player_name=name, bbref_slug=slug, amount=amount
    )


def test_micic_name_variants_collapse_to_one_charge():
    # The live MIL shape: the same $666,667 charge listed under two raw-name
    # variants (plain and reversed+WAIVED). Must sum ONCE per season.
    charges = [
        _charge("MIL", "2026-27", "Vasilije Micic", 666_667),
        _charge("MIL", "2026-27", "Micic Vasilije WAIVED", 666_667),
        _charge("MIL", "2027-28", "Vasilije Micic", 666_667),
        _charge("MIL", "2027-28", "Micic Vasilije WAIVED", 666_667),
    ]
    block = build_dead_money(charges)

    assert block.totals == {"MIL": {"2026-27": 666_667, "2027-28": 666_667}}
    assert len(block.rows) == 2
    row = block.rows[0]
    # Cleanest display name kept; the merged variant is noted, not lost.
    assert row.player_name == "Vasilije Micic"
    assert row.collapsed_variants == ["Micic Vasilije WAIVED"]


def test_slug_resolution_collapses_even_when_names_differ_wildly():
    charges = [
        _charge("PHX", "2026-27", "Bradley Beal", 19_383_010, slug="bealbr01"),
        _charge("PHX", "2026-27", "Beal Bradley Emmanuel WAIVED", 19_383_010, slug="bealbr01"),
    ]
    block = build_dead_money(charges)
    assert block.totals == {"PHX": {"2026-27": 19_383_010}}
    assert len(block.rows) == 1
    assert block.rows[0].bbref_slug == "bealbr01"


def test_different_amounts_do_not_collapse(caplog):
    # Same resolved player, same team-season, DIFFERENT figures: variant
    # evidence requires an identical amount — keep both, count both.
    charges = [
        _charge("SAC", "2026-27", "DeMar DeRozan", 10_000_000),
        _charge("SAC", "2026-27", "DeRozan DeMar WAIVED", 12_000_000),
    ]
    block = build_dead_money(charges)
    assert len(block.rows) == 2
    assert block.totals == {"SAC": {"2026-27": 22_000_000}}


def test_totals_always_equal_sum_of_rows():
    charges = [
        _charge("MIL", "2026-27", "Damian Lillard", 22_516_603, slug="lillada01"),
        _charge("MIL", "2027-28", "Damian Lillard", 22_516_603, slug="lillada01"),
        _charge("MIL", "2026-27", "Vasilije Micic", 666_667),
        _charge("MIL", "2026-27", "Micic Vasilije WAIVED", 666_667),
        _charge("SAC", "2026-27", "DeMar DeRozan", 10_000_000),
    ]
    block = build_dead_money(charges)

    recomputed: dict[str, dict[str, int]] = {}
    for row in block.rows:
        recomputed.setdefault(row.team, {})
        recomputed[row.team][row.season] = (
            recomputed[row.team].get(row.season, 0) + row.amount
        )
    assert block.totals == recomputed
    assert block.totals["MIL"]["2026-27"] == 22_516_603 + 666_667


def test_wire_shape_is_camel_case_and_additive():
    block = build_dead_money([_charge("MIL", "2026-27", "A", 1, slug="a01")])
    dumped = block.model_dump(by_alias=True)
    assert set(dumped.keys()) == {"note", "totals", "rows"}
    assert set(dumped["rows"][0].keys()) == {
        "team",
        "season",
        "playerName",
        "bbrefSlug",
        "amount",
        "collapsedVariants",
    }


def test_empty_charges_yield_empty_block():
    block = build_dead_money([])
    assert block.totals == {}
    assert block.rows == []
    assert block.note
