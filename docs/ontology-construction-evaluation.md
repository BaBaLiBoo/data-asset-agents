# Ontology Construction Evaluation

## Gold isolation

Gold is loaded only after candidate generation. It may be used by an acceptance
test reviewer to simulate decisions a person could make after seeing candidates,
and by the independent evaluator for scoring. It is never passed to the candidate
generator, never used for RAW_METADATA table classification, and never read before
generation reaches `CANDIDATES_READY`.

## Reproducible reports

Run the file-based mock ablation with:

```powershell
.\.venv\Scripts\python.exe scripts\run_ontology_construction_mock.py
```

It writes new reports without overwriting the historical report set:

- `reports/ontology_construction/local_mock_ablation.json`
- `reports/ontology_construction/local_mock_ablation.csv`
- `reports/ontology_construction/local_mock_ablation.md`

The JSON records Git SHA, database/file snapshot hash, metadata snapshot hash,
profiling hash, historical SQL hash, Gold hash, catalog mode, construction mode,
evidence mode, candidate and decision counts, acceptance/modification/rejection
rates, strict compilation status, and all three leakage flags.

`strict_validation_passed` in the file-only report means deterministic strict
compilation had no conflicts and did not access seed, fallback, or legacy
ontology. It does not claim PostgreSQL EXPLAIN, publication, activation, or a live
model run. Those runtime gates are covered by Docker acceptance.

## Results actually run on 2026-07-27

- Ruff and the unit/specialty suites: run locally with Python 3.12.
- PostgreSQL integration suite: run against the real MiniBank PostgreSQL
  container, not SQLite or a memory repository.
- Fresh-Volume Docker acceptance: run through DDL 001-010, real RAW_METADATA
  snapshot, 96 candidates, post-generation test review, strict publication,
  API restart, PostgreSQL persistence queries, and four Text-to-SQL queries.
- Old-Volume upgrade acceptance: run with preserved pre-009 data and idempotent
  DDL 009/010.
- O-A through O-D mock ablation: run deterministically. O-D is explicitly mock
  semantic enrichment.
- The 80-case benchmark definition was validated, but an 80-case live LLM
  experiment was not run because no live API key was used.

The Docker construction acceptance also exercises queries after publishing the
reviewed construction ontology. Manual-seed/Gold and construction versions remain
separate; ontology source and SQLAsset enablement are not treated as one
experimental variable. Result hashes from mock smoke runs are reproducibility
evidence, not claims of live-model quality.
