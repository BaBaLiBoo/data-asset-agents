param(
    [int]$TimeoutSeconds = 900,
    [string]$ComposeProject = "data-asset-agents-demo-flow",
    [int]$PostgresPort = 25432,
    [int]$ApiPort = 28000,
    [int]$WebPort = 28501,
    [switch]$KeepResources
)

$ErrorActionPreference = "Stop"

function Invoke-Json {
    param(
        [string]$Method,
        [string]$Uri,
        [object]$Body = $null,
        [hashtable]$Headers = @{}
    )
    $parameters = @{
        Method = $Method
        Uri = $Uri
        Headers = $Headers
        TimeoutSec = 180
    }
    if ($null -ne $Body) {
        $parameters["ContentType"] = "application/json"
        $parameters["Body"] = ($Body | ConvertTo-Json -Depth 40)
    }
    Invoke-RestMethod @parameters
}

function Wait-Http {
    param([string]$Uri, [int]$DeadlineSeconds)
    $deadline = (Get-Date).AddSeconds($DeadlineSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            Invoke-RestMethod -Uri $Uri -TimeoutSec 5 | Out-Null
            return
        }
        catch {
            Start-Sleep -Seconds 3
        }
    }
    throw "Timed out waiting for $Uri"
}

function Get-ResourceEndpoint {
    param([string]$ResourceType)
    switch ($ResourceType) {
        "object_type" { return "object-types" }
        "property" { return "properties" }
        "metric" { return "metrics" }
        "dimension" { return "dimensions" }
        "binding" { return "bindings" }
        "link_type" { return "link-types" }
        "physical_join" { return "physical-joins" }
        default { throw "Unknown resource type: $ResourceType" }
    }
}

if ([string]::IsNullOrWhiteSpace($env:GIT_COMMIT_SHA)) {
    $env:GIT_COMMIT_SHA = (git rev-parse HEAD).Trim()
}

$env:POSTGRES_HOST_PORT = "$PostgresPort"
$env:API_HOST_PORT = "$ApiPort"
$env:WEB_HOST_PORT = "$WebPort"
$env:POSTGRES_DB = "minibank"
$env:POSTGRES_USER = "minibank"
$env:LLM_MODE = "mock"
$env:DEMO_MODE = "true"
$env:DEMO_ONTOLOGY_VERSION_NAME = "quality-v2-gold-independent"
$env:DEMO_ONTOLOGY_DISPLAY_NAME = "MiniBank 正式业务本体 v1.0"

$api = "http://127.0.0.1:$ApiPort"
$web = "http://127.0.0.1:$WebPort"

