# MarketSignal — Project Status
Last updated: 2026-03-30 (Claude Code hooks: large-file guard + auto-deploy; VPS git conflict resolved)

---

## What This Is
A geopolitical signal detection system that monitors public data sources to detect
escalation/ceasefire signals in the Middle East before they get priced into
Polymarket prediction markets.

Core thesis: convergence of independent signal layers (aviation + maritime + diplomatic)
precedes the news cycle by hours to weeks. GDELT provides the historical ground truth
for back-testing.

**Live on:** DigitalOcean VPS — 1 vCPU / 1GB RAM, Ubuntu 24.04, IP: 159.203.39.59
**Dashboard:** http://159.203.39.59:8501
**Note:** New droplet (2026-03-17) — previous droplet at 146.190.142.71 was shut down after DDoS.
ADS-B and AIS baselines rebuilding; full anomaly detection resumes after ~7 days of new data.
**Services:** managed by systemd (auto-restart on reboot)

---

## What's Been Built

### Stage 1: GDELT — DONE ✓
Historical geopolitical event database. 2.5 million events, Jan 2023–present.
- `gdelt_collector.py` — pulls from Google BigQuery, stores to local SQLite
- `gdelt_verify.py` — verified against 5 known historical events (all pass)
- `gdelt_query.py` — quick date-range query tool
- `gdelt_events.db` — the database (local only, not in git — 768MB)
- Goldstein scale cleanly separates escalation (negative) from ceasefire (positive)

### Stage 2a: ADS-B Airspace Monitor — DONE ✓
Live aircraft tracking across 9 Middle East bounding boxes every 10 minutes.
- `adsb_collector.py` — polls OpenSky Network via OAuth2
- Detects: airline presence/absence (13 watched carriers), aircraft counts by region
- Anomaly detection: flags >40% drop vs 7-day same-hour/same-DOW baseline
- Run with: `python3 adsb_collector.py --loop`

### Stage 2b: Dual-Mode Strategic Tracking — DONE ✓
Extended `adsb_collector.py` with two new detection modes.

**Mode A — VIP Watchlist**
- 28 government/diplomatic aircraft trackable by ICAO24 (49 total in watchlist)
- Logs every sighting with timestamp, region, lat/lon, altitude
- Going-dark detection: flags aircraft that vanish for >24h
- Source: `VIP Aircraft.csv` — E-4B Doomsday planes, C-32A/B, Air Force One,
  E-11A BACN relay, Putin's Il-96, Gulf royal flights, Iranian presidential aircraft,
  and more
- Dashboard shows full watchlist with ADS-B trackable indicator per aircraft

**Mode B — Strategic Type Clustering**
- Aircraft type database loaded from SQLite at startup
- 4 Type-Watch regions (Persian Gulf, Eastern Med, Horn of Africa, Caucasus)
- Watches: strategic lift (C17/IL76/C5M/Y20), tankers (KC135/KC46/A332),
  ISR/command (RC35/E3CF/E3TF/RQ4/E8), diplomatic bizjets (12 types)
- Flags surges >2σ above 30-day baseline (activates after ~25h of polling)
- Bizjet clustering: flags 3+ bizjets from different countries at same airport
- Run with: `python3 adsb_collector.py --loop` (same command as 2a)

### Stage 2c: NOTAM Monitor — DONE ✓ (LIVE as of 2026-03-13)
Airspace restriction filings — leading indicator, hours before kinetic events.
- `notam_collector.py` — polls Cirium Sky API every 30 min, stores to `notam_events.db`
- Auth: Static Token — `CIRIUM_SKY_APP_KEY` (Secret) in `Authorization` header
- Endpoint: `POST https://api.sky.cirium.com/v1/notams` with GeoJSON Feature body
- Stores ALL ME NOTAMs; auto-flags restriction Q-codes as anomalies
- `first_detected_at` preserved on INSERT — critical for back-testing lead times
- Run with: `python3 notam_collector.py --loop`
- Systemd service: `notam-collector` (enabled and running on VPS)

