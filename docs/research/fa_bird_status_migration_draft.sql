-- DRAFT — DO NOT APPLY. fa_bird_status Phase-1 hand table (FA picker).
-- Drafted 2026-07-19 from docs/research/FA_BIRD_RESEARCH.md (the rules
-- substrate) and the house v3_* conventions in
-- databallr_v3/supabase/migrations/20260704120000_add_v3_contract_data.sql:
-- create table if not exists, named CHECK constraints, table comments,
-- indexes, RLS + public read policy, grants, applied BY HAND via the
-- Supabase SQL editor (no migration runner).
--
-- SEMANTICS: this table stores hand-adjudicated CONCLUSIONS (rights class,
-- hold amount), not derivation inputs. Derivation stays human + rubric
-- (FA_BIRD_RESEARCH.md §6 lists the determining facts); every row carries
-- provenance so a later derived pipeline can be diffed against it.
--
-- Writer rules (same as the other v3_* tables, enforced operationally):
--   1. Seeding writes through the dedicated limited role (INSERT/UPDATE only).
--   2. UPSERT only — no path may DELETE or TRUNCATE. Renounced rows are
--      TOMBSTONES (renounced = true), kept forever, filtered by consumers.

-- ---------------------------------------------------------------------------
-- v3_fa_bird_status — one row per (player, season): the player's free-agency
-- rights conclusion for that offseason, held by `team`.
-- ---------------------------------------------------------------------------
create table if not exists public.v3_fa_bird_status (
  id uuid primary key default gen_random_uuid(),

  -- Identity & keying. player_id resolves via v3_players.bbref_slug at seed
  -- time (same as every player-grain v3 table). `team` is the RIGHTS-HOLDING
  -- team in the BBRef salary dialect (BRK/CHO/PHO) — rights transfer whole in
  -- trades/waiver claims (FA_BIRD_RESEARCH.md §1), so this is not necessarily
  -- the team he last played for.
  player_id uuid not null references public.v3_players (id) on delete restrict,
  team text not null,
  season text not null,

  -- Conclusions.
  -- rights_class maps to the CBA formal names (FA_BIRD_RESEARCH.md §1):
  --   full_bird  = Qualifying Veteran Free Agent
  --   early_bird = Early Qualifying Veteran Free Agent
  --   non_bird   = Non-Qualifying Veteran Free Agent
  --   none       = no veteran free agent exception (incl. renounced tombstones)
  rights_class text not null,
  -- fa_type: ufa / rfa are settled statuses; pending_option = an undecided
  -- P/T option gates FA entirely; pending_qo = RFA-eligible, QO not yet
  -- tendered/decided (FA_BIRD_RESEARCH.md §3).
  fa_type text not null,
  -- hold_amount/hold_basis: NULL allowed for renounced tombstones and pending
  -- rows not yet adjudicated. hold_basis names the §2 formula row that
  -- produced the amount; clamped_max/clamped_min record that the min/max
  -- clamp (not the percentage formula) set the final figure.
  hold_amount bigint,
  hold_basis text,

  -- State flags.
  renounced boolean not null default false,
  qo_tendered boolean not null default false,
  qo_amount bigint,
  -- rights_via: how the holding team came to hold the rights (§1 transfer
  -- rules). NULLABLE: unknown/not-yet-adjudicated (spec does not force it).
  rights_via text,
  -- auto_no_trade: the Automatic (implicit) No-Trade Clause created by the
  -- 1-year-contract Bird-reset rule (FA_BIRD_RESEARCH.md §1) — carried here
  -- because it is a conclusion of the same adjudication.
  auto_no_trade boolean not null default false,

  -- Provenance (every row).
  source_note text not null,
  rubric_ref text not null,          -- e.g. '§2 hold table, UFA row'
  adjudicated_at date not null,
  adjudicated_by text not null,
  confidence text not null,

  -- House convention timestamps (updated_at refreshed by the upsert, no
  -- trigger — same as v3_cap_thresholds).
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint v3_fa_bird_status_team_len check (char_length(team) = 3),
  constraint v3_fa_bird_status_season_format check (season ~ '^[0-9]{4}-[0-9]{2}$'),
  constraint v3_fa_bird_status_rights_class_valid check (
    rights_class in ('full_bird', 'early_bird', 'non_bird', 'none')
  ),
  constraint v3_fa_bird_status_fa_type_valid check (
    fa_type in ('ufa', 'rfa', 'pending_option', 'pending_qo')
  ),
  constraint v3_fa_bird_status_hold_basis_valid check (
    hold_basis is null or hold_basis in (
      'rookie_300', 'rookie_250', 'rookie_scale_amount', 'rfa_greatest_of',
      'minimum', 'two_way', 'bird_190', 'bird_150', 'early_130', 'non_120',
      'clamped_max', 'clamped_min'
    )
  ),
  constraint v3_fa_bird_status_rights_via_valid check (
    rights_via is null or rights_via in ('original', 'trade', 'waiver_claim')
  ),
  constraint v3_fa_bird_status_confidence_valid check (
    confidence in ('verified', 'estimate', 'flagged')
  ),
  constraint v3_fa_bird_status_hold_amount_nonneg check (
    hold_amount is null or hold_amount >= 0
  ),
  constraint v3_fa_bird_status_qo_amount_nonneg check (
    qo_amount is null or qo_amount >= 0
  ),
  -- An amount without a basis (or vice versa) is an unfinished adjudication —
  -- both present or both absent.
  constraint v3_fa_bird_status_hold_pair check (
    (hold_amount is null) = (hold_basis is null)
  ),
  constraint v3_fa_bird_status_source_note_nonempty check (char_length(source_note) > 0),
  constraint v3_fa_bird_status_rubric_ref_nonempty check (char_length(rubric_ref) > 0),
  constraint v3_fa_bird_status_adjudicated_by_nonempty check (char_length(adjudicated_by) > 0),

  constraint v3_fa_bird_status_player_season_unique unique (player_id, season)
);

