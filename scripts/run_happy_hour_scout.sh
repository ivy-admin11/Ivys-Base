#!/bin/bash

# Happy Hour Scout Launcher — Lifestyle exploration automation
# Scheduled via launchd for weekly Monday noon alerts

cd /Users/lexi/openclaw-admin

# Load environment
if [ -f .env ]; then
    export $(cat .env | grep -v '#' | xargs)
fi

# Execute scout cycle with live alert dispatch
python3 -c "
from proactive_agents.happy_hour_scout import execute_scout_cycle
import sys
sys.path.insert(0, '/Users/lexi/openclaw-admin')
result = execute_scout_cycle(send_alert=True)
sys.exit(0 if result['status'] == 'success' else 1)
" 2>&1 | tee -a ~/.ivy_logs/happy_hour_scout.log

exit $?