### Stage 2d: Maritime / AIS Layer — DONE ✓ (pending first run)
AIS vessel tracking via aisstream.io WebSocket stream.
- `ais_collector.py` — streams 10 min every 30 min, stores to `ais_events.db`
- 5 regions: Persian Gulf, Strait of Hormuz, Red Sea, Gulf of Aden, Arabian Sea
- Tanker/cargo density vs 7-day same-hour baseline — drop >30% → MEDIUM, >50% → HIGH
- Military vessel surge >2× baseline → HIGH escalation signal
- GPS spoofing detection: SOG >50kn or position jump >50nm flagged
- MMSI watchlist: `VIP Vessels.csv` (8 vessels: Iranian frigates, USNS logistics ships)
- `pages/6_Maritime_Monitor.py` — counts trend, watchlist sightings map, spoofing log
- Auth: `AISSTREAM_API_KEY` in `.env` (free tier at aisstream.io)
- Run: `python3 ais_collector.py --loop`
- Systemd: `ais-collector.service`
- **Baseline needs ~7 days** before anomaly detection activates
- **Future:** satellite AIS for Arabian Sea gaps; event volume weighting on GDELT signal

### Stage 2e: Route Suspension Collector — DONE ✓ (pending first run)
Monitors actual operated flights for 13 watched airlines across all ME airport pairs.
A sustained route suspension is a leading indicator — airlines pull service quietly
before governments announce closures.
- `route_collector.py` — polls Cirium Flex API daily
- **Baseline approach (fixed 2026-03-24):** Cirium Flex `/schedules/` endpoint returns
  404 on this plan. Replaced with a 14-day rolling historical baseline — averages
  operated flights per carrier per route using `/flightstatus/historical/` data.
  Same 60% drop + 3 consecutive day logic; only the schedule source changed.
- Stores in `route_events.db` with `first_detected_at` + `last_confirmed_at`
- Feeds into convergence engine as `route_suspension` signals (λ=0.12/day, Event type)
- Auth: Cirium Flex API — `appId` + `appKey` query params (`CIRIUM_FLEX_APP_ID`, `CIRIUM_FLEX_APP_KEY` in `.env`)
- Base URL: `https://api.flightstats.com/flex`
- Run: `python3 route_collector.py --refresh` (build baseline), then `--loop`
- `pages/7_Route_Monitor.py` — route suspension table, active flags, per-route trend chart
- Systemd: `route-collector.service`
- Airport pairs monitored (both directions): TLV, AMM, BGW, KWI, BAH, DOH, DXB,
  AUH, MCT, IKA, THR, BEY, CAI, IST

### Stage 3: Convergence Engine — DONE ✓ (major overhaul 2026-03-15)
Single scoring daemon that reads all signal tables and outputs escalation +
de-escalation scores every 10 minutes.
- `convergence_engine.py` — main daemon
- **State vs Event logic:**
  - Events (diplomatic VIP sighting, type surge): exponential decay from `last_confirmed_at`
  - States (ISR/command aircraft continuously airborne, active NOTAM, ADS-B blackout):
    sigmoid growth while active, exponential decay after cleared
  - ISR (`isr`) and command (`command`) category VIP aircraft use State logic —
    an E-11A BACN active for 12h is infrastructure, not a one-off event
- **S_0 weights:** isr_high 20.0 | command_high 22.0 | going_dark 15.0 |
  notam_high 5.0 | strategic_lift_high 16.0 | vip_sighting 5.0 |
  ais_spoofing_low 8.0 | ais_spoofing_medium 14.0 | ais_spoofing_high 20.0
- **Lambda values (per day):** strategic_lift 0.03 | tanker 0.04 | ISR/command 0.06 |
  bizjet 0.10 | route_suspension 0.12 | NOTAM 0.35 | going_dark 0.60 | ais_spoofing 0.20
- **State decay uses signal-type lambda** (fixed 2026-03-15): resolved states now decay
  at their own characteristic rate. Previously ALL resolved states used λ=0.35 (NOTAM rate).
  Now: ISR/command λ=0.06 (intelligence requirement persists), going_dark λ=0.60
  (reappearance closes window fast), route_suspension λ=0.12, NOTAM λ=0.35 unchanged.
