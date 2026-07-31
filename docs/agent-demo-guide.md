# Agent Demo Guide

`智能体 Demo` is the product page for ontology-grounded natural-language data questions.

## Runtime Configuration

1. Select a database from `GET /api/v1/demo/data-sources`.
2. Select a published ontology from `GET /api/v1/demo/ontologies`.
3. Confirm AI status from `GET /api/v1/demo/ai-status`.

Only published ontologies with READY artifacts and a matching MiniBank data source are shown.

This is a single-user demo runtime. Activating a selected ontology updates the global online ontology version, then reloads OntologyService, QueryExecutor, validator, ontology index, SQLAsset service, LangGraph, and evaluation inspector through the existing activation path.

## Query Behavior

The page always sends:

```json
{
  "query_mode": "ontology",
  "sql_asset_enabled": true
}
```

Schema and RAG comparison modes remain in evaluation code and are not product controls.

## Answer Layout

Each answer shows:

- Business answer
- Result table and row count
- Why this query was used
- Business metrics
- Dimensions
- Time range
- Filters
- Ontology concepts
- Physical tables and columns
- Join plan
- SQLAsset hit
- Generated SQL
- Validation status

Raw model output, LangGraph trace, provider metadata, token usage, and full API response stay folded under technical details.

## Live AI Boundary

Live mode uses environment variables only. The UI displays configured=true/false, provider, and model, but never shows keys. Missing key, authentication failure, rate limit, or timeout must be displayed as a Live configuration/runtime issue, not silently converted to mock.
