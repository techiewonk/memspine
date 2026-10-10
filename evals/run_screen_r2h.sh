#!/usr/bin/env bash
# R2-4 context order on the best dev config, after the A9 think-on run frees the GPU.
cd "$(dirname "$0")" || exit 1
while ! grep -q "A9ON2 DONE" runs/_logs/a9.txt 2>/dev/null; do sleep 60; done
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
B="--retry-refusal --judge-guards --judge-date-check"
for v in hits_first hit_blocks; do
  bash run.sh --arm BEST_dev_2026-10-10 --run-id r2-$v-t2 --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "--qa-prompt grounded_ordered --memspine-context-order $v $B" --force || echo "r2-$v CHECK FAILED" >> runs/_logs/r2.txt
  bash run.sh --arm BEST_dev_2026-10-10 --run-id r2-$v-i2 --mode qa --topk 10 --items 2 --questions 233 --forensics --batch-turns 32 --flags "--qa-prompt grounded_ordered --memspine-context-order $v $B" --force && echo "r2-$v done" >> runs/_logs/r2.txt || echo "r2-$v FAILED" >> runs/_logs/r2.txt
done
echo R2H DONE >> runs/_logs/r2.txt
