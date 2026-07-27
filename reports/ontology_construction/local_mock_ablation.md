# Ontology Construction Local Mock Ablation

Generated deterministically from the public MiniBank DDL, fictional seed data, and certified historical SQL. Gold is loaded only after candidate generation to simulate human review and independent scoring.

| Run | Catalog | Object F1 | Dimension F1 | Metric F1 | Accept | Modify | Reject |
|---|---|---:|---:|---:|---:|---:|---:|
| mock-O-A-raw_metadata | RAW_METADATA | 1.000 | 1.000 | 0.000 | 0.000 | 0.543 | 0.457 |
| mock-O-B-raw_metadata | RAW_METADATA | 1.000 | 1.000 | 0.000 | 0.000 | 0.543 | 0.457 |
| mock-O-C-raw_metadata | RAW_METADATA | 1.000 | 1.000 | 0.909 | 0.000 | 0.561 | 0.439 |
| mock-O-D-raw_metadata | RAW_METADATA | 1.000 | 1.000 | 0.909 | 0.000 | 0.561 | 0.439 |
| mock-O-C-governed_catalog | GOVERNED_CATALOG | 1.000 | 1.000 | 0.909 | 0.000 | 0.561 | 0.439 |

## Interpretation

- O-A measures the schema-only object, property, and binding scaffold.
- O-B adds profiling; profiling does not invent business metrics.
- O-C may derive aggregation, fixed filters, time properties, and supported dimensions only from certified SQL AST evidence.
- O-D uses mock semantic enrichment and makes no live-LLM gain claim.
- Object boundaries, business Link names, conflicts, and metric definitions still require human review.

## Provenance and validation

- Git SHA: `7c0ca25d6bad833882608012e0708d367c034e53`
- Database snapshot hash: `f8f9a4f295e290e4764daa7a3babca446470f9b153aa87a88ce6b9d12303204f`
- Metadata snapshot hash: `cd95ff2d44028de87bc99cb4dd88d9a042f58f0f84491eec8f59d7f2856055bd`
- Historical SQL hash: `74b8ec78b01f2b3f6901a488b0fc95e1f82250bc95ad60979d41f03e6f5d374c`
- Gold hash: `80dd1d48d4b3361d15e579a08081501b10adbe8c5c636ca83c0d096131b3a88f`
- `strict_validation_passed` in this report means deterministic strict compilation completed without conflicts or seed/fallback/legacy access. It is not a PostgreSQL EXPLAIN or live-model result.
- Publication and runtime activation are intentionally false in this file-only ablation; those gates are covered by Docker acceptance.