comment on table public.v3_fa_bird_status is
  'Hand-adjudicated free-agency conclusions per (player, season): Bird-rights class (CBA: Qualifying / Early Qualifying / Non-Qualifying Veteran Free Agent), FA type, and cap-hold amount+basis, with per-row provenance to the FA_BIRD_RESEARCH.md rubric. Conclusions, not derivation inputs. Renounced rows are tombstones (renounced=true), never deleted.';

comment on column public.v3_fa_bird_status.team is
  'RIGHTS-HOLDING team, BBRef salary code (BRK/CHO/PHO dialect) — rights transfer in trades/waiver claims, so may differ from the last team played for.';
comment on column public.v3_fa_bird_status.hold_basis is
  'FA_BIRD_RESEARCH.md §2 formula row that produced hold_amount; clamped_max/clamped_min mean the min/max-salary clamp set the figure.';

-- Index beyond the unique key: the picker''s primary read is "all FA rows for
-- one team-season card" (mirrors idx_v3_cap_holds_team_season_player and how
-- the Contract Outlook queries every other per-team dataset), which the
-- (player_id, season) unique cannot serve.
create index if not exists idx_v3_fa_bird_status_team_season
  on public.v3_fa_bird_status (team, season);

-- ---------------------------------------------------------------------------
-- RLS + grants (house pattern: public read, service_role all; the limited
-- ingest/seed role gets INSERT/UPDATE — mirroring the "Write policies for the
-- ingest role" block at the end of 20260704120000_add_v3_contract_data.sql,
-- to be added alongside the other v3_* write policies when applied).
-- ---------------------------------------------------------------------------
alter table public.v3_fa_bird_status enable row level security;

drop policy if exists "Public read access" on public.v3_fa_bird_status;
create policy "Public read access" on public.v3_fa_bird_status for select using (true);

grant select on table public.v3_fa_bird_status to anon, authenticated;
grant all on table public.v3_fa_bird_status to service_role;

-- ============================================================
-- STALENESS NOTE 2026-07-20: this draft no longer matches the
-- live table. Two hand-applied deviations:
--   1. on delete CASCADE → RESTRICT (applied at creation).
--   2. 2026-07-20 ALTER: rights_class, rubric_ref, adjudicated_at,
--      adjudicated_by are now NULLABLE, plus CHECK constraint
--      verified_requires_adjudication (confidence='verified'
--      requires all four adjudication fields NOT NULL).
-- The live database is authoritative; this file is historical.
-- ============================================================
