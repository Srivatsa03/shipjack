# Retrieval: what finds the right page

The store's knowledge base answers two kinds of questions for the agents: which product fits a need,
and what the store's policy says. This measures how often it puts the right page first.

## The set

48 questions in [shipjack/data/retrieval_eval.jsonl](../shipjack/data/retrieval_eval.jsonl), written the
way a shopper asks, each with the pages that answer it:

- 20 product questions that avoid the product's own words ("something to connect two monitors to my
  laptop", "keep my phone where I can see it while driving");
- 8 exact-term product searches ("Thunderbolt 4", "UHS-II reader", "65W GaN white");
- 20 policy questions ("the parcel already left, can it go to my office instead").

Over the sample Shopify export (32 products) and the store policy (6 sections).
hit@1: the first result answers the question. hit@3: one of the first three does (the agents read
the top 3 to 4). Run it with `shipjack retrieval-eval --db postgresql://... --embedders hash,model2vec,fastembed`.

## Results

| Embedder | Search | Products (paraphrased) hit@1 / hit@3 | Exact terms hit@1 / hit@3 | Policies hit@1 / hit@3 | p50 |
|---|---|---|---|---|---|
| hash | keyword | 55% / 80% | 100% / 100% | 55% / 75% | 3.8 ms |
| hash | vector | 55% / 75% | 100% / 100% | 60% / 80% | 3.7 ms |
| hash | hybrid, keyword x1 | 60% / 80% | 100% / 100% | 60% / 80% | 6.7 ms |
| hash | hybrid, keyword x0.5 | 55% / 85% | 100% / 100% | 65% / 80% | 6.6 ms |
| hash | hybrid, keyword x0.25 | 60% / 85% | 100% / 100% | 65% / 80% | 6.9 ms |
| minishlab/potion-base-8M | keyword | 55% / 80% | 100% / 100% | 55% / 75% | 3.3 ms |
| minishlab/potion-base-8M | vector | 80% / 95% | 100% / 100% | 65% / 90% | 4.3 ms |
| minishlab/potion-base-8M | hybrid, keyword x1 | 70% / 85% | 100% / 100% | 65% / 90% | 7.3 ms |
| minishlab/potion-base-8M | hybrid, keyword x0.5 | 75% / 90% | 100% / 100% | 60% / 90% | 7.7 ms |
| minishlab/potion-base-8M | hybrid, keyword x0.25 | 75% / 95% | 100% / 100% | 60% / 90% | 8.4 ms |
| BAAI/bge-small-en-v1.5 | keyword | 55% / 80% | 100% / 100% | 55% / 75% | 3.3 ms |
| BAAI/bge-small-en-v1.5 | vector | 90% / 95% | 100% / 100% | 85% / 100% | 17.5 ms |
| BAAI/bge-small-en-v1.5 | hybrid, keyword x1 | 70% / 95% | 100% / 100% | 75% / 100% | 24.8 ms |
| BAAI/bge-small-en-v1.5 | hybrid, keyword x0.5 | 80% / 95% | 100% / 100% | 80% / 100% | 23.2 ms |
| BAAI/bge-small-en-v1.5 | hybrid, keyword x0.25 | 85% / 95% | 100% / 100% | 80% / 100% | 24.0 ms |

`hash` is a bag-of-words baseline with no model. p50 is one query on a laptop, including the embedding.

## What we chose, and why

- **Vector search is the default.** On paraphrased questions it is far ahead of keyword search
  (bge-small: 90% vs 55% of product questions right first time; policies 85% vs 55%).
- **Hybrid is not the default.** Equal-weight fusion cost 20 points of first-hit accuracy on products,
  because the weaker keyword ranking pulled good vector results down. Down-weighting keywords to x0.25
  recovers most of it but never beats vector alone here. The exact-term queries that hybrid exists for
  were all found by every method on a 32-product catalog. Hybrid stays a setting
  (`SHIPJACK_SEARCH_MODE=hybrid`, `SHIPJACK_KEYWORD_WEIGHT`) for catalogs with thousands of SKUs.
- **The deployed demo uses model2vec**, not bge-small: about 140 MB of memory instead of 340 MB, so it
  fits a 512 MB server next to the store. The cost is accuracy on policy questions (65% vs 85% first
  hit, 90% vs 100% in the top 3). Where memory allows, bge-small is the better model.

## Limits

- 48 questions, written by the same person who wrote the catalog and the policies. Enough to see large
  gaps (55% vs 90%), not small ones: one question is 5 points in a group of 20.
- One small store. A catalog of 10,000 products with similar names would stress all three methods
  differently.
- The weights were compared on the same set they are reported on; there is no held-out split.
