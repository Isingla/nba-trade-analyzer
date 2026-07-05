"""Phase 2A contract-data ingest: nightly refresh of the databallr v3_* tables.

Entry point: ``nba-trade-analyzer ingest`` (see ``cli.py``). Orchestration in
``runner.py``; pure decision logic in ``plans.py``/``verify.py`` (unit-tested,
no network, no DB); DB access isolated in ``db.py`` behind the restricted
``$CONTRACT_INGEST_DATABASE_URL`` role (insert/update only — no deletes at
either the code or the role layer).
"""
