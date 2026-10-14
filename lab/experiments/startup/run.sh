#!/bin/bash
# $1 = label; runs 9 one-turn chats with the snapshot preload, then 5 strace -c runs killed at request time
for i in $(seq 1 9); do SNAP_FILE=/snap/$1.jsonl NODE_OPTIONS="--require /snap/snap.cjs" PI_OFFLINE=1 pi --mode json "SCENARIO: chat chars=200" >/dev/null 2>&1; done
for i in $(seq 1 5); do SNAP_FILE=/snap/$1-kill.jsonl SNAP_KILL=1 NODE_OPTIONS="--require /snap/snap.cjs" PI_OFFLINE=1 strace -f -c -o /snap/$1-strace-$i.txt pi --mode json "SCENARIO: chat chars=200" >/dev/null 2>&1; done
for i in $(seq 1 5); do SNAP_FILE=/dev/null PI_OFFLINE=1 strace -f -c -o /snap/$1-strace-full-$i.txt pi --mode json "SCENARIO: chat chars=200" >/dev/null 2>&1; done
echo "session dir:"; du -sb /home/agent/.pi/agent/sessions 2>/dev/null; find /home/agent/.pi/agent/sessions -type f | wc -l
