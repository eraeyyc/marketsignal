---
name: investigate
description: Debug the live MarketSignal system — scan all service logs for errors, check service health, and verify DB freshness on the VPS
---

SSH to root@159.203.39.59 and run this full investigation:

**1. Service health**
Run: `systemctl is-active adsb-collector notam-collector ais-collector route-collector polymarket-collector convergence-engine marketsignal-dashboard`
Note any that are not "active".

**2. Error scan**
For each log file in /var/log/marketsignal/ (adsb.log, notam.log, ais.log, route.log, polymarket.log, convergence.log, dashboard.log), show the last 60 lines filtered for: ERROR, Exception, Traceback, WARN, "failed", "refused", "error"
Command pattern: `tail -60 /var/log/marketsignal/<name>.log | grep -iE "error|exception|traceback|warn|failed|refused"`

**3. DB freshness**
Run sqlite3 checks on each DB in /home/marketsignal/:
- `sqlite3 adsb_events.db "SELECT MAX(polled_at) FROM snapshots"`
- `sqlite3 notam_events.db "SELECT MAX(fetched_at) FROM notams"`
- `sqlite3 ais_events.db "SELECT MAX(timestamp) FROM events"`
- `sqlite3 convergence_engine.db "SELECT MAX(scored_at) FROM scores"`
- `sqlite3 polymarket_markets.db "SELECT MAX(updated_at) FROM markets"`

**4. Summary**
Report a plain-English summary:
- Which services are down (not active)
- Which log files contain errors (with the specific error message)
- Which DBs haven't had a new write in >2 hours (flag as stale) or >6 hours (flag as dead)
- Overall system health: OK / DEGRADED / CRITICAL
