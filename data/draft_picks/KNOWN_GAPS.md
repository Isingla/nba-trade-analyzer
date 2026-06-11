<!-- ============================================================================
MIRROR — DO NOT EDIT HERE. Canonical source:
  databallr_v3/lib/draft-picks/seed/KNOWN_GAPS.md
Synced: 2026-06-10  (166 gap entries).
databallr is canonical; refresh by re-copying. Parsed by pick_ownership.py for
Indeterminate verdicts (verbatim RealGM clauses keyed by team/year/round).
============================================================================ -->

# Known gaps — picks intentionally NOT in the seed CSV

Picks from the RealGM future-drafts extraction that were **skipped** during normalization:
conditional conveyances, multi-team most/least-favorable swap chains, and outcomes that
depend on unknown draft positions. The model has no way to represent a conditional owner,
so these are parked here (with the verbatim site quote) until they resolve. Raw provenance
lives in `raw/batch-1-central.md`.

Columns: team (originating, as RealGM prints) · year · round · verbatim site quote · skip reason.

## Detroit (DET)

1. **DET · 2027 · R2** — "Less favorable of BRK and DAL (via DAL to BRK to DET)" — conditional two-team swap; originator ambiguous between BRK and DAL.
2. **DET · 2028 · R2** — "31-55 Own*; 56-60 to PHL; Less favorable of CHA and LAC* (via CHA to DAL); MIA* if DAL conveys 1st round pick to CHA in 2027 (via SAN to DAL); NYK* — *Least favorable of these to UTH" — conditional "least favorable of these to UTH" clause; final owner indeterminate (policy: conditional conveyance excluded).
3. **DET · 2029 · R2** — "Two most favorable of DET, MIL and NYK then other to CHI (via MIL to BRK to DET; via NYK to DET; via SAC to CHI)" — three-team most/least-favorable swap chain. *(Same cluster as MIL 2029 R2 and CHI 2029 R2.)*
4. **DET · 2031 · R2** — "Less favorable of GOS and MIN (via MIN swap for GOS)" — conditional two-team swap; originator indeterminate.

## Cleveland (CLE)

1. ~~**CLE · 2026 · R1**~~ — **✅ RESOLVED (2026-06-10, batch 3 / ATL page).** Original gap: "To ATL (via SAN swap for ATL; via ATL swap of SAN for CLE); SAN #29 (via SAN swap for ATL; via ATL swap of SAN for CLE)" — three-way swap chain (SAN/ATL/CLE); CLE's own outgoing pick had no single determinable owner. **Resolution:** ATL's page shows the concrete outcome **"CLE #23 (via SAN swap for ATL; via ATL swap of SAN for CLE)"** → CLE's 2026 R1 conveys to **ATL**. Row `CLE,ATL,2026,1` is now in the CSV (batch 3). The full 3-way is captured: ATL→SAN, SAN→CLE (batch 1, `SAN,CLE,2026,1`), CLE→ATL (batch 3). Gap-resolution-by-concrete-number precedent.
   > ✅ **Watch-note CLOSED (2026-06-10, batch 5 / SAN page).** SAN's own page confirms the adjudication verbatim: **"To CLE (via SAN swap for ATL; via ATL swap of SAN for CLE); ATL #20 (via SAN swap for ATL)"** — SAN's 2026 R1 conveys to CLE (the `SAN,CLE,2026,1` row), and what *arrives* at SAN is ATL's #20, not SAN's own. No second owner; the `"(via X)"` provenance tripwire is resolved. (Original watch: the row rested on the same `"(via X)"` notation as the HOU/BRK 2026 contradiction — that risk is now retired.)
2. **CLE · 2027 · R1** — "Most favorable of CLE, MIN and UTH to MEM; second most favorable to UTH and least favorable to PHX (via UTH)" — three-team most/least-favorable conditional; owner depends on draft positions.
3. **CLE · 2028 · R1** — "Least favorable of CLE, UTH and ATL; more favorable of CLE and UTH to UTH; more favorable of (i) ATL and (ii) less favorable of CLE and UTH to ATL (via UTH swap for CLE; via ATL swap for CLE or UTH)" — three-team conditional swap; owner indeterminate.
   > **UTH 2028 R1 swap clause (batch 4b, attached here — ruling-1 reclassification):** UTH's page shows the simpler "Own or swap for CLE **[CLE may convey UTH to ATL]**", which looks bilateral, but it is the same pick described inside *this* gapped three-team CLE/UTH/ATL block — the outright CLE 2028 R1 it would reference is indeterminate (no outright CLE 2028 R1 row exists). Per ruling 2's precedent: UTH's **own** row `UTH,UTH,2028,1` is emitted, but **no `CLE,UTH,2028,1,swap=true` row** — the swap clause lives here (it would otherwise orphan).
4. **CLE · 2029 · R1** — "Most / two most favorable of CLE, MIN 6-30 and UTH to UTH then other to CHA (via UTH to PHX)" — three-team conditional swap; owner indeterminate.
5. **CLE · 2031 · R2** — "More favorable of CLE and BOS to UTH then other to BOS (via CLE to ATL to BOS)" — conditional two-pick swap; outcome depends on draft positions.

## Indiana (IND)

1. **IND · 2027 · R2** — "Most favorable of IND, OKC, HOU and MIA to PHL; second most favorable to NOP and third most favorable to NYK; more favorable of (i) SAN and (ii) least favorable of IND, OKC, HOU and MIA to SAN then least favorable of all to MIA (via HOU to DET to OKC to NYK to NOP; via MIA to OKC to UTH to SAN to MIA; via OKC to PHL)" — five-team most/least-favorable chain; owner indeterminate.
2. **IND · 2028 · R2** — "More favorable of IND and PHX then other to NYK (via IND)" — conditional two-team swap; owner depends on draft positions.
3. **IND · 2029 · R2** — "More favorable of IND and WAS then other to POR (via IND to NYK)" — conditional two-team swap; owner depends on draft positions.
4. **IND · 2031 · R2** — "Least favorable of IND, MIA and MEM; more favorable of IND and MIA to WAS; more favorable of (i) MEM and (ii) less favorable of IND and MIA to MEM (via MIA swap for IND; via UTH to WAS; via MEM swap for IND or MIA)" — three-team conditional chain; owner indeterminate.

