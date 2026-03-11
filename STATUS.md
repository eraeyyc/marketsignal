# MarketSignal — Project Status
Last updated: 2026-03-11

---

## What This Is
A geopolitical signal detection system that monitors public data sources to detect
escalation/ceasefire signals in the Middle East before they get priced into
Polymarket prediction markets.

Core thesis: convergence of independent signal layers (aviation + maritime + diplomatic +
semantic) precedes the news cycle by hours to weeks. GDELT provides the historical
ground truth for back-testing.

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
- 7 known government/diplomatic aircraft tracked by ICAO24 code
- Logs every sighting with timestamp, region, lat/lon, altitude
- Going-dark detection: flags aircraft that vanish for >24h
- Source: `VIP Aircraft.csv` (17 aircraft total, 7 with confirmed ICAO24s)

**Mode B — Strategic Type Clustering**
- 9,978 strategic aircraft loaded from type database at startup
- 4 Type-Watch regions (Persian Gulf, Eastern Med, Horn of Africa, Caucasus)
- Watches: strategic lift (C17/IL76/C5M/Y20), tankers (KC135/KC46/A332),
  ISR/command (RC35/E3CF/E3TF/RQ4/E8), diplomatic bizjets (12 types)
- Flags surges >2σ above 30-day baseline (activates after ~25h of polling)
- Bizjet clustering: flags 3+ bizjets from different countries at same airport
- Run with: `python3 adsb_collector.py --loop` (same command as 2a)

### Stage 2c: NOTAM Monitor — DONE ✓ (blocked on API access)
Airspace restriction filings — leading indicator, hours before kinetic events.
- `notam_collector.py` — polls Laminar/Cirium API, stores to `notam_events.db`
- Auto-flags restriction Q-codes (QRTCA, QRPCA, QRDCA, QRALLT) as anomalies
- `first_detected_at` preserved on INSERT — critical for back-testing lead times
- Run with: `python3 notam_collector.py --loop`
- **BLOCKED**: Cirium Sky API migration requires new token from My Cirium portal.
  Old Laminar `user_key` format is deprecated. New auth: `Authorization: <token>` header.
  New base URL: `https://api.sky.cirium.com`, endpoint: `POST /v1/notams/`

### Dashboard — DONE ✓
Run with: `streamlit run dashboard.py`
- Page 1: GDELT Explorer — BigQuery charts, event table, Goldstein scale trend
- Page 2: ADS-B Monitor — live counts, trend chart, airline presence, anomaly log
- Page 3: NOTAM Monitor — active restrictions map, NOTAM feed, anomaly log
- Page 4: Strategic Monitor — VIP sightings, going-dark, type clusters, bizjet clusters

---

## Known Issues / Blockers

### NOTAM API — needs new Cirium Sky token
The old Laminar Data Hub API (`user_key` auth) has been migrated to Cirium Sky API.
Requires generating a new 48-character token from My Cirium portal:
> Management and Analytics → Credential Management → Request new credentials

Once token is in hand, update `API_TOKEN` in `notam_collector.py` line ~25.
The code is already correctly structured for the new API.

### VIP watchlist — 10 of 17 aircraft missing ICAO24
Only 7 of 17 VIP aircraft in `VIP Aircraft.csv` have confirmed ICAO24 codes.
The rest (USAF E-4B "doomsday plane", Chinese IL-76, USAF C-17, etc.) need
manual lookup via sites like ADS-B Exchange, Flightradar24, or Jetnet.

### ADS-B coverage gaps — conflict zones show zero
Lebanon/Syria and Yemen/Red Sea consistently return 0 aircraft — not because
airspace is empty but because ADS-B transponders are off in active conflict zones.
This is expected. The absence itself is worth noting but can't be baselined.

### Mode B baseline — needs ~25h to activate
The σ anomaly detection for strategic type clustering requires >24h of historical
data before comparisons can fire. Zeros are now recorded every poll so this builds
quickly once `--loop` is running.

