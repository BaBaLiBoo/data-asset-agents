function Initialize-Task1Environment {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
    $EnvFile = Join-Path $ProjectRoot ".env"
    if (Test-Path -LiteralPath $EnvFile) {
        foreach ($Line in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
            $Trimmed = $Line.Trim()
            if (-not $Trimmed -or $Trimmed.StartsWith("#")) { continue }
            $Parts = $Trimmed.Split("=", 2)
            if ($Parts.Count -eq 2) {
                $Name = $Parts[0].Trim()
                $CurrentValue = [Environment]::GetEnvironmentVariable($Name, "Process")
                if ([string]::IsNullOrWhiteSpace($CurrentValue)) {
                    [Environment]::SetEnvironmentVariable(
                        $Name,
                        $Parts[1].Trim(),
                        "Process"
                    )
                }
            }
        }
    }
    # Backward compatibility for .env files created by earlier demo versions.
    if (-not $env:EMBEDDING_API_KEY -and $env:DASHSCOPE_API_KEY) {
        $env:EMBEDDING_API_KEY = $env:DASHSCOPE_API_KEY
    }
    if (-not $env:EMBEDDING_BASE_URL -and $env:DASHSCOPE_BASE_URL) {
        $env:EMBEDDING_BASE_URL = $env:DASHSCOPE_BASE_URL
    }
    if (-not $env:EMBEDDING_API_KEY) {
        throw "Missing EMBEDDING_API_KEY. Configure the deployment environment or run .\configure.ps1 with the Embedding service parameters."
    }
    if (-not $env:EMBEDDING_BASE_URL) {
        throw "Missing EMBEDDING_BASE_URL."
    }
    if (-not $env:EMBEDDING_MODEL) {
        throw "Missing EMBEDDING_MODEL."
    }
    if (-not $env:EMBEDDING_DIMENSION) {
        throw "Missing EMBEDDING_DIMENSION."
    }
    if (-not $env:EMBEDDING_SEND_DIMENSIONS) {
        $env:EMBEDDING_SEND_DIMENSIONS = "true"
    }
    if (-not $env:EMBEDDING_API_KEY_HEADER) {
        $env:EMBEDDING_API_KEY_HEADER = "Authorization"
    }
    if ($null -eq $env:EMBEDDING_API_KEY_PREFIX) {
        $env:EMBEDDING_API_KEY_PREFIX = "Bearer"
    }
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONPATH = $ProjectRoot
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

    $WorkspaceRoot = Split-Path -Parent $ProjectRoot
    $WorkspacePython = Join-Path $WorkspaceRoot ".venv\Scripts\python.exe"
    $ProjectPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $WorkspacePython) { return $WorkspacePython }
    if (Test-Path -LiteralPath $ProjectPython) { return $ProjectPython }
    return "python"
}
