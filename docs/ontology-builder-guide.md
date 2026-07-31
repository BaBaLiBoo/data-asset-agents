# Ontology Builder Guide

`本体构建` is the product page for turning MiniBank PostgreSQL metadata into a governed ontology release.

## Six Steps

1. `选择数据源`: the page reads data sources from the API. The current demo exposes only the real MiniBank PostgreSQL source.
2. `准备数据`: the page checks whether the fictional MiniBank tables and seed rows exist. The initializer is controlled and idempotent.
3. `自动构建`: O-C uses metadata, profiling, and certified historical SQL. O-D adds Live AI semantic suggestions when keys and embedding are configured.
4. `人工审核`: candidates are reviewed with structured forms. Stable IDs are read-only; reject requires a reason; merge requires a target; defer is not complete.
5. `草稿完善`: reviewed candidates are promoted into an OntologyDraft. Draft resources can be added, edited, or deleted with If-Match revision protection.
6. `发布本体`: Validate, Submit, Approve, and Publish use the existing governance APIs. Activation is explicit.

## Seven Reviewed Resource Types

- ObjectType
- Property
- Metric
- Dimension
- Business Link
- Physical Join
- Binding

Business Link describes the business relationship. Physical Join describes the physical field connection. They are separate review resources.

## Draft Safety

Every Draft write carries `If-Match`. A revision conflict tells the user to reload the latest Draft and does not silently overwrite form input.

## Developer Detail

Raw JSON, low-level run fields, validation evidence, audit events, SQLAsset status, and evaluation details are available through API, scripts, tests, and developer-mode expanders. They are not separate Streamlit product pages.