try {
    docker compose -p $ComposeProject up --build -d
    Wait-Http "$api/health" $TimeoutSeconds
    Wait-Http $web $TimeoutSeconds

    $snapshotBuild = Invoke-Json -Method Post -Uri "$api/api/v1/ontology/metadata-snapshots/raw" -Body @{
        schema_name = "public"
        sample_limit = 5
        top_value_limit = 5
    }
    $snapshotId = $snapshotBuild.snapshot.id
    if ([string]::IsNullOrWhiteSpace($snapshotId)) {
        throw "RAW_METADATA Snapshot was not created"
    }

    $run = Invoke-Json -Method Post -Uri "$api/api/v1/ontology/construction-runs" -Body @{
        source_snapshot_id = $snapshotId
        catalog_mode = "GOVERNED_CATALOG"
        construction_mode = "STRICT_CONSTRUCTION"
        evidence_mode = "O-C"
        llm_mode = "mock"
        provider = "mock"
        model = "deterministic-rules-v1"
        temperature = 0
        random_seed = 20260715
        created_by = "demo-flow-fixture"
    }
    $run = Invoke-Json -Method Post -Uri "$api/api/v1/ontology/construction-runs/$($run.run_id)/generate"
    $candidates = Invoke-Json -Method Get -Uri "$api/api/v1/ontology/construction-runs/$($run.run_id)/candidates"
    if ($candidates.Count -eq 0) {
        throw "Construction Run generated no candidates"
    }

    $modifiedOne = $false
    foreach ($candidate in $candidates) {
        $decision = "ACCEPT"
        $modified = $null
        $comment = "Demo flow fixture accept"
        if (-not $modifiedOne -and $candidate.resource_type -eq "metric") {
            $decision = "MODIFY"
            $modified = $candidate.current_resource
            $modified.description = "$($modified.description) Demo fixture reviewed."
            $comment = "Demo flow fixture modifies one metric description"
            $modifiedOne = $true
        }
        Invoke-Json -Method Post `
            -Uri "$api/api/v1/ontology/construction-runs/$($run.run_id)/candidates/$($candidate.candidate_id)/review" `
            -Body @{
                decision = $decision
                reviewer = "demo-flow-fixture"
                modified_resource = $modified
                comment = $comment
                merge_target_candidate_id = $null
            } | Out-Null
    }

    $reviewed = Invoke-Json -Method Get -Uri "$api/api/v1/ontology/construction-runs/$($run.run_id)/candidates"
    $open = @($reviewed | Where-Object { $_.status -in @("PENDING", "DEFERRED") })
    if ($open.Count -gt 0) {
        throw "Not all candidates reached a final state"
    }

    $draft = Invoke-Json -Method Post `
        -Uri "$api/api/v1/ontology/construction-runs/$($run.run_id)/promote-to-draft" `
        -Body @{ draft_name = "Demo flow reviewed ontology"; actor = "demo-flow-fixture" }
    $draftId = $draft.draft.id
    $revision = $draft.draft.resource_revision
    $headers = @{ "If-Match" = "`"$revision`""; "X-Actor" = "demo-flow-fixture" }

    $draft = Invoke-Json -Method Post -Uri "$api/api/v1/ontology/drafts/$draftId/validate" -Headers $headers
    if (-not $draft.draft.validation_report.valid) {
        $report = $draft.draft.validation_report | ConvertTo-Json -Depth 40
        throw "Draft validation failed before submit: $report"
    }
    $revision = $draft.draft.resource_revision
    $headers["If-Match"] = "`"$revision`""
    $draft = Invoke-Json -Method Post -Uri "$api/api/v1/ontology/drafts/$draftId/submit" -Headers $headers -Body @{
        actor = "demo-flow-fixture"
    }
    $revision = $draft.draft.resource_revision
    $headers["If-Match"] = "`"$revision`""
    $draft = Invoke-Json -Method Post -Uri "$api/api/v1/ontology/drafts/$draftId/approve" -Headers $headers -Body @{
        actor = "demo-flow-reviewer"
    }
    $revision = $draft.draft.resource_revision
    $headers["If-Match"] = "`"$revision`""
    $versionName = "demo-flow-$((Get-Date).ToString('yyyyMMddHHmmss'))"
    $version = Invoke-Json -Method Post -Uri "$api/api/v1/ontology/drafts/$draftId/publish" -Headers $headers -Body @{
        actor = "demo-flow-reviewer"
        version = $versionName
        description = "Demo flow acceptance fixture publication"
        acknowledge_breaking_changes = $true
        change_ticket = "DEMO-FLOW"
    }
    Invoke-Json -Method Post -Uri "$api/api/v1/ontology/versions/$($version.version)/activate" | Out-Null

    $artifact = Invoke-Json -Method Get -Uri "$api/api/v1/ontology/versions/$($version.id)/compiled-artifact"
    if ($artifact.status -ne "READY") {
        throw "Compiled Artifact is not READY"
    }
    $graph = Invoke-Json -Method Get -Uri "$api/api/v1/ontology/object-graph"
    if ($graph.nodes.Count -eq 0) {
        throw "Object Graph is empty"
    }
    Invoke-Json -Method Post -Uri "$api/api/v1/query" -Body @{
        question = "查询交易金额"
        query_mode = "ontology"
        sql_asset_enabled = $true
    } | Out-Null
    Invoke-Json -Method Post -Uri "$api/api/v1/query" -Body @{
        question = "按客户类型统计交易笔数"
        query_mode = "ontology"
        sql_asset_enabled = $true
    } | Out-Null
    Invoke-Json -Method Post -Uri "$api/api/v1/query" -Body @{
        question = "查询近30天各分行信用卡交易金额"
        query_mode = "ontology"
        sql_asset_enabled = $true
    } | Out-Null
    $demo = Invoke-Json -Method Get -Uri "$api/api/v1/ontology/demo-status?run_example_checks=true"
    if ($demo.health_checks.Count -eq 0) {
        throw "Demo Status returned no health checks"
    }

    $rejectRun = Invoke-Json -Method Post -Uri "$api/api/v1/ontology/construction-runs" -Body @{
        source_snapshot_id = $snapshotId
        catalog_mode = "GOVERNED_CATALOG"
        construction_mode = "STRICT_CONSTRUCTION"
        evidence_mode = "O-C"
        llm_mode = "mock"
        provider = "mock"
        model = "deterministic-rules-v1"
        temperature = 0
        random_seed = 20260715
        created_by = "demo-flow-reject-fixture"
    }
    $rejectRun = Invoke-Json -Method Post -Uri "$api/api/v1/ontology/construction-runs/$($rejectRun.run_id)/generate"
    $rejectCandidates = Invoke-Json -Method Get -Uri "$api/api/v1/ontology/construction-runs/$($rejectRun.run_id)/candidates"
    $rejectCandidate = $rejectCandidates | Select-Object -First 1
    Invoke-Json -Method Post `
        -Uri "$api/api/v1/ontology/construction-runs/$($rejectRun.run_id)/candidates/$($rejectCandidate.candidate_id)/review" `
        -Body @{
            decision = "REJECT"
            reviewer = "demo-flow-reject-fixture"
            modified_resource = $null
            comment = "Reject fixture is isolated from the publish flow"
            merge_target_candidate_id = $null
        } | Out-Null
    $rejected = Invoke-Json -Method Get -Uri "$api/api/v1/ontology/construction-runs/$($rejectRun.run_id)/candidates"
    $rejectedOne = @($rejected | Where-Object { $_.status -eq "REJECTED" })
    if ($rejectedOne.Count -eq 0) {
        throw "Reject fixture did not persist a REJECTED candidate"
    }
    Write-Host "Demo Flow Acceptance PASSED"
}
finally {
    if (-not $KeepResources) {
        docker compose -p $ComposeProject down -v
    }
}






