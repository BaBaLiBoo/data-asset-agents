# Loaded explicitly as UTF-8 by acceptance.ps1 for Windows PowerShell 5.1.
param(
    [int]$TimeoutSeconds = 300,
    [string]$ComposeProject = "data-asset-agents-acceptance",
    [Parameter(Mandatory = $true)]
    [string]$ScriptsRoot,
    [int]$PostgresPort = 15432,
    [int]$ApiPort = 18000,
    [int]$WebPort = 18501
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$exitCode = 0

function Assert-LastExitCode([string]$Message) {
    if ($LASTEXITCODE -ne 0) { throw $Message }
}

function Get-TextSha256([string]$Text) {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $algorithm.ComputeHash($bytes)
    }
    finally {
        $algorithm.Dispose()
    }
    return ([System.BitConverter]::ToString($hash)).Replace("-", "").ToLowerInvariant()
}

$env:POSTGRES_DB = if ($env:POSTGRES_DB) { $env:POSTGRES_DB } else { "minibank" }
$env:POSTGRES_USER = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { "minibank" }
$env:LLM_MODE = "mock"
$env:LLM_API_KEY = ""
$env:EMBEDDING_API_KEY = ""
$env:POSTGRES_HOST_PORT = [string]$PostgresPort
$env:API_HOST_PORT = [string]$ApiPort
$env:WEB_HOST_PORT = [string]$WebPort
$apiBaseUrl = "http://localhost:$ApiPort"
$webBaseUrl = "http://localhost:$WebPort"

