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

As of the report timestamp in this repository, both the V2 mock ablation and a
file-based live O-C/O-D ablation were run at Git SHA
`126a4e1c208b6affae5fbc247fdf6edae4a92f7b`. Live O-D made nine structured calls
per Catalog mode. It did not improve raw F1 and increased edited fields from 214
to 219, so this run provides no evidence that the configured model reduced
review work.

The dependent T-A through T-H 80-case experiment was not run: independently
published reviewed O-C, reviewed O-D, and Gold versions with version-bound
SQLAsset builds were not created. Mock, historical, and file-only construction
results were not substituted for downstream case results.

## PostgreSQL publication closure

The later local PostgreSQL closure supersedes only the last prerequisite
statement above; it does not replace the file-based O-C/O-D ablation. Three
independent versions and READY SQLAsset builds now exist:

| Source | Construction Run | Ontology Version | SQLAsset Build |
|---|---|---|---|
| Reviewed O-C | `construction-0de800c044194393a9317150c2cde05e` | `version_9792e7e2f4fa4db9b27cb26a3c076f2c` | `sqlbuild-8d5ebe518fc14e76a2646e52cc767145` |
| Reviewed live O-D | `construction-24a6784bc1524aae8242c174d829201f` | `version_23aa2974a54a460694a92024b3d0b1a9` | `sqlbuild-551264a4ab16456ebc67fef848cf88ce` |
| Gold | — | `version_3a17072aa48949f185d167da1e1892e8` | `sqlbuild-6c61de9c015a41f8b0a010555738b8de` |

Version IDs, bundle hashes, artifact source hashes, and build IDs are pairwise
distinct. O-C and O-D artifacts passed `STRICT_CONSTRUCTION` with
`seed_accessed=false`, `fallback_used=false`, and
`legacy_ontology_accessed=false`.

The first O-D review retained every model-suggested semantic field allowed by
the structured schema. Strict PostgreSQL Dry Run rejected it because query
resolution treated `relative_days_30` as a date literal. It was not published.
A second, query-safe review retained only model-authored object boundary
descriptions; query-sensitive names and synonyms remained human-reviewed. That
version passed strict validation and was published. This is evidence that the
publication gate worked, not evidence that arbitrary semantic suggestions are
safe.

The formal 640-case runner is:

```powershell
.\.venv\Scripts\python.exe scripts\run_text2sql_reviewed_ontology_v2.py `
  --o-c-version quality-v2-reviewed-o-c `
  --o-c-run construction-0de800c044194393a9317150c2cde05e `
  --o-d-version quality-v2-reviewed-o-d-live-safe `
  --o-d-run construction-24a6784bc1524aae8242c174d829201f `
  --gold-version quality-v2-gold-independent
```

It writes 80 persisted Case rows and one CSV per T-A through T-H group, then
checks ontology/bundle/artifact/build isolation before comparison. The run was
not started because the environment did not grant explicit approval to send
the fictional MiniBank questions, ontology semantics, and retrieval text to
DeepSeek and DashScope. No earlier result was substituted. Exact hashes and the
not-run evidence are in `reports/ontology_construction_v2/published_versions_v2.json`
and `reports/text2sql_reviewed_ontology_v2/manifest.json`.

The isolated Fresh Acceptance passed with 97 construction candidates and review
decisions 12 ACCEPT / 60 MODIFY / 25 REJECT. Existing-Volume Upgrade Acceptance
also passed after applying DDL 009 and 010 twice. A later retry exposed a Docker
Desktop content-store I/O error while reading the locally cached `pgvector`
image; the acceptance script correctly failed immediately and removed its
temporary project. After local disk space was reclaimed and Docker Desktop
recovered, the complete Fresh Acceptance passed again on commit
`d4b1f5b41678f801dd5c84797060c4f1bbd3a28c`: 97 candidates, review decisions
12 ACCEPT / 60 MODIFY / 25 REJECT, strict publication, runtime activation, and
all five local mock smoke groups. Exact successful attempts and the recovered
storage incident are recorded in
`reports/ontology_construction_v2/local_runtime_verification_v2.json`.
