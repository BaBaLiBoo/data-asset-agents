# T-A through T-H 80-case experiment — Not Run

The three required ontology sources were independently published in PostgreSQL:

- reviewed O-C: `version_9792e7e2f4fa4db9b27cb26a3c076f2c`;
- reviewed live O-D (query-safe review profile):
  `version_23aa2974a54a460694a92024b3d0b1a9`;
- independent Gold: `version_3a17072aa48949f185d167da1e1892e8`.

Their ontology version IDs, bundle hashes, artifact source hashes, and
version-bound READY SQLAsset build IDs are all pairwise distinct. The
construction prerequisite is therefore complete.

The 640-case T-A through T-H experiment is still **not run**. Starting it would
send the fictional MiniBank benchmark questions, reviewed ontology semantics,
and retrieval text to the configured external providers (DeepSeek
`api.deepseek.com` and DashScope `dashscope.aliyuncs.com`). The execution
environment required explicit approval for that outbound payload, and approval
was not granted during this run. No Case rows were created by the formal runner.

No mock, historical, file-only, or earlier four-group benchmark result is
relabeled as V2 evidence.
