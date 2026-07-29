# Ontology Construction

## Entry points and trust boundaries

Reviewed YAML is the Gold ontology, demonstration seed, and compatibility entry
point. It is not the recommended automatic-construction input. The recommended
path starts from a snapshot of the configured PostgreSQL source:

1. Capture a RAW_METADATA snapshot with tables, columns, primary keys, foreign
   keys, bounded/masked profiling, and a content hash.
2. Create an `OntologyConstructionRun`.
3. Generate isolated candidates using O-A, O-B, O-C, or O-D evidence.
4. Have a person ACCEPT, MODIFY, REJECT, MERGE, or DEFER every candidate.
5. Promote only a fully reviewed run to an `OntologyDraft`.
6. Validate, submit, approve, publish, and activate the Draft.

Candidate and rejected resources never enter query-time resolution. Stable
resource IDs cannot be changed during review.

## Catalog and evidence modes

`RAW_METADATA` may read the captured database metadata and the evidence explicitly
enabled for the run. It cannot read governed-catalog answers or Gold. In
`GOVERNED_CATALOG`, the generator may additionally read the independent governed
asset catalog.

- O-A: schema only; it must not create Metrics from historical SQL.
- O-B: schema plus profiling; profiling may suggest enum/sensitivity semantics,
  but cannot invent a business Metric.
- O-C: schema, profiling, and certified historical SQL. Metric aggregation,
  fixed filters, time properties, supported dimensions, and evidence hashes must
  be traceable to certified SQL AST evidence.
- O-D: all evidence plus semantic enrichment. In `LLM_MODE=mock` this is mock
  deterministic enrichment and makes no live-model gain claim. In live mode,
  Pydantic Structured Output can suggest display names, descriptions, synonyms,
  and business Link wording only. Stable IDs, bindings, primary keys, Physical
  Joins, Metric aggregation/filters/dimensions, lifecycle, and publication state
  remain deterministic.

Uncertified SQL and incidental WHERE predicates cannot become high-confidence
fixed business definitions. Business Links and Physical Joins remain distinct.
Every candidate evidence item records source type, source snapshot ID, extracted
fact, and evidence hash.

A Physical Join proves how two approved tables can be connected; it does not
prove that a user-facing relationship should exist. Construction creates a
business Link from a declared foreign key, or from a certified FK-shaped
event-to-entity SQL join. Object role and a deterministic vocabulary establish
direction, cardinality, stable ID, and inverse role. Repeated evidence for the
same Link is merged, while physical joins remain separately reviewable.

## Strict construction

A Draft with `construction_run_id` always resolves to `STRICT_CONSTRUCTION`.
Validate, review-time revalidation, publish, and Dry Run use the same resolver.
The compiler cannot access reviewed YAML, legacy Metrics/Dimensions,
legacy PhysicalMappings/Joins, or the runtime fallback ontology.

Validation and READY artifact provenance must record:

```text
construction_mode=STRICT_CONSTRUCTION
seed_accessed=false
fallback_used=false
legacy_ontology_accessed=false
```

Removing a required construction Metric must make validate, approve, and publish
fail even if the fallback bundle contains a matching Metric.

## Local PowerShell workflow

Use Python 3.11 or 3.12. Development tools stay in the host virtual environment,
not the production image.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q
```

Fresh isolated database:

```powershell
$env:GIT_COMMIT_SHA = (git rev-parse HEAD).Trim()
.\scripts\acceptance.ps1 -TimeoutSeconds 600 `
  -ComposeProject "data-asset-agents-local-closure"
```

This uses a new Compose project and Volume, runs DDL 001 through 010, and removes
only that isolated Volume when finished. Its default host ports are PostgreSQL
`15432`, API `18000`, and Streamlit `18501`, so an ordinary development stack
can remain on `5432`, `8000`, and `8501`. Override them with
`-PostgresPort`, `-ApiPort`, and `-WebPort` when needed.

Existing Volume upgrade:

```powershell
$env:GIT_COMMIT_SHA = (git rev-parse HEAD).Trim()
.\scripts\upgrade_acceptance.ps1 -TimeoutSeconds 300 `
  -ComposeProject "data-asset-agents-upgrade-010"
```

The upgrade acceptance creates a disposable pre-009 database, preserves fictional
legacy rows, applies allowlisted DDL 009 and 010 twice, verifies new tables and
columns, and starts the current API. It defaults to PostgreSQL `25432` and API
`28000`; normal user containers and Volumes are not stopped or deleted.

Interactive services:

```powershell
docker compose up --build -d
```

- API: <http://localhost:8000>
- Swagger: <http://localhost:8000/docs>
- Streamlit: <http://localhost:8501>

The Streamlit workbench supports run creation, catalog/evidence and mock/live
O-D selection, immutable raw-quality and Link/Metric diagnostics, per-resource
review progress, raw-to-reviewed JSON Diff, candidate evidence expansion, all
review decisions, and promotion only after all candidates have a terminal review
decision. Review delta/cost and the final version association are shown after
evaluation. API errors are displayed on the page.

## Live semantic review boundary

Structured output prevents the model from changing stable IDs, bindings,
Physical Joins, aggregation, fixed filters, and supported dimensions. It does
not by itself prove that every allowed name or synonym is query-safe. In the
local Quality V2 closure, retaining all allowed O-D semantic suggestions caused
strict Dry Run to reject a relative-time synonym as an invalid date literal.
That Draft was not published.

The published O-D comparison version therefore uses an explicit query-safe
test-review policy: only object boundary descriptions retain live-model text;
all query-sensitive names, synonyms, and structural/physical fields use the
human-reviewed result. Reports label this policy and do not claim that live O-D
reduced review effort. Production reviewers must inspect semantic changes just
as they inspect structural changes.
