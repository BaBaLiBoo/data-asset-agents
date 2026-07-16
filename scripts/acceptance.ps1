param(
    [int]$TimeoutSeconds = 120,
    [string]$ComposeProject = "data-asset-agents-acceptance"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$exitCode = 0

# Use an ephemeral local-only database password when the caller did not provide one.
$env:POSTGRES_DB = if ($env:POSTGRES_DB) { $env:POSTGRES_DB } else { "minibank" }
$env:POSTGRES_USER = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { "minibank" }
if ([string]::IsNullOrWhiteSpace($env:POSTGRES_PASSWORD)) {
    $env:POSTGRES_PASSWORD = "acceptance-$([Guid]::NewGuid().ToString('N'))"
}
if ([string]::IsNullOrWhiteSpace($env:DOCKER_DATABASE_URL)) {
    $env:DOCKER_DATABASE_URL = (
        "postgresql+psycopg://$($env:POSTGRES_USER):$($env:POSTGRES_PASSWORD)" +
        "@postgres:5432/$($env:POSTGRES_DB)"
    )
}

try {
    Write-Host "[1/12] Validating Docker Compose configuration..."
    docker compose -p $ComposeProject config --quiet

    Write-Host "[2/12] Building and starting services..."
    docker compose -p $ComposeProject up --build -d

    Write-Host "[3/12] Waiting for API health..."
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $healthy = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 5
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

    Write-Host "[4/12] Waiting for Streamlit health..."
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $webHealthy = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $webHealth = Invoke-WebRequest `
                -Uri "http://localhost:8501/_stcore/health" `
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

    Write-Host "[5/12] Running the target ontology query..."
    $body = @{
        question = "查询近30天各分行信用卡交易金额和交易笔数。"
        query_mode = "ontology"
    } | ConvertTo-Json
    $result = Invoke-RestMethod `
        -Method Post `
        -Uri "http://localhost:8000/api/v1/query" `
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

    Write-Host "[6/12] Verifying SQL asset search..."
    $searchBody = @{ question = "查询各分行信用卡交易金额"; limit = 3 } |
        ConvertTo-Json
    $assets = Invoke-RestMethod `
        -Method Post `
        -Uri "http://localhost:8000/api/v1/sql-assets/search" `
        -ContentType "application/json; charset=utf-8" `
        -Body $searchBody `
        -TimeoutSec 30
    if ($assets.Count -le 0 -or -not $assets[0].asset.lifecycle_valid) {
        throw "SQL asset hybrid search returned no lifecycle-valid template"
    }

    Write-Host "[7/12] Running certified CTE + window rewrite..."
    $complexBody = @{
        question = "查询近30天各分行信用卡交易金额和排名。"
        query_mode = "ontology"
    } | ConvertTo-Json
    $complex = Invoke-RestMethod `
        -Method Post `
        -Uri "http://localhost:8000/api/v1/query" `
        -ContentType "application/json; charset=utf-8" `
        -Body $complexBody `
        -TimeoutSec 30
    if ($complex.status -ne "success" -or -not $complex.sql_rewrite.used_template) {
        throw "Complex query did not use a certified AST template"
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

    Write-Host "[8/12] Running isolated strategy smoke queries..."
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
            -Uri "http://localhost:8000/api/v1/query" `
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

    Write-Host "[9/12] Validating the 80-case benchmark..."
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

    Write-Host "[10/12] Creating smoke EvaluationRuns..."
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
            -Uri "http://localhost:8000/api/v1/evaluation/runs" `
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
                -Uri "http://localhost:8000/api/v1/evaluation/runs/$runId" `
                -TimeoutSec 10
            if ($runState.run.status -eq "FAILED") {
                throw "EvaluationRun $runId failed: $($runState.run.error_message)"
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

    Write-Host "[11/12] Comparing smoke runs..."
    $compareQuery = ($runIds | ForEach-Object { "run_id=$_" }) -join "&"
    $comparison = Invoke-RestMethod `
        -Uri "http://localhost:8000/api/v1/evaluation/compare?$compareQuery" `
        -TimeoutSec 30
    if ($comparison.runs.Count -ne 4 -or $comparison.warnings.Count -ne 0) {
        throw "Smoke comparison did not return four fair runs"
    }

    Write-Host "[12/12] Container status..."
    docker compose -p $ComposeProject ps
    Write-Host "Acceptance passed."
}
catch {
    $exitCode = 1
    Write-Error "Acceptance failed: $($_.Exception.Message)" -ErrorAction Continue
    try {
        docker compose -p $ComposeProject ps
        docker compose -p $ComposeProject logs --no-color
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
