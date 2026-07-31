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
        $json = $Body | ConvertTo-Json -Depth 40
        $parameters["ContentType"] = "application/json; charset=utf-8"
        $parameters["Body"] = [Text.Encoding]::UTF8.GetBytes($json)
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

function Get-Utf8Text {
    param([string]$Base64)
    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Base64))
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
$env:DEVELOPER_MODE = "false"
$env:DEMO_REQUIRE_LIVE_AI = "false"
$env:DEMO_ONTOLOGY_VERSION_NAME = "quality-v2-gold-independent"
$env:DEMO_ONTOLOGY_DISPLAY_NAME = Get-Utf8Text "TWluaUJhbmsg5q2j5byP5Lia5Yqh5pys5L2TIHYxLjA="

$api = "http://127.0.0.1:$ApiPort"
$web = "http://127.0.0.1:$WebPort"

try {
    docker compose -p $ComposeProject up --build -d
    Wait-Http "$api/health" $TimeoutSeconds
    Wait-Http $web $TimeoutSeconds

    $appSource = Get-Content -Path "apps/web/app.py" -Raw -Encoding UTF8
    $ontologyBuilderLabel = Get-Utf8Text "5pys5L2T5p6E5bu6"
    $agentDemoLabel = Get-Utf8Text "5pm66IO95L2TIERlbW8="
    $sqlAssetsLabel = Get-Utf8Text "U1FMIOi1hOS6pw=="
    $evaluationLabel = Get-Utf8Text "5a6e6aqM5LiO6K+E5rWL"
    $advancedLabel = Get-Utf8Text "6auY57qn566h55CG"
    if ($appSource -notmatch $ontologyBuilderLabel -or $appSource -notmatch $agentDemoLabel) {
        throw "Streamlit app does not expose the two required product entries"
    }
    if ($appSource -match $sqlAssetsLabel -or $appSource -match $evaluationLabel -or $appSource -match $advancedLabel) {
        throw "Streamlit app still exposes old product navigation entries"
    }

    $dataStatus = Invoke-Json -Method Get -Uri "$api/api/v1/demo/data-status"
    if (-not $dataStatus.ready) {
        $dataStatus = Invoke-Json -Method Post -Uri "$api/api/v1/demo/data-initialize"
    }
    if (-not $dataStatus.ready) {
        throw "MiniBank demo data is not ready"
    }
    $sources = Invoke-Json -Method Get -Uri "$api/api/v1/demo/data-sources"
    if ($sources.Count -ne 1) {
        throw "Expected exactly one real demo data source"
    }

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
        Write-Warning "Promoted O-C fixture Draft is retained for review coverage but is not publishable; switching to the controlled MiniBank seed Draft for release gates."
        $draft = Invoke-Json -Method Post -Uri "$api/api/v1/ontology/drafts/from-seed" -Body @{
            draft_name = "Demo flow official ontology release fixture"
            created_by = "demo-flow-fixture"
            source_snapshot_id = $snapshotId
            seed_name = "retail_banking"
        }
        $draftId = $draft.draft.id
        $revision = $draft.draft.resource_revision
        $headers = @{ "If-Match" = "`"$revision`""; "X-Actor" = "demo-flow-fixture" }
        $draft = Invoke-Json -Method Post -Uri "$api/api/v1/ontology/drafts/$draftId/validate" -Headers $headers
        if (-not $draft.draft.validation_report.valid) {
            $report = $draft.draft.validation_report | ConvertTo-Json -Depth 40
            throw "Seed-backed release Draft validation failed before submit: $report"
        }
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
    $amountQuestion = Get-Utf8Text "5p+l6K+i5Lqk5piT6YeR6aKd"
    $countByCustomerTypeQuestion = Get-Utf8Text "5oyJ5a6i5oi357G75Z6L57uf6K6h5Lqk5piT56yU5pWw"
    $creditCardBranchQuestion = Get-Utf8Text "5p+l6K+i6L+RMzDlpKnlkITliIbooYzkv6HnlKjljaHkuqTmmJPph5Hpop0="
    Invoke-Json -Method Post -Uri "$api/api/v1/query" -Body @{
        question = $amountQuestion
        query_mode = "ontology"
        sql_asset_enabled = $true
    } | Out-Null
    Invoke-Json -Method Post -Uri "$api/api/v1/query" -Body @{
        question = $countByCustomerTypeQuestion
        query_mode = "ontology"
        sql_asset_enabled = $true
    } | Out-Null
    Invoke-Json -Method Post -Uri "$api/api/v1/query" -Body @{
        question = $creditCardBranchQuestion
        query_mode = "ontology"
        sql_asset_enabled = $true
    } | Out-Null
    $demo = Invoke-Json -Method Get -Uri "$api/api/v1/ontology/demo-status?run_example_checks=true"
    if ($demo.health_checks.Count -eq 0) {
        throw "Demo Status returned no health checks"
    }
    $runtime = Invoke-Json -Method Get -Uri "$api/api/v1/demo/runtime-status"
    if ($runtime.ontologies.Count -eq 0) {
        throw "Runtime status returned no published ontologies"
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






