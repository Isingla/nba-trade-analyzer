#!/usr/bin/env python3
"""Operational entry for the export-reads-DB parity acceptance harness.

Usage (from the repo root, with the restricted role loaded):

    source ~/.config/contract-ingest.env
    uv run python scripts/export_parity.py

Runs the export three times (--source scrape / --source db --no-overrides /
--source db) into TEMP files, normalizes, and prints the P1-P6 assertion
table + a CUTOVER: GO / NO-GO verdict. Exit 0 = GO, 1 = any FAIL, 2 = setup
error. Read-only: never touches the committed snapshots or the DB.

All logic lives in nba_trade_analyzer.parity (unit-tested with stub
payloads); this file is just the script-shaped door.
"""

from nba_trade_analyzer.parity import main

if __name__ == "__main__":
    raise SystemExit(main())
