# Future work (not scheduled)

- **word2vec + BM25 retrieval** (raised 2026-10-09): IMPLEMENTED 2026-10-10 as `read.word_vector_leg`
  (model2vec `minishlab/potion-retrieval-32M` by default, or gensim word2vec via `read.word_vector_provider: word2vec`
  + a local model path). Use with `read.hybrid: false` to replace BM25, or alongside it. Arms:
  `arms/qs-eq06-fix-wv-nobm25.json`, `arms/qs-eq06-fix-wv-plusbm25.json`. 2-question test passed; next: one-conversation
  screen, then full run. Open: cascade mode (word vectors pick candidates, dense vectors re-score) if wanted.

  **Screen result 2026-10-10 (conv-26, 152 q, paired vs the fixed config 83.6%):** word vectors in place of BM25
  78.9% (+6/-13); alongside BM25 80.9% (+7/-11). BM25 finds rare exact terms (names, "LGBTQ") that pooled word
  vectors blur; a third leg also dilutes RRF fusion (all gold surviving fusion 62% -> 51%). Temporal improves (83.8 ->
  86.5-91.9, small n). Decision: keep the leg as an opt-in option, BM25 stays on by default.
