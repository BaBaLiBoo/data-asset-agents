# Data Asset Agents contributor guide

- Never add real bank data, internal schemas, internal SQL, credentials, or non-public rules.
- Keep `text2sql.graph.build_text2sql_graph` usable as an independently compiled LangGraph subgraph.
- Resolve natural language to approved business concepts before resolving physical assets.
- Physical mappings and lifecycle policies are deterministic and come from the current published
  ontology version; reviewed YAML is the initialization seed and runtime fallback.
- LLM-generated ontology candidates must remain isolated until a human moves them to VERIFIED and
  explicitly publishes a new version. CANDIDATE and REJECTED records are never query-time inputs.
- Object-first ontology changes must be made in an OntologyDraft. Only a VALIDATED Draft may be
  atomically published; DRAFT, IN_REVIEW, and REJECTED object resources are never runtime inputs.
- New object models default to the allowlisted direct object seed or a blank Draft. Metadata and
  historical SQL may only produce review candidates. LegacyOntologyObjectMigrator is a
  compatibility import tool and must not become a default generator, UI path, or acceptance path.
- Keep business LinkType separate from PhysicalJoinDefinition. A Link may only reference reviewed,
  enabled joins connecting the actual bound object tables.
- DataSourceDefinition stores an environment-variable name such as DATABASE_URL, never a resolved
  URL or secret. The first manager release may inspect only the application-managed PostgreSQL.
- Published object resources are authoritative for Metric/Dimension physical compilation. YAML is
  fallback only; conflicting property references and legacy physical contracts must block publish.
- Versioned PhysicalJoinDefinition resources, Draft Diff/Impact, breaking-change acknowledgement,
  metadata drift, and dynamic Dry Run are publication gates and must not be bypassed.
- Object Explorer may issue only bounded, parameterized, allowlisted read-only SELECT statements
  from current published bindings; sensitive properties must remain masked.
- Metadata profiling accepts only allowlisted identifiers and stores bounded, masked samples.
- All generated SQL is read-only, statically validated, explained, and executed in a read-only transaction.
- Historical SQL assets are retrieval templates only. Uncertified, lifecycle-invalid, parse-invalid,
  missing-column, unreviewed-join, or EXPLAIN-failed assets must never enter online recall.
- SQL assets must belong to the current ontology version and the latest READY SQLAssetBuild;
  BUILDING and FAILED batches must remain invisible while the previous READY batch serves traffic.
- Required metric filters, aggregation roles, time dimensions, supported dimensions, and mapping
  provenance are hard semantic gates, not ranking signals.
- SQL template adaptation must use SQLGlot AST mutations and must fall back to the deterministic
  compiler unless the rewritten SQL passes SQLValidator and PostgreSQL EXPLAIN.
- Add type annotations and tests for changes on the core query path.
- Run `ruff check .` and `pytest` before committing.