- **Coherence multiplier** (1.5×): fires when 2+ signal categories both score > 2.0
  in the same macro-zone. Zones: GULF | LEVANT | IRAN | YEMEN_RED_SEA | EGYPT |
  SAUDI | IRAQ | TURKEY | ME (wildcard).
  Fixed 2026-03-15: previously grouped by raw region string — ADS-B "Persian Gulf / Qatar"
  and AIS "persian_gulf" would never match. Now all layers normalise through `_coherence_zone()`
  to shared macro-zones first. GDELT and going-dark signals are "ME wildcards" — they
  participate in any zone that already has a qualifying physical signal, but cannot
  trigger coherence alone (prevents GDELT earning a bonus with no physical corroboration).
- **GPS spoofing signals wired in** (added 2026-03-15): `read_spoofing_events()` reads
  `spoofing_events` table from `ais_events.db`. Groups events by maritime region (inline
  lat/lon lookup — Hormuz matched before broader Persian Gulf box). Tiers S0 by cluster
  size: 1 event=LOW (8pts), 2-4=MEDIUM (14pts), 5+=HIGH (20pts). λ=0.20/day (half-life
  ~3.5 days). Previously the `spoofing_events` table had no signal path to the engine.
- **GDELT baseline window fixed** (2026-03-15): baseline now uses days 91–270 (179-day
  window, ~3–9 months ago) instead of days 31–90. The old adjacent baseline was
  contaminated by recent escalation — a 60-day conflict would silently shrink the delta
  toward zero. New baseline: even a 3-month sustained escalation cannot contaminate it.
  Confirmed improvement: current delta -1.80 (was being partially masked by old baseline).
- **Bilateral velocity** (v2 2026-03-19): `compute_velocity()` now accepts a `track`
  parameter — each track gets its own urgency bonus independently. De-escalation velocity
  cap = 10.5 pts (15% of DEESC_MAX=70), escalation cap = 30 pts (15% of ESC_MAX=200),
  so the bonus is proportional to each track's magnitude.
- **Ceiling normalisation** (v2 2026-03-19, replaces split-beta approach):
  Each track's raw score is divided by its theoretical maximum before a shared sigmoid.
  - ESC_MAX_PTS = 200 (all 6 escalation layers firing simultaneously)
  - DEESC_MAX_PTS = 70 (full ceasefire-negotiation scenario)
  - SIGMOID_STEEPNESS = 10 (shared, in normalised [0,1] space)
  - A 30% probability on either track now means the same thing: 30% of that track's
    theoretical maximum signal strength. Previously, split betas (β=100 esc, β=40 deesc)
    did the structural compensation *inside* the sigmoid — nonlinear, distorting at low
    signal levels and saturating earlier at high ones. Tension and edge calculations
    were unreliable because the same percentage represented different evidence levels.
- **Tension metric** (v2 2026-03-19): `compute_tension()` = √(esc_prob × deesc_prob).
  Only high when both tracks are simultaneously elevated. >15% flags that the model
  is seeing signals in both directions — most useful indicator of Polymarket mispricing.
  Stored as new `tension` column in `scores` table.
- **Independent track probabilities** (v2 2026-03-19): tracks no longer sum to 1.
  Old competing formula (esc + deesc = 100%) conflated "no signals" with "conflicting
  signals" — both produced ~50%. Now: no signals → (esc≈1%, deesc≈1%), active war →
  (esc≈90%, deesc≈2%), ceasefire-in-progress → (esc≈90%, deesc≈85%, tension≈87%).
- **New DB columns** (auto-migrated): `tension`, `deesc_velocity_24h`, `deesc_velocity_bonus`
- **Divergence detection:** flags when GDELT and physical signals contradict each other
- **NOTAM signal fix (2026-03-15):** ME FIR whitelist (19 FIRs). One signal per FIR,
  worst severity. Reduced notam_high 14→5, notam_medium 7→3.
- **⚠ S_0 weights are placeholders** — must be calibrated via GDELT back-test
- **⚠ Ceiling values (ESC_MAX=200, DEESC_MAX=70) are estimates** — refine after 6+ months
  of live multi-layer data. Key question: does the max ever actually get approached, or
  do we need to lower the ceilings to get meaningful probability spread in practice?
- Run with: `python3 convergence_engine.py --loop`
- Check signals: `python3 convergence_engine.py --signals`
- Outputs to: `convergence_engine.db`
- Systemd: `convergence-engine.service` (deploy/convergence-engine.service)

