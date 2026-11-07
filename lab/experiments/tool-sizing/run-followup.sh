#!/bin/bash
# After run-rest.sh: hybrid layout on latency NFS, and the next dev memory floor (fixed process-group kill).
cd "$(dirname "$0")"
while pgrep -f "[r]un-rest.sh" >/dev/null || pgrep -f "[r]unner.py" >/dev/null; do sleep 5; done
python3 runner.py storage --modes hybrid-lat --repeats 2 && python3 runner.py mem --only nextdev
echo "FOLLOWUP DONE rc=$?"
