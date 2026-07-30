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

At that earlier file-based stage, the dependent T-A through T-H experiment was
not run because independent published versions and version-bound SQLAsset
builds did not yet exist. That historical state is superseded by the PostgreSQL
publication and formal downstream closure below; no mock or historical Case was
substituted into the later formal Runs.

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

The formal runner resolved the following exact identities; the latest READY
Gold build shown here supersedes the earlier publication-time build row:

| Source | Artifact source hash | Bundle hash | Formal SQLAsset Build |
|---|---|---|---|
| O-C | `a660a316a9bdb2a26403eee190d4dee6c4118c9af017acf1574ce8abb2609540` | `4544739e14bdebc0ea2e0248153d9387cfd38e5761e992f172819933eb4a0616` | `sqlbuild-8d5ebe518fc14e76a2646e52cc767145` |
| O-D | `09132da1bf5bbbe80a9348d0a73641de7700d7299a9b4f3efab07dea5a9bfdb8` | `024d318c691a5e670621c5364ddadec3471284010838856b9a3ac2bd0fa02987` | `sqlbuild-551264a4ab16456ebc67fef848cf88ce` |
| Gold | `58c0c6dd1daa88e4dc641fdb6c2045e574e66d9dde484bfe7db3278954c51248` | `c5335c771f809e0e591ea2a3c77e3644e844c1420d5992ac07baf4674c8ab219` | `sqlbuild-b25dced77be84fdbab7e4a460c6c78bc` |

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
checks ontology/bundle/artifact/build isolation before comparison. External
processing was explicitly authorized on 2026-07-30. DashScope Embedding
LIVE_PREFLIGHT passed at 1024 dimensions. The first DeepSeek Schema
LIVE_PREFLIGHT returned HTTP 402 and is retained under Run
`827062c3-c4e2-408f-af33-982ec757d2d3`; after the account balance was restored,
the same provider/model passed the balance check and all eight one-Case
strategy preflights.

The formal run then completed all eight groups on Git SHA
`d9ae4ef12968df5cadb4271af7ff6d1bd885290e`: each Run is `COMPLETED`, each has
80 persisted Cases, total coverage is 640, and the fairness comparison has no
warnings. Result Hash Accuracy for T-A through T-H is respectively 0.013514,
0.418919, 0.459459, 0.513514, 0.445946, 0.500000, 0.756757, and 0.810811.
Exact non-secret preflight and formal evidence is in
`reports/text2sql_reviewed_ontology_v2/preflight.json` and
`reports/text2sql_reviewed_ontology_v2/manifest.json`.

O-C is 0.297298 below Gold without SQLAsset and 0.297297 below Gold with it;
O-D is 0.310811 below Gold in both conditions. SQLAsset adds about 0.054054 for
O-C, O-D, and Gold. O-D made nine Live LLM construction calls but reduced the
review estimate by only one operation and scored about 0.0135 below O-C. This
single run does not support a statistical-significance claim, a general
LLM-driven review-cost reduction, or replacement of human review. The 22 Cases
where O-C failed while Gold succeeded and the single Case where O-D failed
while O-C succeeded are listed in `error-analysis.md` and
`construction-query-correlation.json`.

The runner writes each Case CSV and its progress manifest atomically. Rerunning
with the same output directory (or an explicit `--resume-manifest`) reuses only
a complete, provenance-matching 80-Case group. Failed attempts remain recorded;
individual failed Cases are never selectively rerun or combined.

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