### Stage 4: Polymarket Integration — DONE ✓ (v2 — Claude classification 2026-03-19)
Live ME market prices surfaced alongside model probabilities so edge is visible in one view.
- `polymarket_collector.py` (v2) — polls Gamma API, cheap geography pre-filter, then Claude
  API classifies whether each market is predictable by the signal system and which track it
  belongs to. Falls back to keyword classification when `ANTHROPIC_API_KEY` not set or API down.
- Classification cached per-market (`classified_by` column) — subsequent polls only update prices
  unless `--reclassify` is passed to force a full re-run
- New DB columns: `classified_by` (claude/keyword), `classify_reason` (10-word summary)
- Claude understands signal-system predictability: military strikes/airstrikes/ceasefire deals YES;
  elections/UN votes/economic sanctions/casualty counts NO
- `polymarket_markets.db` — markets + price_history tables (SQLite, local only)
- Convergence Engine page: sidebar filters (track, min |edge|%, max rows), clickable links
- Overview dashboard: top 3 opportunity cards with edge color-coding
- Run with: `python3 polymarket_collector.py --loop` (polls every 15 min)
- Reclassify: `python3 polymarket_collector.py --reclassify`
- Requires: `ANTHROPIC_API_KEY` in `.env` (keyword fallback if missing)
- Systemd: `polymarket-collector.service`
- **Phase 2 (future):** Kelly criterion position sizing, edge threshold alerts, price history charts

### Schema: last_confirmed_at + resolved_at — DONE ✓
All signal tables have `last_confirmed_at` (updated each poll while active) and
`resolved_at` (set when condition clears). Affected tables: `anomalies`,
`type_anomalies`, `bizjet_clusters`, `vip_dark_events`, `notam_anomalies`.

### Dashboard — DONE ✓
Run with: `streamlit run dashboard.py`
- **Home (`dashboard.py`)**: Signal Overview — unified anomaly feed from all layers,
  top metrics (ME aircraft, active NOTAMs, anomaly counts, VIP sightings, GDELT Goldstein),
  ADS-B counts bar chart, NOTAM restrictions by country, GDELT 30-day trend sparkline.
  All data from local SQLite — no BigQuery on page load.
- **Page 1:** GDELT Explorer — BigQuery charts, event table, Goldstein scale trend (deep-dive)
- **Page 2:** ADS-B Monitor — live counts, trend chart, airline presence, anomaly log
- **Page 3:** NOTAM Monitor — active restrictions map, NOTAM feed with Q-code filter, anomaly log
- **Page 4:** Strategic Monitor — VIP sightings, full watchlist expander (49 aircraft),
  going-dark events, type count timeseries, type anomaly log, bizjet cluster log
- **Page 5:** Convergence Engine — probability display, score history chart, Polymarket ME
  market table (Edge, Bet, links), live signal breakdown, coherence multiplier status,
  divergence flag, scoring explainer
- **Page 6:** Maritime Monitor — AIS vessel counts, watchlist sightings map, spoofing log
- **Page 7:** Route Monitor — airline route suspension table, active flags, per-route trend chart
- **Home:** 7th metric tile — "Top Poly Edge" (largest |model − market| opportunity)

---

## Recent Changes (2026-03-30 — Claude Code workflow hooks)

### Large-file guard hook — LIVE ✓
PreToolUse hook in `~/.claude/settings.json` (global). Fires on any `git add` command,
scans for files >50MB outside `.git/`, and blocks staging with a file list if found.
Automates the manual check in CLAUDE.md. Currently would flag:
`aircraft-database-complete.csv` and `gdelt_events.db`.

### Auto-deploy hook — LIVE ✓
PostToolUse hook in `.claude/settings.json` (project). Fires after `git push`.
Script at `.claude/hooks/deploy.sh`:
- Always restarts `marketsignal-dashboard`
- Also restarts the collector service for any source file changed in the last commit
  (adsb, notam, ais, convergence, polymarket, route)
- SSHs to VPS, runs `git pull --ff-only`, restarts services, reports status back

### VPS git conflict resolved ✓
VPS had local edits to `polymarket_collector.py` (the dotenv + batch fix from session 2
that was applied directly on the server before being committed locally). Discarded with
`git checkout` since the same changes were already in origin/main. VPS pulled cleanly,
2 commits fast-forwarded.

---

## Recent Changes (2026-03-28 — bug fixes)

