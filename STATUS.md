# MarketSignal — Project Status
Last updated: 2026-03-10

---

## What This Is
A geopolitical signal detection system that monitors public data sources to detect
escalation/ceasefire signals in the Middle East before they get priced into
Polymarket prediction markets.

---

## What's Been Built

### Stage 1: GDELT — DONE ✓
Historical geopolitical event database. 2.5 million events, Jan 2023–present.
- `gdelt_collector.py` — pulls from Google BigQuery, stores to local SQLite
- `gdelt_verify.py` — verified against 5 known historical events (all pass)
- `gdelt_query.py` — quick date-range query tool
- `gdelt_events.db` — the database (local only, not in git — too large)

### Stage 2a: ADS-B Airspace Monitor — DONE ✓
Live aircraft tracking across 9 Middle East bounding boxes every 10 minutes.
- `adsb_collector.py` — polls OpenSky Network via OAuth2
- 663 snapshots collected so far (Mar 8–10)
- Detects: airline presence/absence, aircraft counts by region, anomalies vs baseline
- `adsb_events.db` — the database (local only, not in git)

### Dashboard — DONE ✓
Run with: `streamlit run dashboard.py`
- Page 1: GDELT Explorer (BigQuery charts, event table, Goldstein trend)
- Page 2: ADS-B Monitor (live counts, trend chart, airline presence, anomaly log)

---

## What's In Progress

### Stage 2b: Dual-Mode Aircraft Tracking — NEXT
Extend `adsb_collector.py` to detect:

**Mode A — VIP Watchlist**
Specific known government/diplomatic aircraft (`VIP Aircraft.csv`).
Flag when watched aircraft cluster together at neutral airports (Doha, Muscat, Ankara).

**Mode B — Type Clustering**
No tail numbers needed. Watch for surges of:
- Strategic lift (C-17, C-5, IL-76) — logistics pre-positioning
- Tankers (KC-135, KC-46, A330 MRTT) — "if tankers surge, fighters are coming"
- ISR/AWACS (RC-135, E-3, Global Hawk) — active intelligence collection
- Diplomatic bizjets (Gulfstream, Global, Falcon) — back-channel negotiations

The escalation sequence to detect (weeks to hours before an event):
```
Weeks 3-4:  Strategic lift surge + tanker pre-positioning
Weeks 1-2:  Tanker staging intensifies + ISR concentration
Days before: AWACS + commercial route pulls + NOTAMs filing
Hours before: Aircraft go dark + fighters forward deploy
```

### Stage 2c: NOTAMs — BLOCKED
ICAO API is down (502 errors). Will build once it recovers.

### Stage 2d: Maritime/AIS — PLANNED
Same concept as ADS-B but for ships. Hormuz tanker routing, carrier strike groups,
Suez Canal anomalies. Belongs in Stage 2.

---

## What's Coming Later

**Stage 3: Semantic Drift**
Embed State Dept / foreign ministry press briefings. Track when language
shifts toward negotiation vs confrontation vocabulary.

**Stage 4: Convergence Engine**
Score all signals together with time-decay (old signals matter less).
Two parallel tracks: escalation signature + de-escalation signature.
Alert when multiple independent signals converge.

**Stage 5: Polymarket Integration**
Surface relevant markets, suggest position sizing based on convergence score.
Human makes the final call.

---

## Immediate Next Steps (in order)
1. **Restart ADS-B collector** after computer reboot:
   ```
   cd /Users/MAC/projects/marketsignal
   python3 adsb_collector.py --loop
   ```
2. **Build dual-mode tracking** (Stage 2b) — extend adsb_collector.py
3. **Add maritime/AIS layer** (Stage 2d)
4. **Build NOTAM collector** once ICAO API recovers (Stage 2c)

---

## Files in This Repo
```
dashboard.py                  Streamlit app entry point
pages/
  1_GDELT_Explorer.py         GDELT dashboard page
  2_ADS-B_Monitor.py          ADS-B dashboard page
gdelt_collector.py            BigQuery → SQLite pipeline
gdelt_verify.py               Historical event verification
gdelt_query.py                Ad-hoc date range query tool
adsb_collector.py             OpenSky polling + anomaly detection
notam_check.py                NOTAM API sanity check (blocked)
VIP Aircraft.csv              Government/diplomatic aircraft watchlist
project ideas.md              Architecture notes and design thinking
STATUS.md                     This file
```

## Files NOT in Repo (local only)
```
credentials.json              OpenSky OAuth2 client credentials
gdelt_credentials.json        Google BigQuery service account key
gdelt_events.db               2.5M GDELT events (768MB)
adsb_events.db                ADS-B snapshots (growing)
aircraft-database-complete.csv  ICAO24 → aircraft type lookup (large)
```