try {
    Write-Host "[1/15] Validating Docker Compose configuration..."
    docker compose -p $ComposeProject config --quiet
    Assert-LastExitCode "Docker Compose configuration is invalid"

    Write-Host "[2/15] Building and starting services..."
    docker compose -p $ComposeProject up --build -d
    Assert-LastExitCode "Could not build and start acceptance services"

    Write-Host "[3/15] Waiting for API health..."
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $healthy = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "$apiBaseUrl/health" -TimeoutSec 5
            if ($health.status -eq "ok" -and $health.database -eq "up") {
                $healthy = $true
                break
            }
        }
        catch {
            Start-Sleep -Seconds 2
        }
    }
    if (-not $healthy) {
        throw "API did not become healthy within $TimeoutSeconds seconds"
    }

    Write-Host "[4/15] Waiting for Streamlit health..."
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $webHealthy = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $webHealth = Invoke-WebRequest `
                -Uri "$webBaseUrl/_stcore/health" `
                -TimeoutSec 5 `
                -UseBasicParsing
            if ($webHealth.StatusCode -eq 200 -and $webHealth.Content.Trim() -eq "ok") {
                $webHealthy = $true
                break
            }
        }
        catch {
            Start-Sleep -Seconds 2
        }
    }
    if (-not $webHealthy) {
        throw "Streamlit did not become healthy within $TimeoutSeconds seconds"
    }

    Write-Host "[5/15] Building evidence and validating a direct object-seed Draft..."
    $build = Invoke-RestMethod `
        -Method Post `
        -Uri "$apiBaseUrl/api/v1/ontology/build" `
        -ContentType "application/json; charset=utf-8" `
        -Body "{}" `
        -TimeoutSec 300
    if ([string]::IsNullOrWhiteSpace($build.snapshot.id) -or
        $build.snapshot.tables.Count -le 0) {
        throw "Metadata evidence build did not return a usable snapshot"
    }
    $reviewCandidate = $build.concepts |
        Where-Object {
            $_.table_name -eq "dwd_card_transaction" -and
            $_.column_name -eq "posted_amount"
        } |
        Select-Object -First 1
    if ($null -eq $reviewCandidate) {
        throw "Metadata build did not produce the expected fictional review candidate"
    }
    $reviewBody = @{
        reviewer = "acceptance-reviewer"
        note = "Reviewed masked metadata and historical SQL evidence"
    } | ConvertTo-Json
    $verifiedCandidate = Invoke-RestMethod `
        -Method Post `
        -Uri ("$apiBaseUrl/api/v1/ontology/candidates/" +
              $reviewCandidate.id + "/verify") `
        -ContentType "application/json; charset=utf-8" `
        -Body $reviewBody `
        -TimeoutSec 30
    if ($verifiedCandidate.status -ne "VERIFIED") {
        throw "Human review did not transition the candidate to VERIFIED"
    }
    $draftBody = @{
        draft_name = "Acceptance direct object seed"
        created_by = "acceptance"
        seed_name = "retail_banking"
        source_snapshot_id = $build.snapshot.id
    } | ConvertTo-Json
    $draft = Invoke-RestMethod `
        -Method Post `
        -Uri "$apiBaseUrl/api/v1/ontology/drafts/from-seed" `
        -ContentType "application/json; charset=utf-8" `
        -Body $draftBody `
        -TimeoutSec 30
    $draftHeaders = @{ "If-Match" = '"' + $draft.draft.resource_revision + '"' }
    if ($draft.draft.resource_revision -lt 0 -or
        [string]::IsNullOrWhiteSpace($draft.draft.resource_hash)) {
        throw "Direct object seed Draft has no deterministic revision/hash identity"
    }
    $transaction = $draft.resources.object_types |
        Where-Object { $_.id -eq "transaction" } |
        Select-Object -First 1
    $binding = $draft.resources.bindings |
        Where-Object { $_.object_type_id -eq "transaction" } |
        Select-Object -First 1
    $branchLink = $draft.resources.link_types |
        Where-Object { $_.id -eq "transaction_belongs_to_branch" } |
        Select-Object -First 1
    $amountMetric = $draft.resources.metrics |
        Where-Object { $_.id -eq "credit_card_transaction_amount" } |
        Select-Object -First 1
    $branchDimension = $draft.resources.dimensions |
        Where-Object { $_.id -eq "branch" } |
        Select-Object -First 1
    if ($null -eq $transaction -or
        $transaction.property_ids -notcontains "transaction.amount" -or
        $binding.table_name -ne "dwd_card_transaction" -or
        $binding.property_bindings."transaction.amount" -ne "txn_amount_cny" -or
        $null -eq $branchLink -or
        $amountMetric.measure_property_id -ne "transaction.amount" -or
        $amountMetric.PSObject.Properties.Name -contains "expression" -or
        $branchDimension.property_id -ne "branch.name" -or
        $branchDimension.PSObject.Properties.Name -contains "table") {
        throw "Direct object seed did not produce the governed Transaction model"
    }

    $candidates = Invoke-RestMethod `
        -Method Post `
        -Uri ("$apiBaseUrl/api/v1/ontology/drafts/" +
              $draft.draft.id + "/candidates/generate") `
        -TimeoutSec 120
    $requiredExclusions = @(
        "dws_branch_transaction_day",
        "legacy_card_transaction",
        "tmp_transaction_result",
        "test_transaction_copy"
    )
    foreach ($excludedTable in $requiredExclusions) {
        if ($null -eq $candidates.excluded_tables.$excludedTable) {
            throw "Object candidate generation did not exclude $excludedTable"
        }
    }
    $transactionProperties = @(
        $candidates.properties |
            Where-Object {
                $_.property.object_type_id -eq "transaction" -and
                $_.property.id -ne "transaction.posted_amount"
            }
    )
    $transactionBinding = $candidates.bindings |
        Where-Object { $_.binding.object_type_id -eq "transaction" } |
        Select-Object -First 1
    if ($transactionProperties.Count -le 0 -or
        $null -eq $transactionBinding -or
        $transactionProperties[0].evidence.Count -le 0) {
        throw "Metadata/profile/SQL evidence did not produce reviewable candidates"
    }
    $candidateIds = @($verifiedCandidate.id) +
        @($transactionProperties.candidate_id) +
        @($transactionBinding.candidate_id)
    $importBody = @{
        candidate_ids = $candidateIds
        actor = "acceptance-reviewer"
    } | ConvertTo-Json -Depth 10
    $draft = Invoke-RestMethod `
        -Method Post `
        -Uri ("$apiBaseUrl/api/v1/ontology/drafts/" +
              $draft.draft.id + "/import-candidates") `
        -ContentType "application/json; charset=utf-8" `
        -Headers $draftHeaders `
        -Body $importBody `
        -TimeoutSec 120
    $draftHeaders = @{ "If-Match" = '"' + $draft.draft.resource_revision + '"' }
    if ($draft.draft.status -ne "DRAFT") {
        throw "Candidate review unexpectedly changed Draft publication state"
    }
    $draftDiff = Invoke-RestMethod `
        -Uri "$apiBaseUrl/api/v1/ontology/drafts/$($draft.draft.id)/diff" `
        -TimeoutSec 30
    $draftImpact = Invoke-RestMethod `
        -Uri "$apiBaseUrl/api/v1/ontology/drafts/$($draft.draft.id)/impact" `
        -TimeoutSec 30
    if ($draftDiff.added_objects.Count -le 0 -or
        -not $draftImpact.rebuild_concept_index) {
        throw "Draft Diff and impact analysis did not identify the direct resources"
    }
    $validated = Invoke-RestMethod `
        -Method Post `
        -Uri "$apiBaseUrl/api/v1/ontology/drafts/$($draft.draft.id)/validate" `
        -Headers $draftHeaders `
        -TimeoutSec 30
    if (-not $validated.draft.validation_report.valid) {
        throw "Object model Draft validation failed"
    }
    if ($validated.draft.validation_report.resource_revision -ne
            $validated.draft.resource_revision -or
        $validated.draft.validation_report.resource_hash -ne
            $validated.draft.resource_hash -or
        [string]::IsNullOrWhiteSpace(
            $validated.draft.validation_report.validation_run_id
        )) {
        throw "Validation report is not bound to the exact Draft snapshot"
    }
    $failedDryRuns = @(
        $validated.draft.validation_report.dry_run_cases |
            Where-Object { -not $_.explain_passed }
    )
    if (@($validated.draft.validation_report.dry_run_cases).Count -ne 4 -or
        $failedDryRuns.Count -ne 0) {
        throw "Dynamic semantic Dry Run did not pass all four core questions"
    }
    $validatedRevision = $validated.draft.resource_revision
    $validatedHash = $validated.draft.resource_hash
    $validatedMetric = $validated.resources.metrics |
        Where-Object { $_.id -eq "credit_card_transaction_amount" } |
        Select-Object -First 1
    $validatedMetric.description = "Acceptance governance freshness update"
    $staleDraft = Invoke-RestMethod `
        -Method Put `
        -Uri ("$apiBaseUrl/api/v1/ontology/drafts/" +
              $draft.draft.id + "/metrics/credit_card_transaction_amount") `
        -ContentType "application/json; charset=utf-8" `
        -Headers $draftHeaders `
        -Body ($validatedMetric | ConvertTo-Json -Depth 20) `
        -TimeoutSec 30
    if ($staleDraft.draft.resource_revision -ne ($validatedRevision + 1) -or
        $staleDraft.draft.resource_hash -eq $validatedHash -or
        $staleDraft.draft.validation_state -ne "STALE" -or
        $null -ne $staleDraft.draft.validation_report) {
        throw "Draft mutation did not invalidate the exact validation snapshot"
    }
    $staleHeaders = @{
        "If-Match" = '"' + $staleDraft.draft.resource_revision + '"'
    }
    $staleSubmitFailed = $false
    try {
        Invoke-RestMethod `
            -Method Post `
            -Uri "$apiBaseUrl/api/v1/ontology/drafts/$($draft.draft.id)/submit" `
            -ContentType "application/json; charset=utf-8" `
            -Headers $staleHeaders `
            -Body (@{ actor = "acceptance-reviewer" } | ConvertTo-Json) `
            -TimeoutSec 30 | Out-Null
    } catch {
        $staleSubmitFailed = $true
    }
    if (-not $staleSubmitFailed) {
        throw "Stale Draft unexpectedly passed Submit"
    }
    $revisionConflict = $false
    try {
        Invoke-RestMethod `
            -Method Put `
            -Uri ("$apiBaseUrl/api/v1/ontology/drafts/" +
                  $draft.draft.id + "/metrics/credit_card_transaction_amount") `
            -ContentType "application/json; charset=utf-8" `
            -Headers $draftHeaders `
            -Body ($validatedMetric | ConvertTo-Json -Depth 20) `
            -TimeoutSec 30 | Out-Null
    } catch {
        $revisionConflict = ($_.Exception.Response.StatusCode.value__ -eq 409)
    }
    if (-not $revisionConflict) {
        throw "Outdated Draft revision unexpectedly overwrote current content"
    }
    $validatedMetric.description = "Acceptance governance current revision update"
    $draft = Invoke-RestMethod `
        -Method Put `
        -Uri ("$apiBaseUrl/api/v1/ontology/drafts/" +
              $draft.draft.id + "/metrics/credit_card_transaction_amount") `
        -ContentType "application/json; charset=utf-8" `
        -Headers $staleHeaders `
        -Body ($validatedMetric | ConvertTo-Json -Depth 20) `
        -TimeoutSec 30
    $draftHeaders = @{ "If-Match" = '"' + $draft.draft.resource_revision + '"' }
    $validated = Invoke-RestMethod `
        -Method Post `
        -Uri "$apiBaseUrl/api/v1/ontology/drafts/$($draft.draft.id)/validate" `
        -Headers $draftHeaders `
        -TimeoutSec 120
    if (-not $validated.draft.validation_report.valid) {
        throw "Revalidation after governed mutation failed"
    }

    Write-Host "[6/15] Reviewing and atomically publishing the object model..."
    $actorBody = @{ actor = "acceptance-reviewer" } | ConvertTo-Json
    Invoke-RestMethod `
        -Method Post `
        -Uri "$apiBaseUrl/api/v1/ontology/drafts/$($draft.draft.id)/submit" `
        -ContentType "application/json; charset=utf-8" `
        -Headers $draftHeaders `
        -Body $actorBody `
        -TimeoutSec 30 | Out-Null
    Invoke-RestMethod `
        -Method Post `
        -Uri "$apiBaseUrl/api/v1/ontology/drafts/$($draft.draft.id)/approve" `
        -ContentType "application/json; charset=utf-8" `
        -Headers $draftHeaders `
        -Body $actorBody `
        -TimeoutSec 30 | Out-Null
    $objectVersion = "acceptance-object-$([Guid]::NewGuid().ToString('N'))"
    $publishBody = @{
        actor = "acceptance-reviewer"
        version = $objectVersion
        description = "Acceptance object model"
    } | ConvertTo-Json
    $publishedBase = Invoke-RestMethod `
        -Method Post `
        -Uri "$apiBaseUrl/api/v1/ontology/drafts/$($draft.draft.id)/publish" `
        -ContentType "application/json; charset=utf-8" `
        -Headers $draftHeaders `
        -Body $publishBody `
        -TimeoutSec 120
    $baseArtifact = Invoke-RestMethod `
        -Uri ("$apiBaseUrl/api/v1/ontology/versions/" +
              $publishedBase.id + "/compiled-artifact") `
        -TimeoutSec 30
    if ($baseArtifact.status -ne "READY" -or
        [string]::IsNullOrWhiteSpace($baseArtifact.bundle_hash) -or
        $baseArtifact.source_resource_hash -ne $draft.draft.resource_hash) {
        throw "Published version did not persist the reviewed compiled artifact"
    }
    $resourceCountSql = (
        "SELECT concat_ws('|'," +
        "(SELECT count(*) FROM published_metric_definition " +
        "WHERE ontology_version_id='$($publishedBase.id)')," +
        "(SELECT count(*) FROM published_dimension_definition " +
        "WHERE ontology_version_id='$($publishedBase.id)')," +
        "(SELECT count(*) FROM ontology_compiled_artifact " +
        "WHERE ontology_version_id='$($publishedBase.id)' AND status='READY'))"
    )
    $publishedResourceCounts = docker compose -p $ComposeProject exec -T postgres `
        psql -U $env:POSTGRES_USER -d $env:POSTGRES_DB -tAc $resourceCountSql
    $publishedCounts = $publishedResourceCounts.Trim().Split('|')
    if ($publishedCounts.Count -ne 3 -or
        [int]$publishedCounts[0] -le 0 -or
        [int]$publishedCounts[1] -le 0 -or
        [int]$publishedCounts[2] -ne 1) {
        throw "Metric, Dimension, and artifact were not atomically published together"
    }
    Write-Host (
        "Published version=$($publishedBase.version) " +
        "resource_hash=$($baseArtifact.source_resource_hash) " +
        "bundle_hash=$($baseArtifact.bundle_hash) " +
        "compiler_version=$($baseArtifact.compiler_version)"
    )
    $baseAudit = Invoke-RestMethod `
        -Uri ("$apiBaseUrl/api/v1/ontology/drafts/" +
              $draft.draft.id + "/audit-events?limit=500") `
        -TimeoutSec 30
    $baseActions = @($baseAudit.action)
    foreach ($requiredAction in @(
        "VALIDATION_PASSED", "SUBMITTED", "APPROVED", "PUBLISHED"
    )) {
        if ($baseActions -notcontains $requiredAction) {
            throw "Ontology audit trail is missing $requiredAction"
        }
    }

    Write-Host "[6a/15] Proving a non-breaking incremental Draft..."
    $incrementalBody = @{
        name = "Acceptance non-breaking description update"
        created_by = "acceptance"
        base_version_id = $publishedBase.id
    } | ConvertTo-Json
    $incremental = Invoke-RestMethod `
        -Method Post `
        -Uri "$apiBaseUrl/api/v1/ontology/drafts" `
        -ContentType "application/json; charset=utf-8" `
        -Body $incrementalBody `
        -TimeoutSec 30
    $incrementalHeaders = @{
        "If-Match" = '"' + $incremental.draft.resource_revision + '"'
    }
    $statusProperty = $incremental.resources.properties |
        Where-Object { $_.id -eq "transaction.status" } |
        Select-Object -First 1
    $statusProperty.description = "Acceptance-only clarified fictional status description"
    $incremental = Invoke-RestMethod `
        -Method Put `
        -Uri ("$apiBaseUrl/api/v1/ontology/drafts/" +
              $incremental.draft.id + "/properties/transaction.status") `
        -ContentType "application/json; charset=utf-8" `
        -Headers $incrementalHeaders `
        -Body ($statusProperty | ConvertTo-Json -Depth 20) `
        -TimeoutSec 30
    $incrementalHeaders = @{
        "If-Match" = '"' + $incremental.draft.resource_revision + '"'
    }
    $metricDefinition = $incremental.resources.metrics |
        Where-Object { $_.id -eq "credit_card_transaction_amount" } |
        Select-Object -First 1
    $metricDefinition.description = "Acceptance-only clarified fictional metric definition"
    $incremental = Invoke-RestMethod `
        -Method Put `
        -Uri ("$apiBaseUrl/api/v1/ontology/drafts/" +
              $incremental.draft.id + "/metrics/credit_card_transaction_amount") `
        -ContentType "application/json; charset=utf-8" `
        -Headers $incrementalHeaders `
        -Body ($metricDefinition | ConvertTo-Json -Depth 20) `
        -TimeoutSec 30
    $incrementalHeaders = @{
        "If-Match" = '"' + $incremental.draft.resource_revision + '"'
    }
    $incrementalDiff = Invoke-RestMethod `
        -Uri ("$apiBaseUrl/api/v1/ontology/drafts/" +
              $incremental.draft.id + "/diff") `
        -TimeoutSec 30
    $incrementalImpact = Invoke-RestMethod `
        -Uri ("$apiBaseUrl/api/v1/ontology/drafts/" +
              $incremental.draft.id + "/impact") `
        -TimeoutSec 30
    if (@($incrementalDiff.modified_properties).Count -ne 1 -or
        @($incrementalDiff.modified_metrics).Count -ne 1 -or
        $incrementalDiff.modified_properties[0].breaking_level -ne "NON_BREAKING" -or
        $incrementalDiff.modified_metrics[0].breaking_level -ne "NON_BREAKING" -or
        -not $incrementalImpact.automatic_publish_allowed) {
        throw "Description-only Draft was not classified as NON_BREAKING"
    }
    Invoke-RestMethod `
        -Method Post `
        -Uri ("$apiBaseUrl/api/v1/ontology/drafts/" +
              $incremental.draft.id + "/validate") `
        -Headers $incrementalHeaders `
        -TimeoutSec 120 | Out-Null
    Invoke-RestMethod `
        -Method Post `
        -Uri ("$apiBaseUrl/api/v1/ontology/drafts/" +
              $incremental.draft.id + "/submit") `
        -ContentType "application/json; charset=utf-8" `
        -Headers $incrementalHeaders `
        -Body $actorBody `
        -TimeoutSec 30 | Out-Null
    Invoke-RestMethod `
        -Method Post `
        -Uri ("$apiBaseUrl/api/v1/ontology/drafts/" +
              $incremental.draft.id + "/approve") `
        -ContentType "application/json; charset=utf-8" `
        -Headers $incrementalHeaders `
        -Body $actorBody `
        -TimeoutSec 120 | Out-Null
    $incrementalPublishBody = @{
        actor = "acceptance-reviewer"
        version = "acceptance-followup-$([Guid]::NewGuid().ToString('N').Substring(0, 12))"
        description = "Acceptance non-breaking follow-up"
    } | ConvertTo-Json
    $publishedFollowup = Invoke-RestMethod `
        -Method Post `
        -Uri ("$apiBaseUrl/api/v1/ontology/drafts/" +
              $incremental.draft.id + "/publish") `
        -ContentType "application/json; charset=utf-8" `
        -Headers $incrementalHeaders `
        -Body $incrementalPublishBody `
        -TimeoutSec 120
    $followupArtifact = Invoke-RestMethod `
        -Uri ("$apiBaseUrl/api/v1/ontology/versions/" +
              $publishedFollowup.id + "/compiled-artifact") `
        -TimeoutSec 30
    if ($followupArtifact.status -ne "READY") {
        throw "Follow-up ontology artifact is not READY"
    }
    $reproBody = @{
        question = "查询近30天各分行信用卡交易金额和交易笔数。"
        query_mode = "ontology"
    } | ConvertTo-Json
    $beforeRestartQuery = Invoke-RestMethod `
        -Method Post `
        -Uri "$apiBaseUrl/api/v1/query" `
        -ContentType "application/json; charset=utf-8" `
        -Body $reproBody `
        -TimeoutSec 30
    $beforeRestartRows = $beforeRestartQuery.execution_result.rows |
        ConvertTo-Json -Depth 20 -Compress
    $beforeRestartResultHash = Get-TextSha256 $beforeRestartRows
    docker compose -p $ComposeProject restart api | Out-Null
    $apiRestarted = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $healthAfterRestart = Invoke-RestMethod `
                -Uri "$apiBaseUrl/health" `
                -TimeoutSec 5
            if ($healthAfterRestart.status -eq "ok") {
                $apiRestarted = $true
                break
            }
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    if (-not $apiRestarted) {
        throw "API did not recover after artifact reproducibility restart"
    }
    $versionsAfterRestart = Invoke-RestMethod `
        -Uri "$apiBaseUrl/api/v1/ontology/versions" `
        -TimeoutSec 30
    $currentAfterRestart = $versionsAfterRestart |
        Where-Object { $_.is_current } |
        Select-Object -First 1
    $artifactAfterRestart = Invoke-RestMethod `
        -Uri ("$apiBaseUrl/api/v1/ontology/versions/" +
              $currentAfterRestart.id + "/compiled-artifact") `
        -TimeoutSec 30
    if ($currentAfterRestart.id -ne $publishedFollowup.id -or
        $artifactAfterRestart.bundle_hash -ne $followupArtifact.bundle_hash) {
        throw "Restart did not reproduce the current ontology artifact"
    }
    $afterRestartQuery = Invoke-RestMethod `
        -Method Post `
        -Uri "$apiBaseUrl/api/v1/query" `
        -ContentType "application/json; charset=utf-8" `
        -Body $reproBody `
        -TimeoutSec 30
    $afterRestartRows = $afterRestartQuery.execution_result.rows |
        ConvertTo-Json -Depth 20 -Compress
    if ($afterRestartQuery.generated_sql -ne $beforeRestartQuery.generated_sql -or
        (Get-TextSha256 $afterRestartRows) -ne $beforeRestartResultHash) {
        throw "Restart changed the governed SQL or query result snapshot"
    }
    $rolledBack = Invoke-RestMethod `
        -Method Post `
        -Uri "$apiBaseUrl/api/v1/ontology/versions/$objectVersion/activate" `
        -TimeoutSec 120
    $rollbackArtifact = Invoke-RestMethod `
        -Uri ("$apiBaseUrl/api/v1/ontology/versions/" +
              $rolledBack.id + "/compiled-artifact") `
        -TimeoutSec 30
    if ($rolledBack.id -ne $publishedBase.id -or
        $rollbackArtifact.bundle_hash -ne $baseArtifact.bundle_hash) {
        throw "Rollback did not load the target version artifact"
    }
    $rollbackAudit = Invoke-RestMethod `
        -Uri ("$apiBaseUrl/api/v1/ontology/versions/" +
              $publishedBase.id + "/audit-events?limit=500") `
        -TimeoutSec 30
    $releaseActions = @($baseActions) + @($rollbackAudit.action)
    foreach ($requiredAction in @(
        "VALIDATION_PASSED", "SUBMITTED", "APPROVED", "PUBLISHED",
        "ACTIVATED", "ROLLED_BACK"
    )) {
        if ($releaseActions -notcontains $requiredAction) {
            throw "Ontology release audit trail is missing $requiredAction"
        }
    }

    Write-Host "[7/15] Verifying the published object graph..."
    $objectGraph = Invoke-RestMethod `
        -Uri "$apiBaseUrl/api/v1/ontology/object-graph" `
        -TimeoutSec 30
    if ($objectGraph.nodes.Count -ne 6 -or
        ($objectGraph.edges.id -notcontains "transaction_belongs_to_branch")) {
        throw "Published object graph is incomplete"
    }

    Write-Host "[7a/15] Building versioned ontology index and checking drift..."
    $indexBody = @{ index_type = "BUSINESS_CONCEPT" } | ConvertTo-Json
    $indexBuild = Invoke-RestMethod `
        -Method Post `
        -Uri "$apiBaseUrl/api/v1/ontology/index-builds" `
        -ContentType "application/json; charset=utf-8" `
        -Body $indexBody `
        -TimeoutSec 120
    if ($indexBuild.status -ne "READY" -or
        -not $indexBuild.is_current -or
        $indexBuild.document_count -le 0) {
        throw "Versioned ontology index did not become READY and current"
    }
    $syncRun = Invoke-RestMethod `
        -Method Post `
        -Uri "$apiBaseUrl/api/v1/ontology/sync-runs" `
        -TimeoutSec 120
    $breakingDrift = @(
        $syncRun.reports | Where-Object { $_.severity -eq "BREAKING" }
    )
    if ($syncRun.status -ne "READY" -or $breakingDrift.Count -gt 0) {
        throw "Metadata sync failed or reported unexpected breaking drift"
    }

    Write-Host "[7b/15] Exercising the read-only Object Explorer..."
    $records = Invoke-RestMethod `
        -Uri "$apiBaseUrl/api/v1/objects/transaction?limit=2" `
        -TimeoutSec 30
    if ($records.Count -le 0 -or
        $records[0].available_links -notcontains "transaction_belongs_to_branch" -or
        $records[0].properties.PSObject.Properties.Value -notcontains "***MASKED***") {
        throw "Object Explorer returned no safe governed Transaction records"
    }
    $objectId = $records[0].primary_key
    $branchRecords = Invoke-RestMethod `
        -Uri ("$apiBaseUrl/api/v1/objects/transaction/" + $objectId +
              "/links/transaction_belongs_to_branch?limit=2") `
        -TimeoutSec 30
    if ($branchRecords.Count -le 0) {
        throw "Object Explorer Link navigation returned no Branch"
    }

    Write-Host "[8/15] Running the target ontology query..."
    $body = @{
        question = "查询近30天各分行信用卡交易金额和交易笔数。"
        query_mode = "ontology"
    } | ConvertTo-Json
    $result = Invoke-RestMethod `
        -Method Post `
        -Uri "$apiBaseUrl/api/v1/query" `
        -ContentType "application/json; charset=utf-8" `
        -Body $body `
        -TimeoutSec 30
    if ($result.status -ne "success") {
        throw "Target query did not return success status"
    }
    if ($result.execution_result.row_count -le 0) {
        throw "Target query returned no rows"
    }
    if ($result.sql_asset_candidates.Count -le 0) {
        throw "Target query did not retrieve a safe certified SQL asset"
    }
    Write-Host "Returned rows: $($result.execution_result.row_count)"
    Write-Host $result.generated_sql

    Write-Host "[9/15] Verifying SQL asset search..."
    $searchBody = @{ question = "查询各分行信用卡交易金额"; limit = 3 } |
        ConvertTo-Json
    $assets = Invoke-RestMethod `
        -Method Post `
        -Uri "$apiBaseUrl/api/v1/sql-assets/search" `
        -ContentType "application/json; charset=utf-8" `
        -Body $searchBody `
        -TimeoutSec 30
    if ($assets.Count -le 0 -or -not $assets[0].asset.lifecycle_valid) {
        throw "SQL asset hybrid search returned no lifecycle-valid template"
    }

    Write-Host "[10/15] Running certified CTE + window rewrite..."
    $complexBody = @{
        question = "查询近30天各分行信用卡交易金额和排名。"
        query_mode = "ontology"
    } | ConvertTo-Json
    $complex = Invoke-RestMethod `
        -Method Post `
        -Uri "$apiBaseUrl/api/v1/query" `
        -ContentType "application/json; charset=utf-8" `
        -Body $complexBody `
        -TimeoutSec 30
    if ($complex.status -ne "success" -or -not $complex.sql_rewrite.used_template) {
        throw "Complex query did not use a certified AST template"
    }
    if ($null -eq $complex.selected_sql_asset -or
        $null -eq $complex.selected_template_rank) {
        throw "Complex query did not record SQLAsset selection provenance"
    }
    if ($complex.generated_sql -notmatch "WITH branch_totals" -or
        $complex.generated_sql -notmatch "DENSE_RANK") {
        throw "Complex rewrite did not preserve CTE and window function"
    }
    if (-not $complex.validation_report.valid -or
        -not $complex.validation_report.explain_passed -or
        $complex.execution_result.row_count -le 0) {
        throw "Complex rewrite failed validation, EXPLAIN, or execution"
    }

    Write-Host "[11/15] Running isolated strategy smoke queries..."
    $strategyCases = @(
        @{ mode = "schema"; sqlAssets = $false; variant = "schema" },
        @{ mode = "rag"; sqlAssets = $false; variant = "rag" },
        @{
            mode = "ontology"
            sqlAssets = $false
            variant = "ontology_no_sql_asset"
        },
        @{ mode = "ontology"; sqlAssets = $true; variant = "ontology_full" }
    )
    foreach ($strategy in $strategyCases) {
        $strategyBody = @{
            question = "查询近30天各分行信用卡交易金额和交易笔数。"
            query_mode = $strategy.mode
            sql_asset_enabled = $strategy.sqlAssets
        } | ConvertTo-Json
        $strategyResult = Invoke-RestMethod `
            -Method Post `
            -Uri "$apiBaseUrl/api/v1/query" `
            -ContentType "application/json; charset=utf-8" `
            -Body $strategyBody `
            -TimeoutSec 30
        if ($strategyResult.status -ne "success" -or
            $strategyResult.strategy_variant -ne $strategy.variant) {
            throw "Strategy smoke failed for $($strategy.variant)"
        }
        if ($strategy.variant -eq "ontology_no_sql_asset" -and
            $null -ne $strategyResult.selected_sql_asset) {
            throw "Ontology ablation unexpectedly selected a SQLAsset"
        }
    }

    Write-Host "[12/15] Validating the 80-case benchmark..."
    docker compose -p $ComposeProject exec -T api `
        python -m data_asset_agents.evaluation.cli validate-benchmark
    if ($LASTEXITCODE -ne 0) {
        throw "Benchmark validation failed"
    }
    docker compose -p $ComposeProject exec -T api `
        python scripts/materialize_benchmark_hashes.py --check
    if ($LASTEXITCODE -ne 0) {
        throw "Benchmark result hashes did not match the fixed MiniBank seed"
    }

    Write-Host "[13/15] Creating smoke EvaluationRuns..."
    $runIds = @()
    foreach ($strategy in $strategyCases) {
        $runBody = @{
            query_mode = $strategy.mode
            strategy_variant = $strategy.variant
            sql_asset_enabled = $strategy.sqlAssets
            run_kind = "smoke"
            max_cases = 2
            concurrency = 1
        } | ConvertTo-Json
        $run = Invoke-RestMethod `
            -Method Post `
            -Uri "$apiBaseUrl/api/v1/evaluation/runs" `
            -ContentType "application/json; charset=utf-8" `
            -Body $runBody `
            -TimeoutSec 30
        $runIds += $run.run_id
    }
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $allCompleted = $true
        foreach ($runId in $runIds) {
            $runState = Invoke-RestMethod `
                -Uri "$apiBaseUrl/api/v1/evaluation/runs/$runId" `
                -TimeoutSec 10
            if ($runState.run.status -eq "FAILED") {
                throw "EvaluationRun $runId failed: $($runState.run.error_message)"
            }
            if ([string]::IsNullOrWhiteSpace($env:GIT_COMMIT_SHA) -or
                $runState.run.git_commit_sha -ne $env:GIT_COMMIT_SHA) {
                throw "EvaluationRun Git SHA does not match the injected commit"
            }
            if ($runState.run.strategy_variant -like "ontology_*") {
                if ([string]::IsNullOrWhiteSpace($runState.run.ontology_version_id) -or
                    $runState.run.ontology_version_id.StartsWith("yaml-seed-") -or
                    [string]::IsNullOrWhiteSpace($runState.run.bundle_hash)) {
                    throw "Ontology EvaluationRun lacks governed provenance"
                }
            }
            if ($runState.run.strategy_variant -eq "ontology_full" -and
                [string]::IsNullOrWhiteSpace($runState.run.sql_asset_build_id)) {
                throw "Ontology full EvaluationRun lacks SQLAssetBuild provenance"
            }
            if ($runState.run.status -ne "COMPLETED") {
                $allCompleted = $false
            }
        }
        if (-not $allCompleted) { Start-Sleep -Seconds 1 }
    } while (-not $allCompleted -and (Get-Date) -lt $deadline)
    if (-not $allCompleted) {
        throw "Smoke EvaluationRuns did not finish before timeout"
    }

    Write-Host "[14/15] Comparing smoke runs..."
    $compareQuery = ($runIds | ForEach-Object { "run_id=$_" }) -join "&"
    $comparison = Invoke-RestMethod `
        -Uri "$apiBaseUrl/api/v1/evaluation/compare?$compareQuery" `
        -TimeoutSec 30
    if ($comparison.runs.Count -ne 4 -or $comparison.warnings.Count -ne 0) {
        throw "Smoke comparison did not return four fair runs"
    }

    Write-Host "[14a/15] Running data-source-driven Ontology Construction acceptance..."
    $constructionPath = Join-Path $ScriptsRoot "ontology_construction_acceptance.ps1"
    $constructionSource = Get-Content -LiteralPath $constructionPath -Raw -Encoding UTF8
    $constructionScript = [ScriptBlock]::Create($constructionSource)
    & $constructionScript `
        -ComposeProject $ComposeProject -TimeoutSeconds $TimeoutSeconds `
        -GoldVersion $publishedBase.version -BaseUrl $apiBaseUrl
    if ($LASTEXITCODE -ne 0) {
        throw "Ontology Construction acceptance failed"
    }

    Write-Host "[15/15] Container status..."
    docker compose -p $ComposeProject ps
    Write-Host "Acceptance passed."
}
catch {
    $exitCode = 1
    Write-Host "Acceptance failed: $($_.Exception.Message)" -ForegroundColor Red
    try {
        docker compose -p $ComposeProject ps
        docker compose -p $ComposeProject logs --no-color --tail 200
    }
    catch {
        Write-Warning "Could not collect Docker diagnostics: $($_.Exception.Message)"
    }
}
finally {
    Write-Host "Stopping Docker Compose services..."
    try {
        docker compose -p $ComposeProject down --volumes --remove-orphans
    }
    catch {
        Write-Warning "Could not stop acceptance services: $($_.Exception.Message)"
        $exitCode = 1
    }
}

exit $exitCode