### 1. ADS-B: hours_dark frozen at detection snapshot — FIXED ✓
`check_going_dark()` only ran for `dark_flagged = 0` aircraft — once flagged, `hours_dark`
and `last_confirmed_at` were never updated. VQ-BJO showed "24h dark" despite being dark 11 days.
Fix: added a second update loop at the end of `check_going_dark()` that refreshes both fields
for all currently-flagged dark aircraft on every poll.

### 2. Convergence engine: VIP diplomatic/strategic_lift using wrong decay lambda — FIXED ✓
`event_score()` was called with `"bizjet"` (λ=0.10/day, ~7d half-life) for ALL non-state VIP
categories including diplomatic and strategic_lift. Fixed:
- `diplomatic` → `"gdelt_deesc"` (λ=0.08/day, ~9d half-life) — peace talks persist longer
- `strategic_lift` → `"strategic_lift"` (λ=0.03/day, ~23d half-life) — airlifts are sustained ops
- unknown category → unchanged `"bizjet"`

### 3. Route collector: empty schedule table silently skips refresh — FIXED ✓
`MIN(cached_at)` returns `None` on empty table. Old guard `if oldest and oldest > cutoff`
treated `None` as truthy-failing and skipped the refresh. Fixed to `if oldest is not None`.

## Recent Changes (2026-03-28 — convergence engine fixes)

### 4. VIP sighting category mislabel — FIXED ✓
Diplomatic and strategic_lift VIP events were emitted with `category: "bizjet"`, putting
them into the bizjet diminishing-returns group. A diplomatic flight in Iran and one in Saudi
Arabia would decay each other's scores as if they were the same type of event. Fixed to
`category: "vip_sighting"` so they form their own independent group.

### 5. Coherence floor raised 2.0 → 4.0 — FIXED ✓
`COHERENCE_FLOOR = 2.0` allowed nearly-expired signals (~7 days old) to still trigger the
1.5× coherence multiplier. Raised to 4.0 so only signals with real current weight contribute.

### 6. AIS spoofing tier uses 7-day window — FIXED ✓
Cluster tier (LOW/MEDIUM/HIGH) was based on distinct vessel count across the full 30-day
signal window, so month-old events could inflate HIGH tier. Now tier is calculated from
the last 7 days only. Score decay still uses `most_recent` from the full window so old
signals fade naturally.

### Polymarket collector memory leak — restarted
`polymarket_collector.py` had been running since 2026-03-19 (9 days) and consumed 51.6%
RAM (508MB). Restarted — back to 7.7%. Monitor for recurrence; if it climbs again
investigate the polling loop for accumulating data structures.

---

## Known Issues / Blockers

### GDELT signal redesigned — Goldstein average approach (2026-03-14) ✓
Old count-based approach was permanently saturated. Replaced with:
- `avg_goldstein(30d)` vs `avg_goldstein(baseline window)`
- Negative delta → escalation signal; positive → de-escalation
- Back-tested against 7 historical events: 4/4 escalation, 2/3 de-escalation correct
- Fixes silent column-name bug (old code used BigQuery names, never actually ran)

**Baseline window extended (2026-03-15):** old baseline (days 31-90) sat immediately
adjacent to the signal window and could be contaminated by sustained escalation.
New baseline: days 91-270 (~3-9 months ago, 179-day window). With live data:
delta improved from partial to full: avg_30d=-2.293 vs baseline=-0.493 → Δ=-1.800.
Detail string in signal now shows `baseline(91-270d)=X.XXX` so it's always clear
what period was compared.

**Future improvement:** add event volume weighting — a Goldstein drop with 5× normal
event count is more significant than the same drop at baseline volume. Would multiply
signal score by `log(count_ratio) + 1`. Not done yet because Goldstein-only already
validates correctly and volume weighting needs backtest re-validation.

### notam_check.py — hardcoded API key ⚠
`notam_check.py` (ICAO Data Services test utility) has a hardcoded API key in the source.
It's not in `.gitignore` and is committed to the repo. If the key is sensitive, revoke
it and move it to `.env`. This file is a one-off testing script; consider deleting it
once the NOTAM pipeline is confirmed stable.

### ADS-B coverage gaps — conflict zones show zero
Lebanon/Syria and Yemen/Red Sea consistently return 0 — transponders off in active
conflict zones. Expected, but limits coverage in key areas.

