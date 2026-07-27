# O-C versus Live O-D Construction Ablation V2

| Run | Catalog | Raw Object F1 | Endpoint Link F1 | Directed Link F1 | Semantic Link F1 | Raw Metric F1 | Edited Fields | Saving Rate | LLM Calls |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| live-O-C-raw_metadata | RAW_METADATA | 0.8571428571428571 | 0.6666666666666666 | 0.6666666666666666 | 0.0 | 0.8333333333333334 | 214 | 0.4893267651888341 | 0 |
| live-O-D-raw_metadata | RAW_METADATA | 0.8571428571428571 | 0.6666666666666666 | 0.6666666666666666 | 0.0 | 0.8333333333333334 | 219 | 0.4811165845648604 | 9 |
| live-O-C-governed_catalog | GOVERNED_CATALOG | 0.8571428571428571 | 0.6666666666666666 | 0.6666666666666666 | 0.0 | 0.8333333333333334 | 214 | 0.4893267651888341 | 0 |
| live-O-D-governed_catalog | GOVERNED_CATALOG | 0.8571428571428571 | 0.6666666666666666 | 0.6666666666666666 | 0.0 | 0.8333333333333334 | 219 | 0.4811165845648604 | 9 |

O-C uses no semantic model. O-D uses the same physical evidence plus constrained live semantic suggestions. These file-only runs do not claim publication, runtime activation, or downstream 80-case results.
