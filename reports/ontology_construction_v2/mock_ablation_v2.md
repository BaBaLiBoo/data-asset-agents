# Ontology Construction Quality Evaluation V2 — Mock Ablation

Generated from the public fictional MiniBank DDL, seed rows, and certified historical SQL. Raw scores are computed from immutable generated candidates before the Gold-backed test reviewer changes any resource.

| Run | Raw Object F1 | Raw Endpoint Link F1 | Raw Directed Link F1 | Raw Semantic Link F1 | Raw Metric F1 | Reviewed F1 | Accept | Modify | Reject | Edit Fields | Saving Rate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| mock-O-A-raw_metadata | 0.857 | 0.667 | 0.667 | 0.000 | 0.000 | 0.857 | 0.000 | 0.604 | 0.396 | 191 | 0.537 |
| mock-O-B-raw_metadata | 0.857 | 0.667 | 0.667 | 0.000 | 0.000 | 0.857 | 0.000 | 0.604 | 0.396 | 191 | 0.537 |
| mock-O-C-raw_metadata | 0.857 | 0.667 | 0.667 | 0.000 | 0.833 | 0.987 | 0.000 | 0.619 | 0.381 | 214 | 0.489 |
| mock-O-D-raw_metadata | 0.857 | 0.667 | 0.667 | 0.000 | 0.833 | 0.987 | 0.000 | 0.619 | 0.381 | 214 | 0.489 |
| mock-O-A-governed_catalog | 0.857 | 0.667 | 0.667 | 0.000 | 0.000 | 0.857 | 0.000 | 0.604 | 0.396 | 191 | 0.537 |
| mock-O-B-governed_catalog | 0.857 | 0.667 | 0.667 | 0.000 | 0.000 | 0.857 | 0.000 | 0.604 | 0.396 | 191 | 0.537 |
| mock-O-C-governed_catalog | 0.857 | 0.667 | 0.667 | 0.000 | 0.833 | 0.987 | 0.000 | 0.619 | 0.381 | 214 | 0.489 |
| mock-O-D-governed_catalog | 0.857 | 0.667 | 0.667 | 0.000 | 0.833 | 0.987 | 0.000 | 0.619 | 0.381 | 214 | 0.489 |

## Correct interpretation

- Raw candidate F1 measures automation quality. Reviewed Draft F1 measures the result after a Gold-backed test reviewer; it is not automation F1.
- `null` in JSON, an empty CSV cell, and `—` here mean not applicable. An empty comparison is never reported as 100%.
- Review saving rate is an engineering operation estimate, not human time. It counts decisions and changed fields against resource creation and populated Gold fields.
- O-D is mock semantic enrichment here and makes no live-model gain claim.
- File-only mock runs do not publish or activate versions. Docker acceptance covers strict publication and runtime activation.

## Provenance

- Git SHA: `126a4e1c208b6affae5fbc247fdf6edae4a92f7b`
- Database snapshot hash: `f8f9a4f295e290e4764daa7a3babca446470f9b153aa87a88ce6b9d12303204f`
- Metadata snapshot hash: `b4bea6fcf9a1b99927af6804247ab6efe6a92946d486048dfec47ec3ceda6bcb`
- Historical SQL hash: `74b8ec78b01f2b3f6901a488b0fc95e1f82250bc95ad60979d41f03e6f5d374c`
- Gold hash: `80dd1d48d4b3361d15e579a08081501b10adbe8c5c636ca83c0d096131b3a88f`
- Catalog mode and evidence mode are independent. Both RAW_METADATA and GOVERNED_CATALOG run across O-A through O-D.