### VIP watchlist — 7 of 49 aircraft still missing ICAO24 (2026-03-15)
42/49 now trackable after manual lookup transfer from VIP Aircraft-updates.csv.
Still missing: Bulgarian LZ-001, USAF C-37Bs x2, C-17, RC-12, Chinese IL-76, Austrian 4D-MNE.
Use ADS-B Exchange or Flightradar24 historical playback to find codes.

---

### Convergence engine — P=100% bug fixed (2026-03-15) ✓
Root cause: Cirium bounding box returns NOTAMs from non-ME FIRs (Romania, Russia,
Greece, India). 195 spurious NOTAM signals were each scoring individually.
Fixes applied:
- ME FIR whitelist (19 FIRs) — non-ME FIRs now excluded
- Deduplicate by FIR location, not notam_id — one signal per FIR, worst severity
- notam_high S₀: 14→5, notam_medium S₀: 7→3
- SIGMOID_BETA: 30→100 (was saturating at tiny scores)
- Dashboard explainer updated to reflect β=100 and GDELT backtest findings
- gdelt_backtest.py SIGMOID_BETA synced to 100

---

## Recent Changes (2026-03-15 — convergence engine overhaul)

Five math fixes and five pending UI/UX improvements were identified in a full code review.
Five math fixes applied this session:

### 1. GPS spoofing → convergence engine ✓
`spoofing_events` table had no path to the scoring engine. Added `read_spoofing_events()`
which groups events by maritime region (inline lat/lon → zone lookup, Hormuz matched before
broader Persian Gulf box) and tiers S0 by cluster density. Previously this high-confidence
signal was silently discarded every cycle.

### 2. Coherence zone normalization ✓
Coherence was grouping by raw `s["region"]` string, which meant ADS-B "Persian Gulf / Qatar"
and AIS "persian_gulf" would never cohere even though they're the same area. Added `_ZONE_MAP`
dict + `_ICAO_PREFIX_ZONES` dict + `_coherence_zone()` function. All 8 signal layers now
normalise to shared macro-zones before the coherence check. GDELT/going-dark act as ME
wildcards (can join any zone's coherence but can't trigger it alone). Verified 15 region
string → zone mappings in tests.

### 3. State decay lambda per signal type ✓
`state_score()` was using `LAMBDAS["notam"]` (λ=0.35) for ALL resolved states regardless
of type. ISR aircraft should persist for weeks after landing (λ=0.06), not fade in 2 days.
Added `signal_type` parameter to `state_score()`. Each caller now passes its own lambda key:
- Traffic anomalies: `"route_suspension"` (λ=0.12)
- ISR/command VIP: `"isr_command"` (λ=0.06)
- Going dark: `"going_dark"` (λ=0.60)
- NOTAM: `"notam"` (λ=0.35) — no behavior change, now explicit

### 4. GDELT baseline window ✓
Baseline moved from days 31-90 → days 91-270. See GDELT section above for detail.

### 5. Score velocity/acceleration ✓
Added `compute_velocity()` — compares current score to 24h-ago score, adds rising-score
bonus before sigmoid normalisation. New constants: VELOCITY_WEIGHT=0.30, VELOCITY_MAX_BONUS=30.
New DB columns: `velocity_24h`, `velocity_bonus`. Convergence Engine dashboard page now
shows a 5th metric tile with direction arrow and 24h change.

### UI improvements applied (2026-03-15)
The same review also identified UI issues. Applied alongside Stage 4:
6. **Polymarket price on convergence page ✓** — full ME market table with Edge/Bet/Volume/
   Expires columns; edge color-coded by magnitude; clickable links to polymarket.com.
   Top-edge metric tile on main overview page.
9. **Progress bar overflow ✓** — replaced CSS progress bars with Plotly horizontal bar
   gauge (`chart_probability_gauge()`). No more overflow at low probabilities.

### Dashboard redesign (2026-03-17) ✓
Complete overhaul of `dashboard.py` main page:
- **Top Polymarket Opportunities** — 3 clickable cards with Bet direction and color-coded
  edge now appear first (was buried; most actionable info front and center)
- **6-column metrics row** — Escalation P, Raw Score, ME Aircraft, Active NOTAMs,
  GDELT Goldstein, VIP Sightings
