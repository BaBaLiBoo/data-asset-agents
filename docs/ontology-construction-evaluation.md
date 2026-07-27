# Ontology Construction Quality Evaluation V2

## Two independent stages

Construction quality is reported at two stages:

1. `RAW_CANDIDATE` scores the immutable generated resources in
   `ConstructionCandidate.original_candidate`.
2. `REVIEWED_DRAFT` scores resources after explicit review and promotion.

The response schema is version `2.0` and contains
`raw_candidate_metrics`, `reviewed_draft_metrics`, `review_delta`,
`review_cost`, `error_analysis`, and `provenance`. The legacy `metrics` field
remains available for older clients and contains reviewed metrics plus review
cost. A reviewed F1 of 1.0 must never be described as automatic-candidate F1.

Gold is loaded only after generation. The raw evaluation endpoint uses Gold as a
read-only comparison target and saves the resulting raw report before a test
reviewer may use Gold to simulate human decisions. Gold is never passed to the
candidate generator, RAW_METADATA classifier, or compiler, and is never written
into `original_candidate`.

```text
GET  /api/v1/ontology/construction-runs/{run_id}/evaluation/raw
POST /api/v1/ontology/construction-runs/{run_id}/evaluate
GET  /api/v1/ontology/construction-runs/{run_id}/evaluation/reviewed
```

## Empty-set semantics

Precision and recall with an empty denominator are undefined and serialize as
`null`. F1 is `0` when one side is non-empty and there is no match; all three
values are `null` when both sets are empty. Field accuracy is `null` when there
is no resource with a common stable ID. CSV leaves these values empty and
Markdown renders them as `—`; an empty comparison is never shown as 100%.

## Link diagnostics

Business Link and Physical Join are evaluated separately. Link evaluation
reports:

- `link_endpoint_pair_f1`, ignoring direction;
- `directed_link_f1`, including source and target direction;
- `semantic_link_f1`, including stable ID and business name;
- `physical_link_consistency`, requiring referenced Physical Join evidence;
- cardinality, stable-ID, business-name, and inverse-role accuracy.

Physical evidence is not automatically promoted to a business Link. A declared
foreign key is sufficient. Certified SQL may support a Link only for an
FK-shaped event-to-entity join whose columns identify the referenced object.
Repeated evidence for the same stable Link is merged. The fictional Gold has a
narrower relationship scope than the source schema, so extra but structurally
supported Link candidates remain visible as errors for human review.

## Review cost

The report counts decisions, edited fields, resource-specific edits and
rejections, and structural, semantic, and physical-mapping changes. Estimated
review operations are:

```text
review decisions + edited fields + merge operations
```

The manual-from-zero baseline is:

```text
resource create operations + populated Gold fields
```

The engineering estimate is:

```text
review_saving_rate =
  1 - estimated_review_operations / manual_creation_operations
```

It is not measured human time. If measured review duration becomes available,
it is reported separately as `optional_review_duration_seconds`.

## Reproduce the mock ablation

```powershell
.\.venv\Scripts\python.exe scripts\run_ontology_construction_mock.py
```

The script runs O-A through O-D independently for RAW_METADATA and
GOVERNED_CATALOG and writes:

- `reports/ontology_construction_v2/raw_candidate_metrics.json`
- `reports/ontology_construction_v2/reviewed_draft_metrics.json`
- `reports/ontology_construction_v2/review_cost.json`
- `reports/ontology_construction_v2/review_delta.json`
- `reports/ontology_construction_v2/link_error_analysis.json`
- `reports/ontology_construction_v2/mock_ablation_v2.csv`
- `reports/ontology_construction_v2/mock_ablation_v2.md`

O-D in these files is mock semantic enrichment. It does not represent a live
model comparison. File-only strict validation means compilation had no conflict
and no seed/fallback/legacy access; it does not claim PostgreSQL EXPLAIN,
publication, or runtime activation.

## Live O-D and downstream benchmark

Live O-D uses the same physical evidence as O-C. Structured model output may
suggest names, descriptions, synonyms, and Link wording, but cannot change IDs,
tables, columns, primary keys, joins, aggregation, filters, dimensions,
lifecycle, or publish state. Each successful call records provider, model,
temperature, token limit, prompt version, input/output hashes, random seed,
retry count, latency, token usage, and invalid-output count.

The formal downstream design is T-A through T-H: Schema, Physical RAG, reviewed
O-C with SQLAsset off/on, reviewed O-D with SQLAsset off/on, and Gold with
SQLAsset off/on. All groups require fixed provider settings and distinct O-C,
O-D, and Gold ontology versions and SQLAsset builds.

As of the report timestamp in this repository, the V2 mock ablation was run.
Live O-D and the dependent T-A through T-H 80-case experiment must be marked
`not run` unless a usable live API key and all three isolated published versions
are present. Mock results must not substitute for them.