## Milwaukee (MIL)

1. **MIL · 2027 · R1** — "More favorable of MIL and NOP to NOP then other to ATL if 5-30 or MIL and NOP to NOP if both 1-4 (via NOP)" — conditional swap with positional clause; outcome indeterminate.
2. **MIL · 2028 · R1** — "Less favorable of (i) less favorable of MIL and POR and (ii) more favorable of (a) WAS and (b) least / less favorable of BRK, PHL 9-30 and PHX then more favorable of (i) and (ii) to WAS (via POR swap for MIL; via BRK swap of BRK or PHL for PHX; via WAS swap for PHX, BRK or PHL; via WAS swap of WAS, BRK, PHL or PHX for MIL or POR)" — deeply nested multi-team conditional chain. **Stays UNRESOLVED.**
   > **POR 2028 R1 swap clause (batch 4, attached here):** POR's page shows "Own or swap for MIL **[WAS then has complex swap rights with MIL]**". The bracketed note confirms the POR↔MIL swap *participates in this same nested chain* — it is a multi-team conditional wearing bilateral clothing, not a clean bilateral swap. Per the ruling-2 convention, POR's **own** row (`POR,POR,2028,1`) is emitted but **no `MIL,POR,2028,1,swap=true` row** — the swap clause lives here. (It would otherwise orphan: MIL's outright 2028 R1 owner is exactly what this gap leaves indeterminate.)
3. **MIL · 2029 · R1** — "Most and least favorable of MIL, BOS and POR to POR; second most favorable to WAS (via BOS to POR; via MIL to POR; via POR to WAS)" — three-team most/least-favorable chain.
4. **MIL · 2029 · R2** — "Two most favorable of MIL, DET and NYK to DET then other to CHI (via MIL to BRK to DET; via NYK to DET; via SAC to CHI)" — three-team conditional chain. *(Same cluster as DET 2029 R2 and CHI 2029 R2.)*
5. ~~**MIL · 2030 · R1**~~ — **✅ RESOLVED (2026-06-10, batch 4b / POR page).** "Own or POR (via POR swap for MIL)" — the "Own" portion `MIL,MIL,2030,1` was already in the CSV; the swap portion is now emitted as `MIL,POR,2030,1,,true,`, cross-confirmed by POR's page quote **"Own or swap for MIL"** (2030 R1). The two form a valid own+swap pair (both key `MIL 2030 R1`), so the swap clause is fully represented — clean bilateral, unlike the 2028 R1 chain (entry #2, still unresolved).

## Chicago (CHI)

1. **CHI · 2029 · R2** — "Least favorable of DET, MIL and NYK (via MIL to BRK to DET; via NYK to DET; via SAC to CHI)" — three-team least-favorable conditional; originator and owner indeterminate. *(Same cluster as DET 2029 R2 and MIL 2029 R2.)*
2. **CHI · 2031 · R2** — "More favorable of MIN and GOS (via MIN swap for GOS)" — conditional two-team swap; originator indeterminate.
3. **CHI · 2032 · R2** — "More favorable of PHX and HOU (via PHX to MIN)" — conditional two-team swap; originator indeterminate.

---

**Cross-reference:** the 2029 R2 **DET / MIL / NYK → CHI** swap cluster appears three times above
(DET #3, MIL #4, CHI #1) — it's one underlying conditional swap described from each team's page.
When it resolves it yields a single pick, not three.

---

# Batch 2 — Atlantic (BOS NYK PHL BRK)

Raw provenance: `raw/batch-2-atlantic.md`.

## Boston (BOS)

1. **BOS · 2027 · R2** — "More favorable of BOS and ORL to UTH then other to CHA (via BOS to ORL to BOS; via ORL to CHA)" — conditional two-team swap; owner (UTH or CHA) depends on draft positions.
2. **BOS · 2028 · R2** — "31-45 to SAN if BOS 1 in 2028" — range + conditional on BOS picking #1 overall.
3. **BOS · 2028 · R2** — "46-60 to NYK (via ORL to PHX)" — range-conditional conveyance (picks 46-60 only); same pick as NYK's "BOS 46-60".
4. **BOS · 2029 · R1** — "Most and least favorable of BOS, POR and MIL to POR; second most favorable to WAS (via BOS to POR; via MIL to POR; via POR to WAS)" — three-team most/least-favorable chain.
5. **BOS · 2030 · R2** — "CHA 56-60" — range-conditional; BOS receives CHA's pick only if 56-60.
6. **BOS · 2031 · R2** — "Less favorable of BOS and CLE then other to UTH (via CLE to ATL to BOS)" — conditional two-team swap, multi-leg routing.
7. **BOS · 2031 · R2** — "HOU 56-60" — range-conditional; BOS receives HOU's pick only if 56-60.

## New York (NYK)

1. **NYK · 2027 · R2** — "Third most favorable of OKC, HOU, IND and MIA (via HOU to DET to OKC)" — four-team conditional chain; originator indeterminate.
2. **NYK · 2028 · R1** — "Least favorable of NYK, BRK and PHX ... [nested BRK / PHL 9-30 / PHX / NYK block] (via BRK swap of BRK or PHL for PHX; via BRK swap of BRK or PHX for NYK; via WAS swap for PHX, BRK or PHL)" — deeply nested multi-team conditional; owner indeterminate.
3. **NYK · 2028 · R2** — "Less favorable of IND and PHX (via IND)" — conditional two-team swap; originator indeterminate.
4. **NYK · 2029 · R2** — "Two most favorable of NYK, DET and MIL to DET then other to CHI (via NYK to DET; via MIL to BRK to DET; via SAC to CHI)" — three-team conditional chain.

## Philadelphia (PHL)

1. **PHL · 2027 · R2** — "More favorable of GOS and PHX (via WAS)" — conditional two-team swap; originator (GOS or PHX) indeterminate.
2. **PHL · 2027 · R2** — "Most favorable of OKC, HOU, IND and MIA to PHL; second most favorable to NOP and third most favorable to NYK ... (via HOU to DET to OKC; via MIA to OKC; via OKC to PHL)" — four-team most/least-favorable chain; originator indeterminate.
3. **PHL · 2028 · R1** — "1-8 Own; [PHL 9-30 BRK / PHX / NYK / WAS / MIL conditional block] (via BRK swap of BRK or PHL for PHX; via BRK swap of BRK or PHX for NYK; via WAS swap for PHX, BRK or PHL)" — the 9-30 branch is a deeply nested multi-team conditional (gapped); owner indeterminate.
   > **Asymmetry note (top-8 vs blank):** the emitted CSV row `PHL,PHL,2028,1,top-8 protected` carries a protection while BOS 2028 R1 was emitted blank — deliberately. BOS *retains* its pick in every branch of its construct (own, or via the `BOS,SAN` swap row), so there is no protection. PHL's 9-30 branch *conveys the pick away* (this gap), so "1-8 Own" is genuine **top-8 protection** semantics — priced at the `0.70` non-canonical default (top-8 isn't in the discount table).
   > **ORL 2029 R1 (batch 3) — same family, blank side:** `ORL,ORL,2029,1` is emitted **blank** (not the verbatim "1-2 Own; 3-30 Own or MEM" text). ORL retains a first in *every* branch — 1-2 own, 3-30 own-or-MEM's-swap — so there is no conveyance and no protection; the `ORL,MEM,2029,1` swap row carries the conditionality. Same precedent as **BOS 2028 R1** (retain-in-every-branch → blank), the opposite of PHL 2028 R1 (conveys-away → real protection).
4. **PHL · 2028 · R2** — "To BRK if PHL 1-8 in 2028" — conditional on PHL picking 1-8 overall.
5. **PHL · 2028 · R2** — "DET 56-60" — range-conditional; conditional on DET drafting 56-60.
6. **PHL · 2030 · R2** — "More favorable of PHX and POR (via WAS)" — conditional two-team swap; originator (PHX or POR) indeterminate.

## Brooklyn (BRK)

1. **BRK · 2027 · R2** — "More favorable of BRK and DAL to WAS then other to DET (via DAL to BRK to DET; via DET to WAS)" — conditional two-team swap; owner (WAS or DET) indeterminate.
2. **BRK · 2027 · R2** — "LAL if LAL conveys 1st round pick to UTH in 2027" — conditional on LAL's conveyance (external event); the `LAL,BRK,2027,2` row is therefore OUT of the CSV.
3. **BRK · 2028 · R1** — "If (i) PHL 9-30 is third most favorable and (ii) NYK is most or second most favorable of BRK, PHL 9-30, PHX and NYK ... [nested block] (via BRK swap of BRK or PHL for PHX; via BRK swap of BRK or PHX for NYK; via WAS swap for PHX, BRK or PHL)" — deeply nested multi-team conditional; quantity and origin indeterminate.
4. **BRK · 2028 · R2** — "PHL if PHL 1-8 in 2028" — conditional on PHL picking 1-8; the `PHL,BRK,2028,2` row is OUT of the CSV / in this gap.
5. **BRK · 2029 · R1** — "Least favorable of DAL, PHX and HOU (via DAL and PHX to BRK; via DAL or PHX to HOU; via HOU swap for DAL or PHX)" — three-team least-favorable chain; originator indeterminate.

## Adjudications

### HOU 2026 R1 — resolved (batch 2, ruling)

A uniqueness contradiction: HOU's 2026 first-rounder was claimed by two owners across pages.
**Resolution — DROP `HOU,BRK,2026,1`; KEEP `HOU,PHL,2026,1` and `BRK,BRK,2026,1`.**

- PHL page: **"HOU #22 (via OKC)"** — concrete pick number + explicit routing → the better-evidenced claim that HOU's pick conveys to PHL.
- BRK page: **"BRK (Own) #6 (via HOU)"** — this is **Brooklyn's OWN** 2026 first-rounder (with `(via HOU)` provenance notation), *not* an HOU→BRK conveyance. The raw mis-read the provenance as a second owner of HOU's pick.

After adjudication, `HOU 2026 R1` has a single owner (PHL) and `BRK 2026 R1` is Brooklyn's own pick —
two distinct picks, no contradiction. This is the `"(via X)"`-notation hazard flagged on the
SAN→CLE 2026 entry above.

---

# Batch 3 — Southeast (MIA ORL ATL WAS)

Raw provenance: `raw/batch-3-southeast.md` (CHA = batch 3b; WAS Section B truncated, gaps
recovered from the surviving inline year-by-year notes).

## Miami (MIA)

1. **MIA · 2027 · R1** — "1-14 Own; 15-30 to CHA" — range-conditional split; owner depends on draft position.
2. **MIA · 2027 · R2** — "Least favorable of MIA, OKC, HOU, IND and SAN; most favorable of MIA, OKC, HOU and IND to PHL; second most favorable to NOP and third most favorable to NYK ... (via MIA to OKC to UTH to SAN to MIA; via HOU to DET to OKC to NYK to NOP; via OKC to PHL)" — five-team most/least-favorable chain; owner indeterminate.
3. **MIA · 2028 · R1** — "To CHA if not already settled" — conditional on whether MIA's 2027 R1 (15-30 scenario) already conveyed.
4. **MIA · 2028 · R2** — "To DET if DAL conveys 1st round pick to CHA in 2027 or to CHA if DAL does not convey ... [DET may convey to UTH] (via SAN to DAL)" — either/or conditional; owner indeterminate.
5. **MIA · 2029 · R2** — "More favorable of MIA and ATL to CHA then other to OKC (via OKC)" — conditional two-team swap; owner (CHA or OKC) indeterminate.
6. **MIA · 2031 · R2** — "More favorable of MIA and IND to WAS; more favorable of (i) MEM and (ii) less favorable of MIA and IND to MEM then least favorable of all to IND (via MIA swap for IND; via UTH to WAS; via MEM swap for IND or MIA)" — three-team chain; owner indeterminate.

## Orlando (ORL)

1. **ORL · 2027 · R2** — "More favorable of ORL and BOS to UTH then other to CHA (via BOS to ORL to BOS; via ORL to CHA)" — conditional two-team swap; owner (UTH or CHA) indeterminate.
2. **ORL · 2028 · R2** — "More favorable of LAL and WAS (via LAL)" — conditional two-team swap (incoming); originator (LAL or WAS) indeterminate.
3. **ORL · 2029 · R2** — "To MEM if ORL 1-2 in 2029" — range-conditional; conveys only if ORL picks 1-2.
4. **ORL · 2031 · R2** — "More favorable of ORL and NOP then other to OKC (via ORL swap for NOP)" — conditional two-team swap; owner (ORL or OKC) indeterminate.

## Atlanta (ATL)

1. **ATL · 2027 · R1** — "Less favorable of MIL and NOP if either or both 5-30 (via NOP)" — conditional two-team + range clause; originator and existence indeterminate.
2. **ATL · 2028 · R1** — "More favorable of (i) ATL and (ii) less favorable of CLE and UTH then least favorable of all to CLE (via UTH swap for CLE; via ATL swap for CLE or UTH)" — three-team conditional chain; owner indeterminate.
3. **ATL · 2029 · R2** — "More favorable of ATL and MIA to CHA then other to OKC (via OKC)" — conditional two-team swap; owner (CHA or OKC) indeterminate.
4. **ATL · 2031 · R2** — "Own or swap for HOU 31-55" (swap portion) — range-conditional swap right; the "Own" portion is in the CSV, the 31-55 swap is gapped.

## Washington (WAS)

(WAS Section B was truncated in the raw; these are recovered verbatim from the surviving inline notes — all standard multi-team/conditional classes.)

1. **WAS · 2027 · R2** — "More favorable of BRK and DAL (via DAL to BRK to DET; via DET to WAS)" — conditional two-team swap; originator (BRK or DAL) indeterminate.
2. **WAS · 2027 · R2** — "Less favorable of GOS and PHX" — conditional two-team swap; originator indeterminate.
3. **WAS · 2028 · R1** — "More favorable of (i) more favorable of (a) WAS and (b) least / less favorable of BRK, PHL 9-30 and PHX and (ii) less favorable of MIL and POR ... least favorable of WAS, PHL 9-30, BRK and PHX to PHX (via POR swap for MIL; via BRK swap of BRK or PHL for PHX; via WAS swap for PHX, BRK or PHL; via WAS swap of WAS, BRK, PHL or PHX for MIL or POR)" — deeply nested multi-team chain; owner indeterminate.
4. **WAS · 2028 · R2** — "Less favorable of WAS and LAL then other to ORL (via WAS to LAL to ORL; via LAL to WAS)" — conditional two-team swap; owner indeterminate.
5. **WAS · 2028 · R2** — "DEN 34-60 (via SAN to SAC)" — range-conditional (picks 34-60 only), multi-leg routing.
6. **WAS · 2029 · R1** — "Second most favorable of POR, BOS and MIL (via BOS to POR; via MIL to POR; via POR to WAS)" — three-team conditional chain; originator indeterminate.
7. **WAS · 2029 · R2** — "More favorable of WAS and IND to IND then other to POR (via IND to NYK)" — conditional two-team swap; owner indeterminate.
8. **WAS · 2030 · R1** — "More favorable of WAS and PHX; more favorable of (i) MEM and (ii) less favorable of WAS and PHX to MEM then least favorable of all to PHX (via WAS swap for PHX; via MEM swap for WAS or PHX)" — three-team conditional chain; owner indeterminate.
9. **WAS · 2030 · R2** — "Less favorable of PHX and POR" — conditional two-team swap; originator indeterminate.
10. **WAS · 2031 · R2** — "More favorable of MIA and IND (via MIA swap for IND; via UTH)" — conditional two-team swap; owner indeterminate.

---

# Batch 4 — Northwest (OKC DEN MIN, POR thru 2028 R1)

Raw provenance: `raw/batch-4-northwest.md` (POR truncated mid-2028 R2; UTH absent — both batch 4b).

> **Convention (ruling 2, batch 4) — "Own or swap" with a MULTI-TEAM conditional swap side.**
> When the conveyance side of an "X Own; …" pick is a three-or-more-pick conditional chain
> (not a clean bilateral swap), emit the **own row with the site-stated protection** (the range
> bounds what the team keeps, e.g. "1-5 Own" → `top-5 protected`, "1 Own" → `top-1 protected`)
> and **gap the chain** — emit **NO `swap=true` row**, because the bilateral swap convention
> ("owner = holder, team = pick that may be taken") can only represent a two-pick swap.
> Applied to: DEN 2027/2028/2029/2030 R1 (`top-5 protected`), MIN 2029 R1 (`top-5`), MIN 2030 R1
> (`top-1`), and the POR 2028 R1 swap clause (attached to the MIL 2028 R1 chain above). This is
> the conveys-away cousin of the BOS/PHL/ORL retain-in-every-branch asymmetry notes.

> **Convention (ruling 4, batch 5) — own-pick `via` is provenance noise.** A `via_team` on a row
> where `team == owner_team` ("Own (via ATL)") describes how the pick routed *back* to its owner;
> it conveys nothing about ownership/value. **Blank the `via_team`** in the CSV (the full chain
> stays only in the raw archive). Applied to `HOU,HOU,2028,2` (raw: "Own (via ATL)").

## Oklahoma City (OKC)

1. **OKC · 2027 · R1** — "Two most / more favorable of OKC, DEN 6-30 and LAC then other to LAC (via OKC swap of OKC or DEN for LAC)" — three-pick multi-team conditional; owner and quantity indeterminate.
2. **OKC · 2027 · R1** — "SAN 17-30 (via SAC)" — range-conditional; OKC receives SAN's pick only if 17-30.
3. **OKC · 2027 · R2** — "Most favorable of OKC, HOU, IND and MIA to PHL; second most favorable to NOP and third most favorable to NYK ... (via HOU to DET to OKC to NYK to NOP; via MIA to OKC to UTH to SAN to MIA; via OKC to PHL)" — five-team chain; owner indeterminate.
4. **OKC · 2027 · R2** — "CHA if SAN 1-16 in 2027 (via SAC)" — conditional on an external event.
5. **OKC · 2027 · R2** — "SAC if SAN 1-16 in 2027" — conditional on an external event.
6. **OKC · 2028 · R1** — "DEN 6-30 if not already settled" — conditional + range.
7. **OKC · 2029 · R1** — "DEN 6-30 if not already settled; or DEN 6-30 if DEN conveys a first potential 1st round pick to OKC in 2027" — conditional + range + prior-year event.
8. **OKC · 2029 · R2** — "Less favorable of ATL and MIA" — conditional two-team; originator indeterminate.
9. **OKC · 2029 · R2** — "DEN if DEN has not conveyed a first potential 1st round pick to OKC by 2029" — conditional on prior-year event.
10. **OKC · 2030 · R1** — "DEN 6-30 if not already settled and if DEN has conveyed a first potential 1st round pick to OKC by 2028" — conditional + range + external event.
11. **OKC · 2031 · R2** — "Less favorable of NOP and ORL (via ORL swap for NOP)" — conditional two-team swap; originator indeterminate.

## Denver (DEN)

1. **DEN · 2027 · R1** — "two most / more favorable of DEN 6-30, OKC and LAC to OKC then other to LAC (via OKC swap of OKC or DEN for LAC)" — three-pick multi-team conditional (6-30 portion; own 1-5 is `top-5 protected` in the CSV per ruling 2).
2. **DEN · 2028 · R1** — "6-30 to OKC if not already settled" — conditional + range.
3. **DEN · 2028 · R2** — "31-33 Own; 34-60 to WAS (via SAN to SAC)" — range-conditional split; owner depends on draft position.
4. **DEN · 2029 · R1** — "6-30 to OKC if not already settled; or 6-30 to OKC if DEN conveys a first potential 1st round pick to OKC in 2027" — conditional + range + prior-year event.
5. **DEN · 2029 · R2** — "To CHA if DEN has conveyed a first potential 1st round pick to OKC by 2029 or to OKC if DEN has not conveyed ..." — two-destination conditional.
6. **DEN · 2030 · R1** — "6-30 to OKC if not already settled and if DEN has conveyed a first potential 1st round pick to OKC by 2028" — conditional + range + external event.

## Minnesota (MIN)

1. **MIN · 2027 · R1** — "Most favorable of MIN, CLE and UTH to MEM; second most favorable to UTH and least favorable to PHX (via UTH)" — three-team most/least-favorable chain; owner indeterminate.
2. **MIN · 2029 · R1** — "most / two most favorable of MIN 6-30, CLE and UTH to UTH then other to CHA (via UTH to PHX)" — three-pick conditional + range (own 1-5 is `top-5 protected` per ruling 2).
3. **MIN · 2029 · R2** — "To UTH if MIN does not convey 1st round pick to UTH in 2029" — conditional on a prior-year event.
4. **MIN · 2030 · R1** — "less favorable of (i) MIN 2-30 and (ii) more favorable of SAN and DAL then most / more favorable of all to SAN (via SAN swap for DAL; via SAN swap of SAN or DAL for MIN)" — three-pick conditional + range (own #1 is `top-1 protected` per ruling 2).
5. **MIN · 2030 · R2** — "MEM 51-60" — range-conditional; MIN receives MEM's pick only if 51-60.
6. **MIN · 2031 · R2** — "More favorable of MIN and GOS to CHI then other to DET (via MIN swap for GOS)" — conditional two-team swap; owner (CHI or DET) indeterminate.

## Portland (POR) — through 2028 R1 only (rest batch 4b)

1. **POR · 2027 · R2** — "Less favorable of POR and NOP then other to CHA [POR may convey to HOU]; then less favorable of POR and NOP to HOU if 56-60 (via POR to NOP to CHA; via POR to BOS to HOU)" — conditional two-team swap + range sub-conditional; owner indeterminate.
2. **POR · 2028 · R1 swap clause** — "Own or swap for MIL [WAS then has complex swap rights with MIL]" → the swap clause is recorded on the **MIL 2028 R1** chain entry above (ruling 2: multi-team conditional, no `swap=true` row). POR's **own** row is in the CSV.

---

# Batch 4b — Northwest completion (POR 2028 R2+, UTH)

Raw provenance: `raw/batch-4b-por-uth.md`. Northwest division (OKC DEN MIN POR UTH) now complete.

## Portland (POR) — 2028 R2 onward

1. **POR · 2029 · R1** — "Most and least favorable of POR, BOS and MIL; second most favorable to WAS (via BOS to POR; via MIL to POR; via POR to WAS)" — three-team most/least-favorable chain; owner of POR's pick indeterminate.
2. **POR · 2029 · R2** — "Less favorable of IND and WAS (via IND to NYK)" — conditional two-team swap (incoming); originator indeterminate.
3. **POR · 2030 · R2** — "More favorable of POR and PHX to PHL then other to WAS (via WAS)" — conditional two-team swap; owner (PHL or WAS) indeterminate.

## Utah (UTH)

1. **UTH · 2027 · R1** — "Second most favorable of UTH, CLE and MIN; most favorable to MEM and least favorable to PHX (via UTH)" — three-team most/least-favorable chain; owner indeterminate.
2. **UTH · 2027 · R2** — "More favorable of BOS and ORL (via BOS to ORL to BOS)" — conditional two-team swap, circular routing; originator indeterminate.
3. **UTH · 2028 · R2** — "Least favorable of (i) DET 31-55, (ii) less favorable of CHA and LAC (via CHA to DAL to DET), (iii) MIA if DAL conveys 1st round pick to CHA in 2027 (via SAN to DAL to DET) and (iv) NYK (via DET)" — four-way least-favorable conditional chain with range + sub-conditional clauses; originator indeterminate.
4. **UTH · 2029 · R1** — "Most / two most favorable of UTH, CLE and MIN 6-30 then other to CHA (via UTH to PHX)" — three-team conditional + range; owner of UTH's pick indeterminate.
5. **UTH · 2029 · R2** — "MIN if MIN does not convey 1st round pick to UTH in 2029" — conditional on a prior-year event.
6. **UTH · 2030 · R2** — "Less favorable of UTH and LAC then other to CHA (via LAC to UTH)" — conditional two-team swap; owner (UTH or CHA) indeterminate.
7. **UTH · 2031 · R2** — "More favorable of BOS and CLE (via CLE to ATL to BOS)" — conditional two-team swap (incoming); originator indeterminate.

*(The UTH 2028 R1 swap clause is reclassified onto the **CLE 2028 R1** entry above, per ruling 1.)*

---

# Batch 5 — Southwest (SAN DAL HOU; MEM/NOP → 5b)

Raw provenance: `raw/batch-5-southwest.md` (MEM/NOP absent → 5b; HOU Section B truncated, gaps
recovered from the surviving inline notes).

> **🟢 RESOLVED (batch 5):** the `DAL,OKC,2028,1` pending-orphan from batch 4 — `DAL,DAL,2028,1`
> (own) now in the CSV, confirmed by DAL's page "Own or OKC (via OKC swap for DAL)". Valid pair.
>
> **🔴 NEW pending-orphan (ruling 5):** `SAC,SAN,2031,1` (SAN holds the right; "Own or swap for
> SAC") references SAC's outright 2031 R1, which doesn't exist yet (SAC = **Pacific batch**).
> BRK/DAL precedent — resolves when SAC's page adds its outright 2031 R1. Needs
> `--allow-pending-orphans` until then. (Net this batch: DAL orphan retires, SAC arms — one for one.)

## San Antonio (SAN)

1. **SAN · 2027 · R1** — "1-16 to SAC; 17-30 to OKC (via SAC)" — range-conditional split; owner depends on draft position.
2. **SAN · 2027 · R2** — "More favorable of (i) SAN and (ii) least favorable of OKC, HOU, IND and MIA then least favorable of all to MIA (via HOU to DET to OKC; via MIA to OKC to UTH to SAN to MIA)" — five-pick conditional chain; owner indeterminate.
3. **SAN · 2028 · R1 swap component** — "swap for BOS 2-30" — range-conditional swap trigger (only if BOS pick is 2-30). SAN's own row is in the CSV.
4. **SAN · 2028 · R2** — "BOS 31-45 if BOS 1 in 2028" — conditional on draft position + range.
5. **SAN · 2030 · R1** — "Most / more favorable of SAN, DAL and MIN 2-30; … to MIN (via SAN swap for DAL; via SAN swap of SAN or DAL for MIN)" — three-pick conditional chain + range; owner indeterminate.
   > **DAL 2030 R1 bilateral (batch 5, attached here — ruling 3):** DAL's page shows "Own or SAN (via SAN swap for DAL)", which looks bilateral, but `"via SAN swap of SAN or DAL for MIN"` appears in *both* pages' chains — the SAN/DAL swap is the mechanism *inside* this three-pick conditional. So `DAL,DAL,2030,1` (own) is emitted but **no `DAL,SAN,2030,1,swap=true` row**; the swap clause lives here.

## Dallas (DAL)

1. **DAL · 2027 · R1** — "3-30 to CHA" — range-conditional outbound (DAL keeps 1-2 = `top-2 protected` in the CSV; 3-30 conveys here).
2. **DAL · 2027 · R2** — "More favorable of DAL and BRK to WAS then other to DET (via DET to WAS; via DAL to BRK to DET)" — conditional two-team swap; owner (WAS or DET) indeterminate.
3. **DAL · 2029 · R1** — "Two most favorable of DAL, HOU and PHX to HOU then other to BRK (via DAL and PHX to BRK; via DAL or PHX to HOU; via HOU swap for DAL or PHX)" — three-pick conditional; owner indeterminate.
4. **DAL · 2030 · R1** — "less favorable of (i) MIN 2-30 and (ii) more favorable of DAL and SAN [or MIN if MIN not conveyable] to MIN then most / more favorable of all to SAN (via SAN swap for DAL; via SAN swap of SAN or DAL for MIN)" — multi-team conditional chain + range. *(Same SAN/DAL/MIN cluster as SAN 2030 R1.)*
5. **DAL · 2030 · R1** — "GOS 21-30 (via WAS)" — range-conditional on GOS draft position.
6. **DAL · 2030 · R2** — "GOS if GOS does not convey 1st round pick to DAL in 2030 (via WAS)" — conditional on an external event.

## Houston (HOU) — Section B truncated; recovered from inline notes

1. **HOU · 2027 · R2** — "Most favorable of HOU, OKC, IND and MIA to PHL; second most favorable to NOP and third most favorable to NYK … (via HOU to DET to OKC to NYK to NOP; via MIA to OKC to UTH to SAN to MIA; via OKC to PHL)" — five-team chain; owner indeterminate.
2. **HOU · 2027 · R2** — "Less favorable of POR and NOP if 56-60 (via POR to BOS)" — conditional two-team + range.
3. **HOU · 2029 · R1** — "Two most favorable of HOU, DAL and PHX to HOU then other to BRK (via DAL and PHX to BRK; via DAL or PHX to HOU; via HOU swap for DAL or PHX)" — three-pick conditional; owner indeterminate.
4. **HOU · 2031 · R2** — "or ATL (via ATL swap for HOU)" — range-conditional swap trigger (picks 31-55). HOU keeps 31-55 = `31-55 Own` in the CSV.
5. **HOU · 2031 · R2** — "56-60 to BOS" — range-conditional outbound.
6. **HOU · 2032 · R2** — "More favorable of HOU and PHX to CHI then other to PHX (via PHX to MIN)" — conditional two-team swap; owner indeterminate.

---

# Batch 5b — Southwest completion (MEM NOP) + cross-batch corrections

Raw provenance: `raw/batch-5b-mem-nop.md`. Completes the Southwest. **Project status: only Pacific
(GOS LAC LAL PHX SAC) remains** — the raw's "all 30 extracted" claim is false.
*(HOU's recovered Section B was already written above under Batch 5 — the batch-5b raw re-lists the
same 6 items verbatim; no new HOU entries.)*

> **Convention (ruling 2, batch 5b) — swap=true rows are for UNCONDITIONAL swap rights only.**
> A bilateral `swap=true` row may only represent a swap right with **no positional trigger**. Any
> swap gated on draft position ("swap for X **N-M**", "**1 Own**; 2-30 Own or X") is range-conditional
> → **gap it** (own row stays, no swap row), exactly like a multi-team chain. Retroactively applied:
> removed `PHL,LAC,2029,1` ("swap for LAC 4-30") and `ORL,MEM,2029,1` ("3-30 Own or MEM"); clauses
> gapped below. Forward: batch 5 already gapped SAN "swap for BOS 2-30" and ATL "swap for HOU 31-55".
> Also removed (3rd instance): `BOS,SAN,2028,1` ("1 Own; 2-30 Own or SAN") — same 2-30 range-trigger
> class, marked *Skipped* in the batch-2 archive but emitted in error. The rule is now **exception-free**:
> no range-triggered swap remains emitted in the CSV.

> **🔵 Holder-reversal CORRECTION (ruling 2):** `ORL,NOP,2030,2,swap=true` → **`NOP,ORL,2030,2,swap=true`**.
> Both pages agree ORL holds the right over NOP's pick — ORL page (batch 3): **"Own or swap for NOP"**
> (ORL is the actor → ORL holds); NOP page (batch 5b): **"Own or ORL (via ORL swap for NOP)"**. The old
> form encoded NOP as holder (backwards). Corrected row references `NOP,NOP,2030,2` (own, this batch) —
> valid pair; `ORL 2030 R2` left as own-only. **⚠ The structural validator cannot catch this class:** a
> holder reversal doesn't orphan when both teams have outright rows — only cross-page reading caught it.

> **✅ Tripwire CLOSED (ruling 4):** original-seed row `LAL,MEM,2027,1,top-4 protected,…,UTH` cross-confirmed
> from MEM's side — MEM page **"LAL 5-30"**, LAL page **"1-4 Own; 5-30 to MEM (via UTH)"**. Top-4 protection
> and via-UTH both verified; no re-emit (dedup).

> **🟥 Fabricated dedup (ruling 1):** the extraction claimed `MEM,LAC,2026,2` dedups against a non-existent
> "LAC batch" (Pacific unextracted; CSV had no MEM 2026 R2). Emitted as **new** (`MEM,LAC,2026,2`, via blank).
> Trust-nothing audit of the other 13 claimed dedups: all real.

## Range-conditional swap triggers removed from the CSV (gapped here, ruling 2)

1. **PHL · 2029 · R1** — "Own or swap for LAC 4-30" — PHL keeps if 1-3 (`top-3 protected`, own row in CSV); the LAC swap right (picks 4-30) is range-conditional → gapped, swap row removed.
2. **ORL · 2029 · R1** — "1-2 Own; 3-30 Own or MEM (via MEM swap for ORL)" — ORL keeps if 1-2 (own row in CSV); the MEM swap right (picks 3-30) is range-conditional → gapped, swap row removed. *(Cross-confirmed from MEM's page: "Own or swap for ORL 3-30".)*
3. **BOS · 2028 · R1** — "1 Own; 2-30 Own or SAN (via SAN swap for BOS)" — BOS keeps #1 (own row in CSV); the SAN swap right (picks 2-30) is range-conditional → gapped, swap row `BOS,SAN,2028,1` removed. The rule is now **exception-free** — no range-triggered swap remains emitted. *(Batch-2 archive had already marked this swap component "Skipped"; the row was emitted in error.)*

## Memphis (MEM)

1. **MEM · 2027 · R1** — "Most favorable of UTH, CLE and MIN (via UTH)" — three-team most-favorable; originator indeterminate.
2. **MEM · 2027 · R2** — "LAL if LAL does not convey 1st round pick to MEM in 2027 (via UTH)" — conditional on a prior-year event.
3. **MEM · 2029 · R1 swap component** — "swap for ORL 3-30" — range-conditional swap trigger (own row in CSV).
4. **MEM · 2029 · R2** — "ORL if ORL 1-2 in 2029" — range conditional.
5. **MEM · 2030 · R1** — "More favorable of (i) MEM and (ii) less favorable of PHX and WAS then least favorable of all to PHX (via WAS swap for PHX; via MEM swap for PHX or WAS)" — three-pick conditional chain; owner indeterminate.
6. **MEM · 2030 · R2** — "51-60 to MIN" — range-conditional outbound (MEM keeps 31-50 = `31-50 Own` in CSV).
7. **MEM · 2031 · R2** — "More favorable of (i) MEM and (ii) less favorable of IND and MIA then least favorable of all to IND (via MIA swap for IND; via MEM swap for IND or MIA)" — three-pick conditional chain; owner indeterminate.
8. **MEM · 2032 · R2** — "GOS 51-60" — range-conditional incoming.

## New Orleans (NOP)

1. **NOP · 2027 · R1** — "More favorable of NOP and MIL then other to ATL if 5-30 or MIL and NOP to NOP if both 1-4 (via NOP)" — conditional two-team swap with a positional exception clause; owner indeterminate.
2. **NOP · 2027 · R2** — "More favorable of NOP and POR to CHA then other to POR [POR may convey to HOU] (via NOP)" — conditional two-team swap; owner indeterminate.
3. **NOP · 2027 · R2** — "Second most favorable of OKC, HOU, IND and MIA (via HOU to DET to OKC to NYK)" — four-team conditional chain; originator indeterminate.
4. **NOP · 2031 · R2** — "More favorable of NOP and ORL to ORL then other to OKC (via ORL swap for NOP)" — conditional two-team swap; owner indeterminate.

---

# Batch 6 — Pacific (GOS→GSW, LAC, LAL, PHX, SAC)

Raw provenance: `raw/batch-6-pacific.md`.

> **🟢 ORPHAN RETIRED (ruling 1):** `SAC,SAC,2031,1` (own) landed — "Own or SAN (via SAN swap for SAC)" —
> pairing with the existing `SAC,SAN,2031,1` swap. **The validator is now at ZERO issues and seeding
> works WITHOUT `--allow-pending-orphans`.** No range-triggered or orphan swaps remain.
>
> **🟥 Fabricated dedup (same MEM/LAC pattern):** the extraction claimed `PHX,CHA,2031,2` dedups
> against a non-existent "CHA batch" (CHA unextracted). Emitted as **new** (`PHX,CHA,2031,2`,
> PHX page "To CHA") → forward-ref for batch 7.
>
> **🚨 PROJECT INCOMPLETE — Charlotte (CHA) never extracted.** 30-team own-row census = 29 present,
> CHA absent (batch-3 "CHA → 3b" never ran). **Batch 7 (CHA) required for a true 30/30.**

## Golden State (GSW)

1. **GSW · 2027 · R2** — "More favorable of GOS and PHX to PHL then other to WAS (via WAS)" — multi-team conditional swap; owner indeterminate.
2. **GSW · 2030 · R1** — "21-30 to DAL (via WAS)" — range-conditional outbound (GSW keeps 1-20 = `top-20 protected` in CSV).
3. **GSW · 2030 · R2** — "To DAL if GOS does not convey 1st round pick to DAL in 2030 (via WAS)" — conditional on an external event.
4. **GSW · 2031 · R2** — "More favorable of GOS and MIN to CHI then other to DET (via MIN swap for GOS)" — multi-team conditional swap; owner indeterminate.
5. **GSW · 2032 · R2** — "51-60 to MEM" — range-conditional outbound (GSW keeps 31-50 = `31-50 Own` in CSV).

## LA Clippers (LAC)

1. **LAC · 2027 · R1** — "Least / less favorable of LAC, DEN 6-30 and OKC then other(s) to OKC (via OKC swap of OKC or DEN for LAC)" — multi-team conditional chain + range; owner indeterminate.
2. **LAC · 2028 · R2** — "More favorable of LAC and CHA to CHA then other to DET [DET may convey to UTH] (via CHA to DAL)" — multi-team conditional swap; owner indeterminate.
3. **LAC · 2029 · R1** — "4-30 Own or PHL (via PHL swap for LAC)" — range-conditional swap trigger (LAC keeps 1-3 = `top-3 protected` in CSV; 4-30 swap with PHL gapped). **✅ Cross-confirms the batch-5b removal of `PHL,LAC,2029,1`:** LAC's own page states the same 4-30 trigger, so removing the unconditional swap row was correct.
4. **LAC · 2030 · R2** — "More favorable of LAC and UTH to CHA then other to LAC (via LAC to UTH)" — multi-team conditional swap; owner indeterminate.

## LA Lakers (LAL)

1. **LAL · 2027 · R2** — "To BRK if LAL conveys 1st round pick to MEM in 2027 or to MEM if LAL does not convey … (via UTH to MEM)" — conditional either/or destination.
2. **LAL · 2028 · R2** — "More favorable of LAL and WAS to ORL then other to WAS (via WAS to LAL to ORL; via LAL to WAS)" — multi-team conditional swap; owner indeterminate.

## Phoenix (PHX)

1. **PHX · 2027 · R1** — "Least favorable of UTH, CLE and MIN (via UTH)" — three-team conditional; originator indeterminate.
2. **PHX · 2027 · R2** — "More favorable of PHX and GOS to PHL then other to WAS (via WAS)" — multi-team conditional swap; owner indeterminate.
3. **PHX · 2028 · R1** — "Least favorable of PHX, PHL 9-30, WAS and BRK; … (via BRK swap of BRK or PHL for PHX; via BRK swap of BRK or PHX for NYK; via WAS swap for PHX, BRK or PHL)" — multi-team conditional chain; owner indeterminate.
4. **PHX · 2028 · R2** — "More favorable of PHX and IND to IND then other to NYK (via IND)" — multi-team conditional swap; owner indeterminate.
5. **PHX · 2029 · R1** — "Two most favorable of PHX, DAL and HOU to HOU then other to BRK (via DAL and PHX to BRK; via DAL or PHX to HOU; via HOU swap for DAL or PHX)" — three-pick conditional chain; owner indeterminate.
6. **PHX · 2030 · R1** — "Least favorable of PHX, WAS and MEM; … (via WAS swap for PHX; via MEM swap for PHX or WAS)" — multi-team conditional chain; owner indeterminate.
7. **PHX · 2030 · R2** — "More favorable of PHX and POR to PHL then other to WAS (via WAS)" — multi-team conditional swap; owner indeterminate.
8. **PHX · 2032 · R2** — "Less favorable of PHX and HOU then other to CHI (via PHX to MIN)" — conditional two-team swap; owner indeterminate.

## Sacramento (SAC)

1. **SAC · 2027 · R1** — "SAN 1-16" — range-conditional incoming (SAC receives SAN's pick only if 1-16).
2. **SAC · 2027 · R2** — "To OKC if SAN 1-16 in 2027; CHA if SAN 17-30 in 2027 (via NYK to ATL to SAN)" — conditional on SAN's draft position.

---

# Batch 7 — Charlotte (CHA), the 30th and final team

Raw provenance: `raw/batch-7-charlotte.md`. Closes the coverage gap caught by the census after batch 6.

> **✅ PROJECT COMPLETE — 30/30 teams.** `bun run check:draft-picks` goes green. All 6 incoming CHA
> picks were pre-staged (batches 1/3/6) and confirmed dedups; CHA emitted its own picks only.
> `ORL,CHA,2026,1` keeps its batch-3 form (via=MEM) — CHA's line shows swap mechanics, not new routing.

1. **CHA · 2027 · R1** — "DAL 3-30" — range-conditional incoming (DAL page: "1-2 Own; 3-30 to CHA").
2. **CHA · 2027 · R1** — "MIA 15-30" — range-conditional incoming (MIA page: "1-14 Own; 15-30 to CHA").
3. **CHA · 2027 · R2** — "To OKC if SAN 1-16 in 2027 or to SAC if SAN 17-30 in 2027 (via NYK to ATL to SAN)" — conditional either/or on SAN's draft position.
4. **CHA · 2027 · R2** — "Less favorable of BOS and ORL (via BOS to ORL)" — multi-team conditional; originator indeterminate.
5. **CHA · 2027 · R2** — "More favorable of POR and NOP (via POR)" — multi-team conditional; originator indeterminate.
6. **CHA · 2028 · R1** — "MIA if not already settled" — conditional on an external event.
7. **CHA · 2028 · R2** — "More favorable of CHA and LAC then other to DET [DET may convey to UTH] (via CHA to DAL)" — multi-team conditional chain; owner indeterminate.
8. **CHA · 2028 · R2** — "MIA if DAL does not convey 1st round pick to CHA in 2027 (via SAN to DAL)" — conditional on an external event.
9. **CHA · 2029 · R1** — "Least / less favorable of UTH, CLE and MIN 6-30 (via UTH to PHX)" — multi-team conditional + range; originator indeterminate.
10. **CHA · 2029 · R2** — "DEN if DEN has conveyed a first potential 1st round pick to OKC by 2029" — conditional on an external event.
11. **CHA · 2029 · R2** — "More favorable of ATL and MIA (via OKC)" — multi-team conditional; originator indeterminate.
12. **CHA · 2030 · R2** — "56-60 to BOS" — range-conditional outbound (CHA keeps 31-55 = `31-55 Own` in CSV).
13. **CHA · 2030 · R2** — "More favorable of UTH and LAC (via UTH)" — multi-team conditional; originator indeterminate.
