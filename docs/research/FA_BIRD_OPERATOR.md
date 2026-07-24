# FA_BIRD_OPERATOR.md — v3_fa_bird_status operator doc

Conventions for hand-adjudicating v3_fa_bird_status rows. Written
2026-07-19, BEFORE row 1. Every row is entered against these rules,
not memory. Rules substrate: docs/research/FA_BIRD_RESEARCH.md
(cbaguide.com authority of record, read 2026-07-19). Migration:
docs/research/fa_bird_status_migration_draft.sql.

## What this table is

Hand-adjudicated CONCLUSIONS about free-agent rights and cap holds —
the Tier-2 archive pattern applied to FA status. One row per player
per season, rights-holding team as an attribute. It does NOT store
derivation inputs (season-by-season accrual history, EAPS inputs,
starter stats); those live in source_note as prose.

## Season semantics (pinned)

season = '2026-27' means THE OFFSEASON ENTERING 2026-27, matching
v3_cap_holds season keying. All Phase-1 rows are '2026-27'.

## Column conventions

rights_class — the player's Bird class with the rights-holding team,
per RESEARCH §1 accrual rules. 'none' only for tombstones and
genuine no-rights cases.

fa_type — what the player IS right now:
ufa = unrestricted FA today
rfa = restricted (QO tendered, ROFR live)
pending_option = under contract, option decision not yet made
pending_qo = rookie-scale 4th-yr (or other RFA-eligible)
finishing; QO not yet tendered
FLIP RULE: pending_qo -> rfa the moment the QO is tendered
(qo_tendered=true preserves the event). fa_type always answers
"right now"; it never means two things.

hold_amount / hold_basis — BOTH or NEITHER (DB-enforced). The basis
names which RESEARCH §2 formula row produced the number. An amount
without a basis is an unfinished adjudication.

CLAMP CONVENTION: when hold_basis is clamped_max/clamped_min, the
source_note MUST name the underlying formula row, e.g.
"bird_190 clamped to max salary (10+ YOS tier)".

renounced — TOMBSTONE flag. Full renounce = renounced=true,
rights_class='none', hold_amount=null, event dated in source_note.
Tombstone rows are KEPT, never deleted — a vanished row is a
silent data change; a tombstone is a receipt.

RENOUNCE-DOWN (Early->Non to escape the 2-yr minimum, RESEARCH §1)
is NOT a tombstone: update in place, rights_class='non_bird',
hold_basis='non_120', renounced stays FALSE (the team downgraded
its exception, it did not give up the player), event in
source_note.

qo_amount — may be filled BEFORE qo_tendered=true (an adjudicated
amount in the June window is a fact; the tender is an event).

rights_via — how the current team got the rights: original / trade /
waiver_claim. Nullable while unadjudicated. Trades/claims carry
the RESEARCH §1 transfer exceptions — check the 1-year-contract
reset (auto_no_trade=true marker) and the waiver-claim
first-season timing before writing 'full_bird'.

source_note — APPEND-ONLY. State changes (rights traded, renounce,
QO events) prepend the new fact with a date and keep the old text:
"rights MIL->BOS via trade 2026-08-05; previously: <old note>".
The row's history lives here until a Phase-2 event table exists.

rubric_ref — the governing RESEARCH section, e.g. "§2 hold table,
UFA row" / "§3 QO matrix" / "§1 Bird Clock".

confidence:
verified — adjudicated against the rubric with sources; would
survive an independent audit (the 8/8 standard)
estimate — pre-filled (scraper) or partially checked; NOT yet
adjudicated
flagged — internals contradict, inputs missing, or the case
lands on a RESEARCH QUESTIONS ambiguity; needs
re-adjudication. Never fake precision.

## Adjudication order (suggested)

1. Minimum-contract vets — mechanical (hold = new minimum, capped at
   2-YOS min); build momentum.
2. Clear Full-Bird UFAs — 190/150 split on EAPS, straightforward.
3. Pending options — status snapshot only; hold pair may stay null.
4. Two-ways — 0-YOS minimum holds.
5. RFAs LAST — QO matrix + greatest-of-three, gnarliest math,
   least externally checkable.

## Known ambiguities (from RESEARCH QUESTIONS — flag, don't guess)

- Hold removal on signing elsewhere: implied, not citable. Treat as
  convention; note it when a row's state depends on it.
- Pre-QO-tender hold basis (RFA gap): if it matters for a row,
  confidence='flagged' with both readings.
- Two-way seasons mixed into Early/Full accrual counting: flag any
  row whose class depends on it.

## Standing rules (inherited, unchanged)

- SQL by Ishaan's hands only. BEGIN/COMMIT one execution,
  verify-SELECT after, key on player_id where possible.
- Fresh two-source verification before any row is marked verified.
- Scraper pre-fill lands as confidence='estimate' with
  source_note="Spotrac claim <date>, unverified" — Spotrac is a
  CLAIM to grade, never an authority. cbaguide remains the rules
  authority; BBRef transactions the events source.
- Disagreement protocol: scraper claim vs your adjudication
  disagree -> flagged, both readings in source_note, never silent
  overwrite.

## Amendment 2026-07-20 — pre-fill rows and Spotrac mapping

- Pre-fill rows (confidence='estimate') MAY leave rights_class,
  rubric_ref, adjudicated_at, adjudicated_by NULL. The DB constraint
  verified_requires_adjudication enforces that confidence='verified'
  requires all four. No row may carry adjudication fields it did not
  earn.
- Write path unchanged: ALL SQL by Ishaan's hands. Scraper tasks
  produce a SQL file for review; they never hold DB credentials.
- Spotrac label → fa_type mapping (pre-fill only):
    "UFA"            → ufa
    "RFA"            → rfa
    "Player Option"  → pending_option
    "Team Option"    → pending_option
    "Club Option"    → pending_option
  Any other label, or any player Spotrac lists ambiguously → OMIT
  from the pre-fill file entirely; goes straight to the hand-
  adjudication queue. Post-deadline option labels are Spotrac
  staleness claims like any other — the pending_option value records
  the claim; adjudication resolves it.
- Spotrac cap-hold figures NEVER land in hold_amount (no derivable
  hold_basis). They go in source_note prose only:
  "Spotrac lists hold $X — unverified, basis unknown."
- Compound Spotrac labels ("UFA / <class>", "RFA / <class>") map on
  the FA-type component before the separator; the class suffix is a
  rights_class CLAIM, preserved in row comments only, never
  prefilled. Ruling 2026-07-20, applied to fa_prefill_2026-07-20.sql.
- Retirement convention (ruling 2026-07-20): a free agent's
  retirement is a STATUS FACT, not a rights event. RESEARCH §4
  lists no hold removal on retirement; the hold persists until the
  team renounces. Convention: fa_type and renounced unchanged, the
  dated retirement fact (with source) prepends source_note,
  confidence = flagged until an actual cap event (renounce ->
  standard tombstone; new contract -> §4 replacement). No new
  columns or enum values; retirement lives in source_note until a
  cap event occurs.
- hold_basis vocabulary (ruling 2026-07-20): the canonical token
  list is the DB constraint v3_fa_bird_status_hold_basis_valid
  (rookie_300, rookie_250, rookie_scale_amount, rfa_greatest_of,
  minimum, two_way, bird_190, bird_150, early_130, non_120,
  clamped_max, clamped_min). Never coin new tokens; a formula row
  with no token means the constraint needs a reviewed ALTER first.
