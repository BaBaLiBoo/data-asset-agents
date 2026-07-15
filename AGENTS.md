# Data Asset Agents contributor guide

- Never add real bank data, internal schemas, internal SQL, credentials, or non-public rules.
- Keep `text2sql.graph.build_text2sql_graph` usable as an independently compiled LangGraph subgraph.
- Resolve natural language to approved business concepts before resolving physical assets.
- Physical mappings and lifecycle policies are deterministic and come from reviewed ontology YAML.
- All generated SQL is read-only, statically validated, explained, and executed in a read-only transaction.
- Add type annotations and tests for changes on the core query path.
- Run `ruff check .` and `pytest` before committing.

