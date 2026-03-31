#!/bin/bash
# Auto-deploy to VPS after git push.
# Always restarts marketsignal-dashboard.
# Also restarts any collector service whose file was changed in the last commit.

VPS="root@159.203.39.59"
APP_DIR="/home/marketsignal"

# Files changed in last commit
CHANGED=$(git show --name-only --format="" HEAD 2>/dev/null)

# Always restart dashboard
SERVICES="marketsignal-dashboard"

# Add collector service if its source file changed
echo "$CHANGED" | grep -q "adsb_collector.py"        && SERVICES="$SERVICES adsb-collector"
echo "$CHANGED" | grep -q "notam_collector.py"       && SERVICES="$SERVICES notam-collector"
echo "$CHANGED" | grep -q "ais_collector.py"         && SERVICES="$SERVICES ais-collector"
echo "$CHANGED" | grep -q "convergence_engine.py"    && SERVICES="$SERVICES convergence-engine"
echo "$CHANGED" | grep -q "polymarket_collector.py"  && SERVICES="$SERVICES polymarket-collector"
echo "$CHANGED" | grep -q "route_collector.py"       && SERVICES="$SERVICES route-collector"

echo "Deploying to VPS — restarting: $SERVICES"

RESULT=$(ssh -o StrictHostKeyChecking=no root@159.203.39.59 \
  "cd $APP_DIR && git pull --ff-only 2>&1 && systemctl restart $SERVICES 2>&1 && systemctl is-active $SERVICES 2>&1")

echo "$RESULT"

# Output context back to Claude
python3 -c "
import json, sys
services = '$SERVICES'.split()
result = sys.stdin.read().strip()
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PostToolUse',
        'additionalContext': f'VPS deploy complete. Services restarted: {services}. Output: {result}'
    }
}))
" <<< "$RESULT"
