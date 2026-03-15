# MarketSignal — Project Status
Last updated: 2026-03-15

---

## What This Is
A geopolitical signal detection system that monitors public data sources to detect
escalation/ceasefire signals in the Middle East before they get priced into
Polymarket prediction markets.

Core thesis: convergence of independent signal layers (aviation + maritime + diplomatic)
precedes the news cycle by hours to weeks. GDELT provides the historical ground truth
for back-testing.

**Live on:** DigitalOcean VPS — 1 vCPU / 1GB RAM, Ubuntu 24.04, IP: 146.190.142.71
**Dashboard:** http://146.190.142.71:8501
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

### Stage 2e: Route Suspension Collector — DONE ✓ (pending first run)
Monitors scheduled vs. actual operated flights for 13 watched airlines across all
ME airport pairs. A sustained route suspension is a leading indicator — airlines pull
service quietly before governments announce closures.
- `route_collector.py` — polls Cirium Flex API daily
- Compares scheduled vs. operated flights over 7-day rolling window
- Flags when operated flights drop >60% below schedule for 3+ consecutive days
- Stores in `route_events.db` with `first_detected_at` + `last_confirmed_at`
- Feeds into convergence engine as `route_suspension` signals (λ=0.12/day, Event type)
- Auth: Cirium Flex API — `appId` + `appKey` query params
- Base URL: `https://api.flightstats.com/flex`
- Run: `python3 route_collector.py --refresh` (schedule cache), then `--loop`
- Airport pairs monitored (both directions): TLV, AMM, BGW, KWI, BAH, DOH, DXB,
  AUH, MCT, IKA, THR, BEY, CAI, IST

### Stage 3: Convergence Engine — DONE ✓ (AIS wired in 2026-03-15)
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
  notam_high 5.0 | strategic_lift_high 16.0 | vip_sighting 5.0
- **Lambda values (per day):** strategic_lift 0.03 | tanker 0.04 | ISR 0.06 |
  bizjet 0.10 | route_suspension 0.12 | NOTAM 0.35 | going_dark 0.60
- **Coherence multiplier** (1.5×): fires only when 2+ signals in same region both score > 2.0
- **Sigmoid normalisation** → 0–1 probability for Polymarket comparison; β=100
- **Divergence detection:** flags when GDELT and physical signals contradict each other
- **NOTAM signal fix (2026-03-15):** Cirium bounding box returns non-ME FIRs (Romania,
  Russia, Greece, India). Added ME FIR whitelist (19 FIRs). Deduplicate by FIR not
  notam_id — one signal per FIR, worst severity. Reduced notam_high 14→5, notam_medium 7→3.
  Raised SIGMOID_BETA 30→100. Was producing P=100% from 195 spurious signals.
- **⚠ S_0 weights are placeholders** — must be calibrated via GDELT back-test
- **⚠ Sigmoid β=100 is a rough calibration** — refine after back-test
- **AIS signals wired in:** tanker/cargo density drops + military surges + watchlist sightings
  → S0: ais_tanker_medium 6.0 | ais_tanker_high 12.0 | ais_military_high 14.0 | ais_watchlist 8.0
  → λ: ais_tanker 0.04/day | ais_military 0.06/day
- Run with: `python3 convergence_engine.py --loop`
- Check signals: `python3 convergence_engine.py --signals`
- Outputs to: `convergence_engine.db`
- Systemd: `convergence-engine.service` (deploy/convergence-engine.service)

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
- **Page 5:** Convergence Engine — probability display, score history chart, live signal
  breakdown table, coherence multiplier status, divergence flag, scoring explainer

---

## Known Issues / Blockers

### GDELT signal redesigned — Goldstein average approach (2026-03-14) ✓
Old count-based approach was permanently saturated. Replaced with:
- `avg_goldstein(30d)` vs `avg_goldstein(prior 60d baseline)`
- Negative delta → escalation signal; positive → de-escalation
- Back-tested against 7 historical events: 4/4 escalation, 2/3 de-escalation correct
- Fixes silent column-name bug (old code used BigQuery names, never actually ran)
- Live right now: delta=-1.47 (escalation signal scoring 4.67) as of 2026-03-14

**Future improvement:** add event volume weighting — a Goldstein drop with 5× normal
event count is more significant than the same drop at baseline volume. Would multiply
signal score by `log(count_ratio) + 1`. Not done yet because Goldstein-only already
validates correctly and volume weighting needs backtest re-validation.

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

## What's Planned But Not Started

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
- **Future:** add event volume weighting to GDELT signal; satellite AIS for Arabian Sea gaps

### Stage 4: Polymarket Integration — IN PROGRESS
Surface relevant prediction markets, suggest position sizing based on convergence
score. Human makes the final trading decision. Look for "incongruent markets" —
high convergence score but low Polymarket probability = entry point.

### Stage 5: Planespotter Layer (manual for now)
Telegram channels and planespotter forums pick up events faster than ADS-B.
Worth monitoring manually until volume justifies automation.

---

## To Run Everything

### Locally (macOS)
```bash
python3 adsb_collector.py --loop        # ADS-B + strategic tracking
python3 notam_collector.py --loop       # NOTAMs
python3 convergence_engine.py --loop    # Convergence scoring
streamlit run dashboard.py              # Dashboard at localhost:8501
```

### On VPS (systemd)
```bash
systemctl start adsb-collector
systemctl start notam-collector
systemctl start marketsignal-dashboard
systemctl start convergence-engine

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
gdelt_collector.py              BigQuery → SQLite pipeline
gdelt_verify.py                 Historical event verification (5 events)
gdelt_query.py                  Ad-hoc date range query tool
gdelt_backtest.py               GDELT signal back-test + calibration diagnostics
ais_collector.py                aisstream.io AIS maritime collection + anomaly detection
VIP Vessels.csv                 Maritime watchlist (Iranian frigates, USNS logistics ships)
adsb_collector.py               OpenSky polling + Mode A/B detection
notam_collector.py              Cirium Sky API NOTAM collection
convergence_engine.py           Stage 3 scoring daemon
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
  marketsignal-dashboard.service systemd service
STATUS.md                       This file
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
aircraft-database-complete.csv  616K-row source CSV (only needed for load_aircraft_db.py)
```
