#!/usr/bin/env bash
# After the first ablation: reader fixes with the ORIGINAL judge, to split real gain from judge leniency.
cd "$(dirname "$0")" || exit 1
while ! grep -q "ABLATION DONE" runs/_logs/qa_fix_times.txt 2>/dev/null; do sleep 60; done
ITEMS=1 TOPK=10 FLAGS="--qa-prompt grounded --retry-refusal" bash run_fix_qa.sh _test-roff-cpu -abl-reader-nojudgeguard
echo ABLATION2 DONE >> runs/_logs/qa_fix_times.txt
