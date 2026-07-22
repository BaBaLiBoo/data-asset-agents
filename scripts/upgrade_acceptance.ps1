param(
    [int]$TimeoutSeconds = 180,
    [string]$ComposeProject = "data-asset-agents-upgrade"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$exitCode = 0
$legacyContainer = "$ComposeProject-legacy-postgres"
$volumeName = "${ComposeProject}_minibank_data"

function Assert-LastExitCode([string]$Message) {
    if ($LASTEXITCODE -ne 0) { throw $Message }
}

function Invoke-DatabaseSql([string]$Sql) {
    $Sql | docker compose -p $ComposeProject exec -T postgres `
        psql -U $env:POSTGRES_USER -d $env:POSTGRES_DB -v ON_ERROR_STOP=1
    Assert-LastExitCode "PostgreSQL command failed"
}

function Wait-Api([int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 5
            if ($health.status -eq "ok" -and $health.database -eq "up") { return }
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    throw "API did not become healthy within $Seconds seconds"
}

$env:POSTGRES_DB = if ($env:POSTGRES_DB) { $env:POSTGRES_DB } else { "minibank" }
$env:POSTGRES_USER = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { "minibank" }
$env:LLM_MODE = "mock"

try {
    Write-Host "[1/10] Preparing an isolated pre-DDL-009 PostgreSQL volume..."
    docker compose -p $ComposeProject down --volumes --remove-orphans 2>$null
    docker rm -f $legacyContainer 2>$null
    docker volume rm $volumeName 2>$null
    docker volume create `
        --label "com.docker.compose.project=$ComposeProject" `
        --label "com.docker.compose.volume=minibank_data" `
        $volumeName | Out-Null

    $oldFiles = @(
        "data/ddl/001_schema.sql",
        "data/ddl/002_ontology_builder.sql",
        "data/ddl/003_release_safety_and_search.sql",
        "data/ddl/004_sql_assets.sql",
        "data/ddl/005_sql_asset_builds.sql",
        "data/ddl/006_evaluation.sql",
        "data/ddl/006_ontology_manager_core.sql",
        "data/ddl/007_ontology_runtime_governance.sql",
        "data/ddl/008_ontology_analysis_semantics.sql",
        "data/seed/002_seed.sql"
    )
    $dockerArgs = @(
        "run", "-d", "--name", $legacyContainer,
        "--mount", "type=volume,source=$volumeName,target=/var/lib/postgresql/data",
        "-e", "POSTGRES_DB=$($env:POSTGRES_DB)",
        "-e", "POSTGRES_USER=$($env:POSTGRES_USER)",
        "-e", "POSTGRES_HOST_AUTH_METHOD=trust"
    )
    for ($index = 0; $index -lt $oldFiles.Count; $index++) {
        $source = (Resolve-Path $oldFiles[$index]).Path
        $target = if ($index -eq ($oldFiles.Count - 1)) {
            "/docker-entrypoint-initdb.d/010_seed.sql"
        } else {
            "/docker-entrypoint-initdb.d/$([IO.Path]::GetFileName($oldFiles[$index]))"
        }
        $dockerArgs += @("--mount", "type=bind,source=$source,target=$target,readonly")
    }
    $dockerArgs += "pgvector/pgvector:pg16"
    & docker @dockerArgs | Out-Null
    Assert-LastExitCode "Could not start the isolated legacy PostgreSQL container"

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        docker exec $legacyContainer pg_isready `
            -U $env:POSTGRES_USER -d $env:POSTGRES_DB *> $null
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    if ($LASTEXITCODE -ne 0) { throw "Legacy PostgreSQL did not become ready" }

    Write-Host "[2/10] Inserting fictional legacy version, Draft, resources, and builds..."
    docker compose -p $ComposeProject build api | Out-Null
    Assert-LastExitCode "Could not build the API image for fixture generation"
    $fixtureSql = & docker compose -p $ComposeProject run --rm --no-deps api `
        python scripts/generate_upgrade_fixture.py 2>$null
    Assert-LastExitCode "Could not generate the legacy upgrade fixture"
    $fixtureSql | docker exec -i $legacyContainer `
        psql -U $env:POSTGRES_USER -d $env:POSTGRES_DB -v ON_ERROR_STOP=1
    Assert-LastExitCode "Could not insert the legacy upgrade fixture"

    Write-Host "[3/10] Handing the populated old volume to current Compose..."
    docker stop $legacyContainer | Out-Null
    docker rm $legacyContainer | Out-Null
    docker compose -p $ComposeProject up -d postgres | Out-Null
    Assert-LastExitCode "Could not start PostgreSQL from the populated old volume"

    Write-Host "[4/10] Applying DDL 009 twice to prove upgrade idempotency..."
    $migration = Get-Content data/ddl/009_ontology_release_governance.sql -Raw
    Invoke-DatabaseSql $migration
    Invoke-DatabaseSql $migration
    $migrationState = docker compose -p $ComposeProject exec -T postgres `
        psql -U $env:POSTGRES_USER -d $env:POSTGRES_DB -tAc `
        "SELECT resource_hash || '|' || validation_state FROM ontology_draft WHERE draft_id='legacy-upgrade-draft'"
    Assert-LastExitCode "Could not read the migrated Draft"
    $parts = $migrationState.Trim().Split('|')
    if ($parts.Count -ne 2 -or $parts[0].Length -ne 64 -or $parts[1] -ne "STALE") {
        throw "Legacy Draft did not receive a compatibility hash and STALE validation state"
    }

    Write-Host "[5/10] Starting the current API against the upgraded volume..."
    docker compose -p $ComposeProject up --build -d --no-deps api | Out-Null
    Assert-LastExitCode "Could not start the current API"
    Wait-Api $TimeoutSeconds

    $versions = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/ontology/versions"
    $legacyVersion = $versions | Where-Object { $_.id -eq "legacy-upgrade-version-id" }
    $legacyDraft = Invoke-RestMethod `
        -Uri "http://localhost:8000/api/v1/ontology/drafts/legacy-upgrade-draft"
    if ($null -eq $legacyVersion -or $null -eq $legacyDraft -or
        $legacyDraft.draft.validation_state -ne "STALE") {
        throw "Legacy published version or Draft was not readable after upgrade"
    }
    $legacyArtifactStatus = docker compose -p $ComposeProject exec -T postgres `
        psql -U $env:POSTGRES_USER -d $env:POSTGRES_DB -tAc `
        "SELECT count(*) FROM ontology_compiled_artifact WHERE ontology_version_id='legacy-upgrade-version-id'"
    if ([int]$legacyArtifactStatus.Trim() -ne 0) {
        throw "Migration invented an artifact for a legacy version"
    }

    Write-Host "[6/10] Publishing a current-version Draft with a READY artifact..."
    $draftBody = @{
        draft_name = "Upgrade acceptance current Draft"
        created_by = "upgrade-acceptance"
        seed_name = "retail_banking"
    } | ConvertTo-Json
    $draft = Invoke-RestMethod -Method Post `
        -Uri "http://localhost:8000/api/v1/ontology/drafts/from-seed" `
        -ContentType "application/json; charset=utf-8" -Body $draftBody
    $headers = @{ "If-Match" = '"' + $draft.draft.resource_revision + '"' }
    $validated = Invoke-RestMethod -Method Post `
        -Uri "http://localhost:8000/api/v1/ontology/drafts/$($draft.draft.id)/validate" `
        -Headers $headers -TimeoutSec 120
    if (-not $validated.draft.validation_report.valid -or
        @($validated.draft.validation_report.dry_run_cases).Count -ne 4) {
        throw "Current Draft validation did not pass all four Dry Runs"
    }
    $actor = @{ actor = "upgrade-acceptance" } | ConvertTo-Json
    Invoke-RestMethod -Method Post `
        -Uri "http://localhost:8000/api/v1/ontology/drafts/$($draft.draft.id)/submit" `
        -Headers $headers -ContentType "application/json" -Body $actor | Out-Null
    Invoke-RestMethod -Method Post `
        -Uri "http://localhost:8000/api/v1/ontology/drafts/$($draft.draft.id)/approve" `
        -Headers $headers -ContentType "application/json" -Body $actor -TimeoutSec 120 | Out-Null
    $publish = @{
        actor = "upgrade-acceptance"
        version = "upgrade-current-v1"
        description = "Fictional post-upgrade release"
    } | ConvertTo-Json
    $currentVersion = Invoke-RestMethod -Method Post `
        -Uri "http://localhost:8000/api/v1/ontology/drafts/$($draft.draft.id)/publish" `
        -Headers $headers -ContentType "application/json" -Body $publish -TimeoutSec 120
    $artifact = Invoke-RestMethod `
        -Uri "http://localhost:8000/api/v1/ontology/versions/$($currentVersion.id)/compiled-artifact"
    if ($artifact.status -ne "READY" -or
        [string]::IsNullOrWhiteSpace($artifact.bundle_hash)) {
        throw "Post-upgrade publication did not create a READY artifact"
    }

    Write-Host "[7/10] Activating legacy fallback and current artifact independently..."
    $legacyActivation = Invoke-RestMethod -Method Post `
        -Uri "http://localhost:8000/api/v1/ontology/versions/legacy-upgrade-v1/activate" `
        -TimeoutSec 120
    $currentActivation = Invoke-RestMethod -Method Post `
        -Uri "http://localhost:8000/api/v1/ontology/versions/upgrade-current-v1/activate" `
        -TimeoutSec 120
    if ($legacyActivation.id -ne "legacy-upgrade-version-id" -or
        $currentActivation.id -ne $currentVersion.id) {
        throw "Legacy fallback or current artifact activation failed"
    }

    Write-Host "[8/10] Verifying old build rows and audit redaction survived..."
    $preserved = docker compose -p $ComposeProject exec -T postgres `
        psql -U $env:POSTGRES_USER -d $env:POSTGRES_DB -tAc `
        "SELECT concat_ws('|',(SELECT count(*) FROM sql_asset_build WHERE build_id='legacy-upgrade-sql-build'),(SELECT count(*) FROM ontology_index_build WHERE build_id='legacy-upgrade-index-build'),(SELECT count(*) FROM ontology_audit_event WHERE metadata::text ~* '(secret|credential|password)'))"
    $preservedParts = $preserved.Trim().Split('|')
    if ($preservedParts.Count -ne 3 -or [int]$preservedParts[0] -ne 1 -or
        [int]$preservedParts[1] -ne 1 -or [int]$preservedParts[2] -ne 0) {
        throw "Legacy builds were lost or sensitive text entered the audit log"
    }

    Write-Host "[9/10] Proving artifact and audit rows are database-immutable..."
    docker compose -p $ComposeProject exec -T postgres `
        psql -U $env:POSTGRES_USER -d $env:POSTGRES_DB -c `
        "UPDATE ontology_compiled_artifact SET bundle_hash=repeat('0',64) WHERE ontology_version_id='$($currentVersion.id)'" *> $null
    if ($LASTEXITCODE -eq 0) { throw "READY artifact update unexpectedly succeeded" }
    docker compose -p $ComposeProject exec -T postgres `
        psql -U $env:POSTGRES_USER -d $env:POSTGRES_DB -c `
        "DELETE FROM ontology_audit_event WHERE ontology_version_id='$($currentVersion.id)'" *> $null
    if ($LASTEXITCODE -eq 0) { throw "Audit event delete unexpectedly succeeded" }

    Write-Host "[10/10] Upgrade acceptance passed."
}
catch {
    $exitCode = 1
    Write-Error "Upgrade acceptance failed: $($_.Exception.Message)" -ErrorAction Continue
    try {
        docker compose -p $ComposeProject ps
        docker compose -p $ComposeProject logs --no-color
    } catch {
        Write-Warning "Could not collect upgrade diagnostics: $($_.Exception.Message)"
    }
}
finally {
    try { docker rm -f $legacyContainer 2>$null | Out-Null } catch { }
    try { docker compose -p $ComposeProject down --volumes --remove-orphans | Out-Null } catch {
        Write-Warning "Could not stop upgrade acceptance services: $($_.Exception.Message)"
        $exitCode = 1
    }
    try { docker volume rm $volumeName 2>$null | Out-Null } catch { }
}

exit $exitCode
