#!/bin/bash
# Deterministic mock control (3 x A/C), then the real Sonnet runs through the subscription (3 x A/C).
cd "$(dirname "$0")"
python3 runner.py --modes A C --repeats 3 --tag mock && python3 runner.py --modes A C --repeats 3 --tag real --real
echo "CC-OFFLOAD DONE rc=$?"
