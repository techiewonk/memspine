#!/usr/bin/env bash
# Third ablation: date fixes without the wider window, combined with the reader fixes.
cd "$(dirname "$0")" || exit 1
while ! grep -q "ABLATION2 DONE" runs/_logs/qa_fix_times.txt 2>/dev/null; do sleep 60; done
export ITEMS=1
TOPK=10 bash run_fix_qa.sh _test-dates-cpu -abl-reader-dates          # A5 reader fixes + anchored dates + date-mention leg (+-2 window)
TOPK=10 bash run_fix_qa.sh _test-fix-cpu -abl-reader-window-dates     # A6 reader fixes + window 2/4 + dates
echo ABLATION3 DONE >> runs/_logs/qa_fix_times.txt
