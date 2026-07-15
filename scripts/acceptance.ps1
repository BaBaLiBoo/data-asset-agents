param(
    [int]$TimeoutSeconds = 120,
    [string]$ComposeProject = "data-asset-agents-acceptance"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$exitCode = 0

try {
    Write-Host "[1/5] Validating Docker Compose configuration..."
    docker compose -p $ComposeProject config --quiet

    Write-Host "[2/5] Building and starting services..."
    docker compose -p $ComposeProject up --build -d

    Write-Host "[3/5] Waiting for API health..."
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

    Write-Host "[4/5] Running the target ontology query..."
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
    Write-Host "Returned rows: $($result.execution_result.row_count)"
    Write-Host $result.generated_sql

    Write-Host "[5/5] Container status..."
    docker compose -p $ComposeProject ps
    Write-Host "Acceptance passed."
}
catch {
    $exitCode = 1
    Write-Error "Acceptance failed: $($_.Exception.Message)"
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
