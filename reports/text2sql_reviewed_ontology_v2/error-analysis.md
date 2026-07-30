# Text-to-SQL Quality V2 Error Analysis

All counts below come from completed persisted Case results. No mock cases are substituted.

| Group | Cases | Successful cases | Result hash accuracy | Main failures |
|---|---:|---:|---:|---|
| T-A | 80 | 0 | 0.013514 | BUSINESS_POLICY_ERROR: 74, CLARIFICATION_ERROR: 3, UNSUPPORTED_CLASSIFICATION_ERROR: 3 |
| T-B | 80 | 28 | 0.418919 | RESULT_MISMATCH: 25, COLUMN_SELECTION_ERROR: 15, TABLE_SELECTION_ERROR: 5, CLARIFICATION_ERROR: 3, UNSUPPORTED_CLASSIFICATION_ERROR: 3, BUSINESS_POLICY_ERROR: 1 |
| T-C | 80 | 40 | 0.459459 | RESULT_MISMATCH: 19, COLUMN_SELECTION_ERROR: 13, SEMANTIC_PARSE_ERROR: 8 |
| T-D | 80 | 44 | 0.513514 | RESULT_MISMATCH: 15, COLUMN_SELECTION_ERROR: 13, SEMANTIC_PARSE_ERROR: 8 |
| T-E | 80 | 39 | 0.445946 | RESULT_MISMATCH: 20, COLUMN_SELECTION_ERROR: 13, SEMANTIC_PARSE_ERROR: 8 |
| T-F | 80 | 43 | 0.5 | RESULT_MISMATCH: 15, COLUMN_SELECTION_ERROR: 13, SEMANTIC_PARSE_ERROR: 8, TABLE_SELECTION_ERROR: 1 |
| T-G | 80 | 62 | 0.756757 | RESULT_MISMATCH: 15, TABLE_SELECTION_ERROR: 3 |
| T-H | 80 | 66 | 0.810811 | RESULT_MISMATCH: 12, TABLE_SELECTION_ERROR: 1, PROVIDER_ERROR: 1 |

## Result-accuracy deltas

| Comparison | Delta |
|---|---:|
| O-C vs Schema, SQLAsset disabled | +0.445945 |
| O-C vs Physical RAG, SQLAsset disabled | +0.040540 |
| O-D vs O-C, SQLAsset disabled | -0.013513 |
| O-D vs O-C, SQLAsset enabled | -0.013514 |
| O-C gap to Gold, SQLAsset disabled | -0.297298 |
| O-C gap to Gold, SQLAsset enabled | -0.297297 |
| O-D gap to Gold, SQLAsset disabled | -0.310811 |
| O-D gap to Gold, SQLAsset enabled | -0.310811 |
| SQLAsset gain for O-C | +0.054055 |
| SQLAsset gain for O-D | +0.054054 |
| SQLAsset gain for Gold | +0.054054 |

## Case-level differences

O-C failed while Gold succeeded (SQLAsset disabled): `basic-03`, `basic-09`, `filter-08`, `join-01`, `join-02`, `join-03`, `join-04`, `join-05`, `join-06`, `join-07`, `join-08`, `join-09`, `join-10`, `lifecycle-01`, `lifecycle-02`, `lifecycle-03`, `lifecycle-04`, `lifecycle-05`, `lifecycle-06`, `lifecycle-07`, `lifecycle-08`, `synonym-03`.

O-C failed while Gold succeeded (SQLAsset enabled): `basic-03`, `basic-09`, `filter-08`, `join-01`, `join-02`, `join-03`, `join-04`, `join-05`, `join-06`, `join-07`, `join-08`, `join-09`, `join-10`, `lifecycle-01`, `lifecycle-02`, `lifecycle-03`, `lifecycle-04`, `lifecycle-05`, `lifecycle-06`, `lifecycle-07`, `lifecycle-08`, `synonym-03`.

O-D failed while O-C succeeded (SQLAsset disabled): `filter-05`.

O-D failed while O-C succeeded (SQLAsset enabled): `filter-05`.

## Construction quality and review cost

| Source | Raw Object F1 | Raw Link F1 | Raw Metric F1 | Reviewed Object F1 | Reviewed Link F1 | Reviewed Metric F1 | Review operations | Edited fields | Saving rate | Live LLM calls |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| O-C | 0.857143 | 0.666667 | 0.833333 | 1.000000 | 0.769231 | 0.909091 | 294 | 197 | 0.517241 | 0 |
| O-D | 0.857143 | 0.666667 | 0.833333 | 1.000000 | 0.769231 | 0.909091 | 293 | 196 | 0.518883 | 9 |

## Interpretation boundaries

- This is one completed run per group, so no statistical significance claim is made.
- O-D used live semantic enrichment, but it saved only one estimated review operation relative to O-C and did not improve downstream Result Hash Accuracy in either SQLAsset condition.
- O-C and O-D have nearly identical raw core F1 values, while their reviewed Draft core F1 values are also identical; neither implies Gold-level downstream result accuracy.
- The Reviewed ontologies remain materially below Gold, so the experiment does not support replacing human review.
- Ontology strategy token totals are unavailable because token metadata is not propagated into EvaluationCaseResult; zeros in the CSV must not be interpreted as zero model usage.
- Cases labeled `PROVIDER_ERROR`: T-H/complex-08: Filter concept branch has no unambiguous binding. The recorded reason must be used to distinguish an external HTTP failure from an internal strategy exception.

`sql_asset_selection_accuracy` is `null` because the benchmark does not define a Gold SQLAsset ID per case. Template adoption and result-correct template rewrite are reported separately.
