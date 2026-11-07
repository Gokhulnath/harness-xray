#!/bin/bash
# Runs the remaining phases one after another once the profile phase has finished.
cd "$(dirname "$0")"
while pgrep -f "[r]unner.py profile" >/dev/null; do sleep 5; done
python3 runner.py calib && python3 runner.py cpu --repeats 2 && python3 runner.py period && python3 runner.py mem \
  && python3 runner.py storage --repeats 2 && python3 runner.py nfsopts
echo "ALL PHASES DONE rc=$?"
