param(
    [Parameter(Mandatory = $true)]
    [string]$ComposeProject,
    [Parameter(Mandatory = $true)]
    [string]$GoldVersion,
    [string]$BaseUrl = "http://localhost:8000",
    [int]$TimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"

function Invoke-JsonPost {
    param([string]$Path, [object]$Payload, [hashtable]$Headers = @{})
    Invoke-RestMethod -Method Post -Uri "$BaseUrl$Path" `
        -ContentType "application/json; charset=utf-8" `
        -Headers $Headers -Body ($Payload | ConvertTo-Json -Depth 40) `
        -TimeoutSec 120
}

function Get-ResultHash([object]$ExecutionResult) {
    $payload = $ExecutionResult.rows | ConvertTo-Json -Depth 30 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString(
            $algorithm.ComputeHash($bytes)
        )).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

Write-Host "[construction] Capturing a real RAW_METADATA snapshot..."
$build = Invoke-JsonPost "/api/v1/ontology/metadata-snapshots/raw" @{
    schema_name = "public"
    sample_limit = 5
    top_value_limit = 5
}
$snapshot = $build.snapshot
if ([string]::IsNullOrWhiteSpace($snapshot.id) -or
    @($snapshot.tables).Count -le 0 -or
    @($snapshot.tables.columns).Count -le 0 -or
    @($snapshot.tables.profiles).Count -le 0 -or
    @($snapshot.tables | Where-Object { @($_.primary_key).Count -gt 0 }).Count -le 0 -or
    @($snapshot.tables | Where-Object { @($_.foreign_keys).Count -gt 0 }).Count -le 0) {
    throw "RAW_METADATA snapshot lacks tables, columns, keys, profiles, or snapshot ID"
}

$run = Invoke-JsonPost "/api/v1/ontology/construction-runs" @{
    source_snapshot_id = $snapshot.id
    catalog_mode = "RAW_METADATA"
    construction_mode = "STRICT_CONSTRUCTION"
    evidence_mode = "O-C"
    llm_mode = "mock"
    random_seed = 20260727
    created_by = "local-acceptance"
}
$generated = Invoke-JsonPost `
    "/api/v1/ontology/construction-runs/$($run.run_id)/generate" @{}
if ($generated.status -ne "CANDIDATES_READY") {
    throw "ConstructionRun did not reach CANDIDATES_READY"
}
foreach ($kind in @(
    "object_type", "property", "binding", "link_type", "physical_join",
    "dimension", "metric"
)) {
    if ([int]$generated.candidate_counts.$kind -le 0) {
        throw "ConstructionRun generated no $kind candidates"
    }
}
foreach ($table in @(
    "legacy_card_transaction", "tmp_transaction_result", "test_transaction_copy",
    "dws_branch_transaction_day"
)) {
    if (-not $generated.excluded_tables.PSObject.Properties.Name.Contains($table)) {
        throw "ConstructionRun did not exclude $table"
    }
}

$candidates = Invoke-RestMethod `
    -Uri "$BaseUrl/api/v1/ontology/construction-runs/$($run.run_id)/candidates" `
    -TimeoutSec 60
$metricCandidates = @($candidates | Where-Object { $_.resource_type -eq "metric" })
foreach ($candidate in $metricCandidates) {
    $metric = $candidate.current_resource
    if ([string]::IsNullOrWhiteSpace($metric.measure_property_id) -or
        [string]::IsNullOrWhiteSpace($metric.aggregation) -or
        $null -eq $metric.filter_predicates -or
        $null -eq $metric.supported_dimension_ids -or
        @($candidate.evidence).Count -le 0 -or
        @($candidate.evidence | Where-Object {
            [string]::IsNullOrWhiteSpace($_.source_type) -or
            [string]::IsNullOrWhiteSpace($_.source_snapshot_id) -or
            [string]::IsNullOrWhiteSpace($_.extracted_fact) -or
            [string]::IsNullOrWhiteSpace($_.evidence_hash)
        }).Count -gt 0) {
        throw "O-C Metric candidate lacks semantic fields or hashed evidence"
    }
}

Write-Host "[construction] Applying the post-generation Gold test reviewer..."
$reviewOutput = docker compose -p $ComposeProject exec -T api `
    python scripts/review_ontology_construction_candidates.py `
    --base-url "http://localhost:8000" --run-id $run.run_id
if ($LASTEXITCODE -ne 0) {
    throw "Post-generation Gold test reviewer failed"
}
$review = ($reviewOutput | Select-Object -Last 1) | ConvertFrom-Json
if (-not $review.gold_loaded_after_generation -or
    -not $review.raw_evaluated_before_gold_review -or
    $review.reviewer_kind -ne "POST_GENERATION_GOLD_TEST_REVIEWER" -or
    [int]$review.decisions.ACCEPT -le 0 -or
    [int]$review.decisions.MODIFY -le 0 -or
    [int]$review.decisions.REJECT -le 0) {
    throw "Test reviewer provenance or decision coverage is incomplete"
}

$pending = Invoke-RestMethod `
    -Uri ("$BaseUrl/api/v1/ontology/construction-runs/$($run.run_id)" +
        "/candidates?review_status=PENDING") -TimeoutSec 30
if (@($pending).Count -ne 0) {
    throw "ConstructionRun still has unreviewed candidates"
}
$reviewCountSql = (
    "SELECT count(*) FROM ontology_construction_candidate_review " +
    "WHERE run_id='$($run.run_id)'"
)
$persistedReviewCount = docker compose -p $ComposeProject exec -T postgres `
    psql -U minibank -d minibank -At -c $reviewCountSql
if ($LASTEXITCODE -ne 0 -or
    [int]$persistedReviewCount.Trim() -ne @($candidates).Count) {
    throw "Candidate review decisions were not persisted in PostgreSQL"
}

$promoted = Invoke-JsonPost `
    "/api/v1/ontology/construction-runs/$($run.run_id)/promote-to-draft" @{
    draft_name = "Local PostgreSQL construction acceptance"
    actor = "local-acceptance-test-reviewer"
}
if ($promoted.draft.construction_run_id -ne $run.run_id) {
    throw "Promoted Draft lost construction_run_id"
}
foreach ($collection in @(
    "object_types", "properties", "bindings", "link_types", "physical_joins",
    "dimensions", "metrics"
)) {
    if (@($promoted.resources.$collection).Count -le 0) {
        throw "Promoted Draft has no $collection"
    }
}

$draftHeaders = @{
    "If-Match" = '"' + $promoted.draft.resource_revision + '"'
    "X-Actor" = "local-acceptance-validator"
}
$validated = Invoke-JsonPost `
    "/api/v1/ontology/drafts/$($promoted.draft.id)/validate" @{} $draftHeaders
$report = $validated.draft.validation_report
if (-not $report.valid -or
    $report.construction_mode -ne "STRICT_CONSTRUCTION" -or
    $report.seed_accessed -or $report.fallback_used -or
    $report.legacy_ontology_accessed -or
    -not $report.explain_passed -or
    @($report.dry_run_cases).Count -ne 4 -or
    @($report.dry_run_cases | Where-Object {
        -not $_.sqlglot_valid -or -not $_.ontology_policy_valid -or
        -not $_.explain_passed -or @($_.errors).Count -gt 0
    }).Count -gt 0) {
    throw "Strict Construction Draft validation or Dry Run failed"
}

$null = Invoke-JsonPost `
    "/api/v1/ontology/drafts/$($promoted.draft.id)/submit" `
    @{ actor = "local-acceptance-author" } $draftHeaders
$approved = Invoke-JsonPost `
    "/api/v1/ontology/drafts/$($promoted.draft.id)/approve" `
    @{ actor = "local-acceptance-reviewer" } $draftHeaders
$reviewReport = $approved.draft.validation_report
if ($reviewReport.construction_mode -ne "STRICT_CONSTRUCTION" -or
    $reviewReport.seed_accessed -or $reviewReport.fallback_used -or
    $reviewReport.legacy_ontology_accessed) {
    throw "Approve replaced the strict validation report"
}

$versionName = "construction-local-" + [guid]::NewGuid().ToString("N").Substring(0, 12)
$version = Invoke-JsonPost `
    "/api/v1/ontology/drafts/$($promoted.draft.id)/publish" @{
    actor = "local-acceptance-reviewer"
    version = $versionName
    description = "Local PostgreSQL strict construction acceptance"
    acknowledge_breaking_changes = $true
    change_ticket = "LOCAL-ACCEPTANCE"
} $draftHeaders
$artifact = Invoke-RestMethod `
    -Uri "$BaseUrl/api/v1/ontology/versions/$($version.id)/compiled-artifact" `
    -TimeoutSec 30
if ($artifact.status -ne "READY" -or
    $artifact.construction_run_id -ne $run.run_id -or
    $artifact.source_resource_hash -ne $approved.draft.resource_hash -or
    [string]::IsNullOrWhiteSpace($artifact.bundle_hash) -or
    $artifact.construction_mode -ne "STRICT_CONSTRUCTION" -or
    $artifact.seed_accessed -or $artifact.fallback_used -or
    $artifact.legacy_ontology_accessed -or
    @($artifact.compilation_evidence.metrics).Count -le 0 -or
    @($artifact.compilation_evidence.dimensions).Count -le 0 -or
    @($artifact.compilation_evidence.links).Count -le 0) {
    throw "Compiled Artifact lacks strict provenance or compilation evidence"
}

$persistenceSql = (
    "SELECT " +
    "(SELECT count(*) FROM ontology_construction_run WHERE run_id='$($run.run_id)')," +
    "(SELECT count(*) FROM ontology_construction_candidate WHERE run_id='$($run.run_id)')," +
    "(SELECT count(*) FROM ontology_construction_candidate_review WHERE run_id='$($run.run_id)')," +
    "(SELECT count(*) FROM ontology_draft WHERE draft_id='$($promoted.draft.id)' " +
    "AND construction_run_id='$($run.run_id)')," +
    "(SELECT count(*) FROM ontology_compiled_artifact WHERE ontology_version_id=" +
    "'$($version.id)' AND construction_run_id='$($run.run_id)' " +
    "AND construction_evidence_summary <> '{}'::jsonb)"
)
$persistence = docker compose -p $ComposeProject exec -T postgres `
    psql -U minibank -d minibank -At -F "|" -c $persistenceSql
if ($LASTEXITCODE -ne 0) { throw "PostgreSQL construction persistence query failed" }
$parts = $persistence.Trim().Split("|")
if ($parts.Count -ne 5 -or
    [int]$parts[0] -ne 1 -or
    [int]$parts[1] -ne @($candidates).Count -or
    [int]$parts[2] -ne @($candidates).Count -or
    [int]$parts[3] -ne 1 -or [int]$parts[4] -ne 1) {
    throw "Construction run, candidates, reviews, Draft, or Artifact are not durable"
}

Write-Host "[construction] Restarting API and reloading the strict Artifact..."
docker compose -p $ComposeProject restart api | Out-Null
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
do {
    Start-Sleep -Seconds 1
    try {
        $health = Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 5
        $ready = $health.status -eq "ok" -and $health.database -eq "up"
    }
    catch { $ready = $false }
} while (-not $ready -and (Get-Date) -lt $deadline)
if (-not $ready) { throw "API did not recover after strict Artifact publication" }
$reloaded = Invoke-RestMethod `
    -Uri "$BaseUrl/api/v1/ontology/versions/$($version.id)/compiled-artifact" `
    -TimeoutSec 30
$persistedRun = Invoke-RestMethod `
    -Uri "$BaseUrl/api/v1/ontology/construction-runs/$($run.run_id)" `
    -TimeoutSec 30
if ($reloaded.bundle_hash -ne $artifact.bundle_hash -or
    $persistedRun.promoted_draft_id -ne $promoted.draft.id -or
    $persistedRun.published_version_id -ne $version.id -or
    -not $persistedRun.runtime_activation_succeeded) {
    throw "API restart did not preserve ConstructionRun or active Artifact provenance"
}

$questions = @(
    "查询近30天各分行信用卡交易金额和交易笔数",
    "按交易渠道查询交易金额",
    "查询各分行活跃客户数",
    "查询近30天各分行信用卡交易金额和排名"
)
$queryResults = @()
foreach ($question in $questions) {
    $result = Invoke-JsonPost "/api/v1/query" @{
        question = $question
        query_mode = "ontology"
    }
    if ($result.status -ne "success" -or
        -not $result.validation_report.valid -or
        -not $result.validation_report.explain_passed -or
        [int]$result.execution_result.row_count -le 0 -or
        $result.generated_sql -match (
            "legacy_card_transaction|tmp_transaction_result|test_transaction_copy"
        )) {
        throw "Constructed ontology failed core query: $question"
    }
    $queryResults += $result
}
if ($queryResults[0].generated_sql -notmatch 'COUNT\(DISTINCT') {
    throw "Branch credit-card query omitted the requested transaction count"
}

Write-Host "[construction] Running five-source Text-to-SQL mock smoke..."
$constructionSmoke = @(
    @{
        label = "schema"
        mode = "schema"
        sql_assets = $false
        expected_variant = "schema"
    },
    @{
        label = "physical_rag"
        mode = "rag"
        sql_assets = $false
        expected_variant = "rag"
    },
    @{
        label = "reviewed_auto_scaffold_ontology_no_sql_asset"
        mode = "ontology"
        sql_assets = $false
        expected_variant = "ontology_no_sql_asset"
    },
    @{
        label = "reviewed_auto_scaffold_ontology_full"
        mode = "ontology"
        sql_assets = $true
        expected_variant = "ontology_full"
    }
)
$smokeHashes = @{}
foreach ($strategy in $constructionSmoke) {
    $smoke = Invoke-JsonPost "/api/v1/query" @{
        question = $questions[0]
        query_mode = $strategy.mode
        sql_asset_enabled = $strategy.sql_assets
    }
    if ($smoke.status -ne "success" -or
        $smoke.strategy_variant -ne $strategy.expected_variant -or
        [int]$smoke.execution_result.row_count -le 0) {
        throw "Construction smoke failed for $($strategy.label)"
    }
    $smokeHashes[$strategy.label] = Get-ResultHash $smoke.execution_result
}

$goldActivation = Invoke-JsonPost `
    "/api/v1/ontology/versions/$GoldVersion/activate" @{}
if ($goldActivation.id -eq $version.id) {
    throw "Gold and construction smoke used the same ontology_version_id"
}
$goldSmoke = Invoke-JsonPost "/api/v1/query" @{
    question = $questions[0]
    query_mode = "ontology"
    sql_asset_enabled = $true
}
if ($goldSmoke.status -ne "success" -or
    $goldSmoke.strategy_variant -ne "ontology_full" -or
    [int]$goldSmoke.execution_result.row_count -le 0) {
    throw "Gold ontology full smoke failed"
}
$smokeHashes["gold_ontology_full"] = Get-ResultHash $goldSmoke.execution_result
$null = Invoke-JsonPost `
    "/api/v1/ontology/versions/$($version.version)/activate" @{}

$evaluation = Invoke-JsonPost `
    "/api/v1/ontology/construction-runs/$($run.run_id)/evaluate" @{}
if (-not $evaluation.metrics.strict_validation_passed -or
    -not $evaluation.metrics.publication_succeeded -or
    -not $evaluation.metrics.runtime_activation_succeeded -or
    $evaluation.metrics.seed_accessed -or
    $evaluation.metrics.fallback_used -or
    $evaluation.metrics.legacy_ontology_accessed) {
    throw "Construction Evaluation does not reflect the completed strict lifecycle"
}
if ($evaluation.evaluation_schema_version -ne "2.0" -or
    $null -eq $evaluation.raw_candidate_metrics -or
    $null -eq $evaluation.reviewed_draft_metrics -or
    $null -eq $evaluation.review_delta -or
    $null -eq $evaluation.review_cost -or
    $evaluation.raw_candidate_metrics.link_endpoint_pair_f1 -le 0 -or
    $evaluation.raw_candidate_metrics.directed_link_f1 -le 0) {
    throw "Construction Evaluation V2 lacks separated raw/reviewed quality or Link diagnostics"
}

Write-Host (
    "[construction] Smoke hashes schema=$($smokeHashes.schema) " +
    "physical_rag=$($smokeHashes.physical_rag) " +
    "reviewed_no_asset=" +
    "$($smokeHashes.reviewed_auto_scaffold_ontology_no_sql_asset) " +
    "reviewed_full=$($smokeHashes.reviewed_auto_scaffold_ontology_full) " +
    "gold_full=$($smokeHashes.gold_ontology_full)"
)
Write-Host (
    "[construction] Passed run=$($run.run_id) candidates=$(@($candidates).Count) " +
    "reviews=$($review.decisions.ACCEPT)/$($review.decisions.MODIFY)/" +
    "$($review.decisions.REJECT) draft=$($promoted.draft.id) " +
    "version=$($version.id) bundle_hash=$($artifact.bundle_hash)"
)
