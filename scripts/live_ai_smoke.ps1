param(
    [string]$ApiBaseUrl = "http://127.0.0.1:8000"
)

$ErrorActionPreference = "Stop"

function Invoke-Json {
    param(
        [string]$Method,
        [string]$Uri,
        [object]$Body = $null
    )
    $parameters = @{
        Method = $Method
        Uri = $Uri
        TimeoutSec = 180
    }
    if ($null -ne $Body) {
        $parameters["ContentType"] = "application/json"
        $parameters["Body"] = ($Body | ConvertTo-Json -Depth 40)
    }
    Invoke-RestMethod @parameters
}

if ([string]::IsNullOrWhiteSpace($env:LLM_API_KEY)) {
    throw "LLM_API_KEY is not configured. The key value is not printed."
}
if ([string]::IsNullOrWhiteSpace($env:EMBEDDING_API_KEY)) {
    throw "EMBEDDING_API_KEY is not configured. The key value is not printed."
}

$ai = Invoke-Json -Method Get -Uri "$ApiBaseUrl/api/v1/demo/ai-status"
if ($ai.mode -ne "live" -or -not $ai.live_ready) {
    throw "Live AI is not ready. Check LLM_MODE, LLM_API_KEY and EMBEDDING_API_KEY."
}

$query = Invoke-Json -Method Post -Uri "$ApiBaseUrl/api/v1/query" -Body @{
    question = "查询交易金额"
    query_mode = "ontology"
    sql_asset_enabled = $true
}
if ($query.status -ne "success") {
    throw "Live ontology query failed with status $($query.status)"
}
if ($query.raw_model_output -match "mock" -or $query.strategy_variant -match "mock") {
    throw "Live smoke detected a mock response marker"
}

$snapshot = Invoke-Json -Method Post -Uri "$ApiBaseUrl/api/v1/ontology/metadata-snapshots/raw" -Body @{
    schema_name = "public"
    sample_limit = 5
    top_value_limit = 5
}
$run = Invoke-Json -Method Post -Uri "$ApiBaseUrl/api/v1/ontology/construction-runs" -Body @{
    source_snapshot_id = $snapshot.snapshot.id
    catalog_mode = "GOVERNED_CATALOG"
    construction_mode = "STRICT_CONSTRUCTION"
    evidence_mode = "O-D"
    llm_mode = "live"
    provider = $ai.chat.provider
    model = $ai.chat.model
    temperature = 0
    random_seed = 20260715
    created_by = "live-ai-smoke"
}
$run = Invoke-Json -Method Post -Uri "$ApiBaseUrl/api/v1/ontology/construction-runs/$($run.run_id)/generate"
if ($run.llm_mode -ne "live") {
    throw "O-D construction run did not remain in live mode"
}

Write-Host "Live AI Smoke PASSED"
