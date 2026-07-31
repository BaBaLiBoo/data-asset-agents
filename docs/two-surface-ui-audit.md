# Two-Surface UI Audit

## Findings

1. Current main navigation had six entries before this closure: `演示首页`, `本体构建与治理`, `智能问数`, `SQL 资产`, `实验与评测`, and `高级管理`.
2. The old `legacy_app.py` still contained Ontology Builder, Ontology Construction Workbench, Ontology Manager, SQLAsset, Evaluation, and low-level debugging pages. The newer `ontology_workbench.py` duplicated the construction, review, Draft, publish, and version-management flow.
3. `demo_home.py`, `sql_assets.py`, `evaluation.py`, and `advanced.py` were product-navigation distractions. They remain backend or hidden engineering capabilities, but they are no longer imported by `apps/web/app.py`.
4. `本体构建` now owns data-source selection, MiniBank data status/initialization, RAW_METADATA snapshot creation, O-C/O-D construction, seven-type candidate review, Promote to Draft, Draft editing, validation, submission, approval, publication, activation, and published version listing.
5. `智能体 Demo` now owns database selection, READY published ontology selection, AI status display, explicit activation, chat-style questions, fixed ontology query mode, SQLAsset-enabled execution, business explanation, result display, SQL, and folded technical details.
6. Hidden old pages: `demo_home.py`, `intelligent_query.py`, `sql_assets.py`, `evaluation.py`, `advanced.py`, and old functions inside `legacy_app.py`. They are not registered in the Streamlit main navigation.
7. Reusable components retained or added: resource forms, evidence renderer, diff renderer, workflow stepper, data-source panel, data-import panel, AI status, ontology selector, query result explanation, construction status, candidate review wrapper, Draft editor wrapper, and publish panel wrapper.
8. Backend capabilities retained: Ontology Construction, Ontology Manager, Draft governance, Artifact, Index, Drift, Diff, Impact, Audit, Object Explorer, SQLAsset, Evaluation, and Text-to-SQL LangGraph.
9. The Streamlit navigation bug was caused by page code calling `set_value(StateKey.PAGE, ...)` after the `st.radio(..., key="daa.page")` navigation widget had already been instantiated. The fix uses `request_navigation(page)` to write `daa.pending_page`; `render_navigation()` applies that pending value before creating the widget.
10. Fresh MiniBank data is prepared by Docker init scripts in normal Compose startup. The new `POST /api/v1/demo/data-initialize` endpoint is a controlled fallback that only runs repository allowlisted `data/ddl/001_schema.sql` and `data/seed/002_seed.sql`; it does not accept SQL, file paths, external sources, or credentials.
11. Published ontology and data source association is exposed to the UI through `GET /api/v1/demo/ontologies`. The current demo has one real source, MiniBank PostgreSQL, and published ontologies are filtered by `PUBLISHED`, `Artifact READY`, and matching data source.
12. Live AI and mock mode are separated by settings. The UI displays provider, model, configured status, and Live/Mock. O-D Live is blocked when `LLM_MODE=live` lacks chat or embedding keys; it does not silently fall back to mock. CI sets mock mode and `DEMO_REQUIRE_LIVE_AI=false`.

## File Modification Plan

- `apps/web/app.py`: register only `本体构建` and `智能体 Demo`.
- `apps/web/navigation.py`: two-entry navigation and pending navigation handling.
- `apps/web/state.py`: centralized two-surface state keys and safe navigation request.
- `apps/web/pages/ontology_builder.py`: new productized ontology construction surface.
- `apps/web/pages/agent_demo.py`: new productized AI demo surface.
- `apps/web/components/*`: product components for data source, data import, AI status, ontology selection, query explanation, construction status, candidate review, Draft editing, publish panel, diff, evidence, resource forms, and stepper.
- `apps/api/main.py`: demo aggregation endpoints and controlled MiniBank data initializer.
- `src/data_asset_agents/core/config.py`, `.env.example`, `docker-compose.yml`: Live AI and developer/demo mode controls.
- `tests/test_web_demo_ui.py`: two-surface UI and API contract tests.
- `scripts/demo_flow_acceptance.ps1`, `scripts/live_ai_smoke.ps1`: mock acceptance and optional local Live AI smoke.
- `README.md`, `docs/two-surface-demo.md`, `docs/ontology-builder-guide.md`, `docs/agent-demo-guide.md`: final demo documentation.