---

## What's Planned But Not Started

### Stage 2d: Maritime / AIS Layer
Ships are as important as aircraft for Middle East signals:
- Carrier strike group positioning (Hormuz, Arabian Sea)
- Tanker rerouting away from Hormuz/Red Sea
- Suez Canal traffic anomalies
- Warship surge in Gulf of Oman
Same structure as ADS-B. MarineTraffic or VesselFinder API.

### Stage 3: Semantic Drift
Embed State Dept / foreign ministry press briefings using Claude API.
Track when official language shifts toward negotiation vs confrontation vocabulary.
Acceleration of semantic drift = signal. Planned implementation:
- Weekly scrape of State Dept briefing transcripts
- Embed each briefing, track cosine distance from "war" vs "peace" cluster centroids
- Feed drift score into convergence engine

### Stage 4: Convergence Engine
Score all signals together with time-decay. Each signal fires with an initial
score that decays at a type-specific rate. Signals in the correct escalation
sequence get a coherence multiplier.

Escalation sequence (earliest → latest):
```
Strategic lift surge    → ~21 days before
Tanker surge            → ~14 days before
ISR concentration       →  ~7 days before  (ISR doesn't deploy speculatively)
NOTAM restrictions      →  ~2 days before
Aircraft going dark     →  ~0.5 days before
```

Decay rates (per day):
- Strategic lift: 0.03 | Tanker: 0.04 | ISR: 0.06
- Bizjet: 0.10 | Route suspension: 0.12 | Semantic drift: 0.15
- NOTAM restriction: 0.35 | AWACS: 0.40 | Going dark: 0.60

Two parallel tracks: escalation + de-escalation (ceasefire signals same framework in reverse).
Score acceleration (rising trend) triggers alerts earlier than static high score.
All signal memory persists to DB so restarts don't lose state.

### Stage 5: Polymarket Integration
Surface relevant prediction markets, suggest position sizing based on convergence
score. Human makes the final trading decision.

### Stage 6: Planespotter Layer (manual for now)
Telegram channels and planespotter forums pick up events faster than ADS-B.
Worth monitoring manually until volume justifies automation.

---

## To Run Everything

```bash
# ADS-B + strategic tracking (one terminal)
python3 adsb_collector.py --loop

# NOTAM monitoring (second terminal — needs Cirium Sky token first)
python3 notam_collector.py --loop

# Dashboard (third terminal)
streamlit run dashboard.py
```

---

## Files in This Repo
```
dashboard.py                    Streamlit app entry point
pages/
  1_GDELT_Explorer.py           GDELT BigQuery dashboard
  2_ADS-B_Monitor.py            ADS-B traffic monitoring dashboard
  3_NOTAM_Monitor.py            NOTAM airspace restriction dashboard
  4_Strategic_Monitor.py        VIP + strategic type clustering dashboard
gdelt_collector.py              BigQuery → SQLite pipeline
gdelt_verify.py                 Historical event verification (5 events)
gdelt_query.py                  Ad-hoc date range query tool
adsb_collector.py               OpenSky polling + Mode A/B detection
notam_collector.py              Cirium Sky API NOTAM collection
notam_check.py                  NOTAM API sanity check script
VIP Aircraft.csv                Government/diplomatic aircraft watchlist
openapi.json                    Cirium NOTAM API v2 OpenAPI spec
STATUS.md                       This file
```

## Files NOT in Repo (local only)
```
credentials.json                OpenSky OAuth2 credentials
gdelt_credentials.json          Google BigQuery service account key
gdelt_events.db                 2.5M GDELT events (768MB)
adsb_events.db                  ADS-B + strategic tracking database
notam_events.db                 NOTAM database
aircraft-database-complete.csv  616K-row ICAO24 → typecode lookup (large)
Cirium Sky API Endpoints.xlsx   Migration endpoint mapping
```