- Fixed GDELT SQLite column name bug (was using BigQuery names — GDELT section always showed offline)
- Fixed ADS-B/Maritime/Convergence status strip showing "offline" due to naive datetime bug
- Fixed `add_hline(secondary_y=True)` Plotly crash on GDELT Explorer page
- Maritime Monitor: added shared styles + dark theme
- Removed Inter font override (user preference)

### Route collector schedules fix (2026-03-24) ✓
The Cirium Flex `/schedules/` endpoint returns 404 on this plan tier. `fetch_schedule()`
was replaced with `build_baseline_from_history()` — uses the last 14 days of actual
`/flightstatus/historical/` data as the operated-flight baseline instead. Same 60%-drop +
3-consecutive-day suspension logic; only the baseline source changed. Route collector is
now unblocked and ready for its first live run (`--refresh` then `--loop`).

Also in this commit:
- `pages/7_Route_Monitor.py` added (route suspension dashboard — already existed as
  `6_Route_Monitor.py` but had a Streamlit page-number collision with Maritime Monitor;
  renamed to 7)
- `convergence_engine_v2_patches.py` and `polymarket_collector_v2.py` added to repo
  as reference artifacts (v2 code already live in main files — safe to delete)

### UX improvements (2026-03-19) ✓
- **Overview:** added De-escalation metric card, renamed "Escalation P" → "Escalation",
  "Active NOTAM Restrictions" → "Active NOTAMs"
- **Convergence Engine:** probability gauge replaced with two independent horizontal bars
  (tracks are no longer stacked / summing to 1); tension metric card added; bilateral
  velocity display; Polymarket table sidebar filters (track, min |edge|%, max rows)
- **ADS-B Monitor:** consistent sidebar state (collapsed); metrics row capped to top 5
  regions with remaining regions behind an expander
- **Formula explainer** on Convergence Engine page updated to reflect v2 math (ceiling
  normalisation, tension, independent tracks, bilateral velocity)

### Convergence engine v2 + Polymarket v2 (2026-03-19) ✓
See Stage 3 and Stage 4 sections above for full detail. Key changes:
- Independent track probabilities, ceiling normalisation, tension metric
- Claude-based market classification with keyword fallback
- `compute_edge()` function discounts edge under high tension
- Auto-migrated DB schema (no manual migration needed)

### Going-dark resolution fix + ceiling recalibration (2026-03-25) ✓
Three convergence engine issues identified from live data inspection:

**1. Going-dark events not resolving (bug)**
`adsb_collector.py`: when a VIP aircraft reappeared, `vip_last_seen.dark_flagged` was reset
to 0 (allowing new dark events) but `vip_dark_events.resolved_at` was never set. Aircraft
that went dark and reappeared multiple times accumulated stale active events. Fix: added
`UPDATE vip_dark_events SET resolved_at = ? WHERE icao24 = ? AND resolved_at IS NULL` in
two places — when aircraft is seen (in `process_watchlist`) and when creating a new dark
event (in `check_going_dark`, to close any leftover from the prior episode).
Stale events manually resolved on VPS (A6-PFA × 3, SU-GGG × 1).

**2. ESC_MAX_PTS = 200 too low (model calibration)**
Live data showed esc_raw regularly hitting 340-350 with `ESC_MAX_PTS=200`, permanently
saturating the sigmoid at ~99%. The original 200pt estimate was based on a simplified
"6 layers" model that didn't account for multi-signal depth within each layer (e.g.
up to 4 going-dark aircraft × 30pts = 120pts alone; 19 NOTAM FIRs × 10pts = 190pts).
Raised `ESC_MAX_PTS` to **400**. New curve: 50% prob at 200pts raw, 92% at 300pts,
99% at 380pts. Gives meaningful spread across realistic signal levels.

**3. Multiple engine instances accumulating (resolved by restart)**
Before a clean restart, 3 different process versions were writing to the scores table
simultaneously (3-5 rows per 10-minute window from different code versions). Resolved
by the systemd restart on 2026-03-25. Now 1 row per cycle as expected.

### Pending UI improvements (not yet done)
- **Stacked signal composition chart** — shows which signal types are driving the score
  over time. A spike that's all-NOTAM is less significant than a multi-layer convergence.

---

## What's Planned But Not Started

### Stage 5: Planespotter Layer (manual for now)
Telegram channels and planespotter forums pick up events faster than ADS-B.
Worth monitoring manually until volume justifies automation.

