# Chunk size sensitivity analysis

Compares the system with documents split into chunks of 256, 512 and 1024
characters, on the same company corpus and against the same ground truth.
512/64 is the configuration of the validation campaigns and is the baseline.

This analysis was not run for the thesis: it is listed among the future
developments (thesis §3.4.1 and §6.5). This document is the procedure for
running it.

## Design choices

- **One company, same corpus, same ground truth.** Use the documents and the
  ground truth of the validation campaigns for that company (CSV with the
  columns `requirement_id` and `gt_verdict`).
- **Overlap proportional to the chunk size (12.5%)**, so that only the
  granularity changes: 256/32, 512/64, 1024/128.
- **k fixed at 12** (`ORG_CONTEXT_CHUNKS`): the system is tested as it is.
  The amount of text shown to the model therefore grows with the chunk size
  (about 3k, 6k and 12k characters of company documents); state this when
  reporting the results. To keep the volume constant instead, set
  `ORG_CONTEXT_CHUNKS` to 24 / 12 / 6 and say so.
- **K = 1 run per configuration is enough**: the campaigns showed IR = 100%
  at temperature zero. Use `--runs 3` if there is time, to measure IR too.
- **Dedicated org_id** (e.g. `chunksens-aerarium`): `prepare` deletes all
  the company-document chunks of that org_id, and nothing else.
- Do not use the UI while the experiment is running: the ISO index is shared.

## Procedure

Repeat steps 1-4 for each configuration, starting from 512 (it reproduces
the campaign and checks that everything works), then 256 and 1024.

1. Set the configuration in `.env`:

       CHUNK_SIZE=256
       CHUNK_OVERLAP=32

2. Recreate the application services so that they all read the new values:

       docker compose up -d --force-recreate as1 as2 as3 aga aiu orchestrator

3. Align the index with the configuration (inside AS-1). It waits for AS-1
   to rebuild the ISO index if needed, rebuilds it otherwise, and deletes the
   experiment's company chunks:

       docker compose exec as1 python scripts/chunk_sensitivity.py prepare --org-id chunksens-aerarium

4. Run the agents (on the host). The script refuses to start if any agent
   reports a different chunk size:

       python scripts/chunk_sensitivity.py run --org-id chunksens-aerarium \
           --docs-dir ./corpus/aerarium --label 256 --runs 1 --out results/chunks

After the three configurations:

5. Compare them:

       python scripts/chunk_sensitivity.py compare --out results/chunks \
           --ground-truth ./ground_truth/aerarium.csv --baseline 512

   It prints a table (also saved as `summary.csv`) and the list of verdicts
   that changed against 512 (`summary.json`).

6. Restore the campaign configuration: set `CHUNK_SIZE=512`,
   `CHUNK_OVERLAP=64`, recreate the services as in step 2 and run `prepare`
   once more, so that the ISO index is rebuilt with 512/64.

## Reading the table

| Column | Meaning |
|---|---|
| TCN %, EEC % | Structural and effective coverage (EEC excludes degraded cards) |
| exact % | Cards with exactly the ground-truth verdict |
| TA auto % | Automatic approximation of the reviewer's scoring: exact or adjacent level (C/PC, PC/NC) or applicability disagreement; C vs NC is an error |
| errors | C vs NC disagreements |
| C/PC/NC/NA | Verdict distribution |
| Gamma | Internal summary index, Eq. (3.6) of the thesis |
| downgrades | Verdicts downgraded by the grounding check (AUTO-FLAG) |
| evid. dropped | AUTO-FLAG notes about citations not found or quoting the standard |
| evid./card | Verified evidences per card |
| IR % | Only with `--runs` > 1 |

`TA auto` approximates, but does not replace, the human scoring used in the
thesis: check by hand the cards listed as changed against the baseline
before reporting a TA value.

## Expected duration

One model call per requirement and 70 requirements per run. On a laptop
with CPU/Metal inference, count roughly the duration of one campaign run
per configuration, plus a few minutes to rebuild the ISO index.
