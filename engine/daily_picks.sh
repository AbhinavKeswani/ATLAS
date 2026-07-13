#!/bin/zsh
# Atlas daily Picks job: refresh scores on fresh prices + gap-fill adjusted closes.
cd /Users/akeswani/Atlas/engine || exit 1
.venv/bin/python -m atlas.daily_job >> /Users/akeswani/Atlas/engine/daily_job.log 2>&1