---

## To Run Everything

### Locally (macOS)
```bash
python3 adsb_collector.py --loop        # ADS-B + strategic tracking
python3 notam_collector.py --loop       # NOTAMs
python3 ais_collector.py --loop         # AIS maritime (needs ~7 days to build baseline)
python3 route_collector.py --refresh    # Build route baseline first (once)
python3 route_collector.py --loop       # Route suspension monitoring (daily)
python3 convergence_engine.py --loop    # Convergence scoring
python3 polymarket_collector.py --loop  # Polymarket ME market prices (every 15 min)
streamlit run dashboard.py              # Dashboard at localhost:8501
```

### On VPS (systemd)
```bash
systemctl start adsb-collector
systemctl start notam-collector
systemctl start ais-collector
systemctl start convergence-engine
systemctl start polymarket-collector
systemctl start route-collector
systemctl start marketsignal-dashboard

# Logs
tail -f /var/log/marketsignal/adsb.log
tail -f /var/log/marketsignal/notam.log
tail -f /var/log/marketsignal/dashboard.log
```

---

## Files in This Repo
```
dashboard.py                    Signal overview — unified anomaly feed + all-layer metrics
pages/
  1_GDELT_Explorer.py           GDELT BigQuery deep-dive explorer
  2_ADS-B_Monitor.py            ADS-B traffic monitoring dashboard
  3_NOTAM_Monitor.py            NOTAM airspace restriction dashboard
  4_Strategic_Monitor.py        VIP + strategic type clustering dashboard
  5_Convergence_Engine.py       Aggregated score, signal breakdown, coherence status
  6_Maritime_Monitor.py         AIS vessel counts, watchlist sightings, spoofing events
  7_Route_Monitor.py            Airline route suspension table, flags, per-route trend
utils/
  styles.py                     Shared Streamlit CSS + Plotly theme helpers
gdelt_collector.py              BigQuery → SQLite pipeline
gdelt_verify.py                 Historical event verification (5 events)
gdelt_query.py                  Ad-hoc date range query tool
gdelt_backtest.py               GDELT signal back-test + calibration diagnostics
ais_collector.py                aisstream.io AIS maritime collection + anomaly detection
VIP Vessels.csv                 Maritime watchlist (Iranian frigates, USNS logistics ships)
adsb_collector.py               OpenSky polling + Mode A/B detection
notam_collector.py              Cirium Sky API NOTAM collection
notam_check.py                  ICAO Data Services API sanity-check utility (ad-hoc testing)
convergence_engine.py           Stage 3 scoring daemon
polymarket_collector.py         Polymarket Gamma API → SQLite (polls every 15 min)
route_collector.py              Cirium Flex API route suspension collector
load_aircraft_db.py             One-time migration: CSV → SQLite aircraft_lookup table
VIP Aircraft.csv                Government/diplomatic aircraft watchlist (49 aircraft, 28 trackable)
.env.example                    Environment variable template
openapi.json                    Cirium NOTAM API OpenAPI spec (reference)
deploy/
  setup.sh                      Fresh VPS setup script (run as root)
  adsb-collector.service        systemd service
  notam-collector.service       systemd service
  ais-collector.service         systemd service
  convergence-engine.service    systemd service
  polymarket-collector.service  systemd service
  route-collector.service       systemd service
  marketsignal-dashboard.service systemd service
STATUS.md                       This file
```

### Leftover reference files (safe to delete)
```
convergence_engine_v2_patches.py   v2 patch notes — already merged into convergence_engine.py
polymarket_collector_v2.py         Identical to polymarket_collector.py — leftover from dev session
```

## Files NOT in Repo (local/VPS only)
```
.env                            Secrets (OpenSky, Cirium, BigQuery)
gdelt_credentials.json          Google BigQuery service account key
gdelt_events.db                 2.5M GDELT events (768MB)
adsb_events.db                  ADS-B + strategic tracking database
notam_events.db                 NOTAM database
convergence_engine.db           Convergence scores output database
route_events.db                 Route suspension database
ais_events.db                   AIS maritime vessel tracking database
polymarket_markets.db           Polymarket ME markets + price history
aircraft-database-complete.csv  616K-row source CSV (only needed for load_aircraft_db.py)
```
