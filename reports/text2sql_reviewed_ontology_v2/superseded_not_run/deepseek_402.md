# T-A through T-H 80-case experiment — Not Run

The three required ontology sources were independently published in PostgreSQL:

- reviewed O-C: `version_9792e7e2f4fa4db9b27cb26a3c076f2c`;
- reviewed live O-D (query-safe review profile):
  `version_23aa2974a54a460694a92024b3d0b1a9`;
- independent Gold: `version_3a17072aa48949f185d167da1e1892e8`.

Their ontology version IDs, bundle hashes, artifact source hashes, and
version-bound READY SQLAsset build IDs are all pairwise distinct. The
construction prerequisite is therefore complete.

External processing of the public, fictional MiniBank inputs was explicitly
authorized on 2026-07-30. DashScope Embedding succeeded and returned the
configured 1024 dimensions.

The first DeepSeek Schema LIVE_PREFLIGHT request reached the configured service
but returned HTTP 402 `Insufficient Balance`. The persisted preflight Run is
`827062c3-c4e2-408f-af33-982ec757d2d3`; its single Case is retained as
`PROVIDER_ERROR`. Because all eight strategies require the same configured chat
provider, the formal 640-case experiment was not started.

Historical status at the time: completed formal groups 0 of 8; persisted
formal Cases 0 of 640. This record is `SUPERSEDED_BY_COMPLETED_RUN`.

No mock, historical, file-only, or earlier four-group benchmark result is
relabeled as V2 evidence.

The earlier authorization-based not-run explanation is retained under
`superseded_not_run/authorization_not_granted.md`. Exact non-secret preflight
evidence is in `preflight.json`.
