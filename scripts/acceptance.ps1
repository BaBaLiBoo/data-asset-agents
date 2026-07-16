param(
    [int]$TimeoutSeconds = 120,
    [string]$ComposeProject = "data-asset-agents-acceptance"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$exitCode = 0

try {
    Write-Host "[1/8] Validating Docker Compose configuration..."
    docker compose -p $ComposeProject config --quiet

    Write-Host "[2/8] Building and starting services..."
    docker compose -p $ComposeProject up --build -d

    Write-Host "[3/8] Waiting for API health..."
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

    Write-Host "[4/8] Waiting for Streamlit health..."
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

    Write-Host "[5/8] Running the target ontology query..."
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

    Write-Host "[6/8] Verifying SQL asset search..."
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

    Write-Host "[7/8] Running certified CTE + window rewrite..."
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

    Write-Host "[8/8] Container status..."
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
