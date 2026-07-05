"""CLI wiring for `ingest --accept-baseline` (Phase 2A follow-up).

No DB, no network: connect_from_env and run_ingest are patched; assertions
cover flag validation order, kwarg pass-through, and the reason landing in
the run summary path.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from nba_trade_analyzer.cli import app
from nba_trade_analyzer.ingest.runner import RunResult

runner = CliRunner()

_CONNECT = "nba_trade_analyzer.ingest.db.connect_from_env"
_RUN = "nba_trade_analyzer.ingest.runner.run_ingest"


def test_empty_reason_is_rejected_before_any_db_connection():
    with patch(_CONNECT) as connect:
        result = runner.invoke(app, ["ingest", "--accept-baseline", "   "])
    assert result.exit_code == 2
    assert "requires a non-empty reason" in result.output
    connect.assert_not_called()


def test_reason_is_passed_through_to_the_runner():
    conn = MagicMock()
    with (
        patch(_CONNECT, return_value=conn),
        patch(_RUN, return_value=RunResult(status="success", summary={})) as run,
    ):
        result = runner.invoke(
            app, ["ingest", "--accept-baseline", "first real ingest over seed"]
        )
    assert result.exit_code == 0
    assert run.call_args.kwargs["accept_baseline"] == "first real ingest over seed"
    conn.close.assert_called_once()


def test_absent_flag_passes_none_current_behavior():
    conn = MagicMock()
    with (
        patch(_CONNECT, return_value=conn),
        patch(_RUN, return_value=RunResult(status="success", summary={})) as run,
    ):
        result = runner.invoke(app, ["ingest"])
    assert result.exit_code == 0
    assert run.call_args.kwargs["accept_baseline"] is None


def test_guard_blocked_still_exits_nonzero_with_flag():
    conn = MagicMock()
    blocked = RunResult(
        status="guard_blocked",
        guard_failures=[{"guard": "empty_source", "subject": "nba_options.csv"}],
    )
    with (
        patch(_CONNECT, return_value=conn),
        patch(_RUN, return_value=blocked),
    ):
        result = runner.invoke(app, ["ingest", "--accept-baseline", "reset"])
    assert result.exit_code == 3
    assert "guard_blocked" in result.output


def test_help_documents_supervised_use_only():
    result = runner.invoke(app, ["ingest", "--help"])
    assert result.exit_code == 0
    out = result.output.replace("\n", " ")
    assert "SUPERVISED" in out
    assert "accept-baseline" in out
