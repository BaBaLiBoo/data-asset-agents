param(
    [int]$TimeoutSeconds = 300,
    [string]$ComposeProject = "data-asset-agents-acceptance"
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($env:GIT_COMMIT_SHA)) {
    $env:GIT_COMMIT_SHA = (git rev-parse HEAD).Trim()
}
if ($env:GIT_COMMIT_SHA -notmatch "^[0-9a-f]{40}$") {
    throw "GIT_COMMIT_SHA must be a 40-character lowercase hexadecimal SHA"
}
$implementation = Join-Path $PSScriptRoot "acceptance_impl.ps1"
$source = Get-Content -LiteralPath $implementation -Raw -Encoding UTF8
$script = [ScriptBlock]::Create($source)
& $script -TimeoutSeconds $TimeoutSeconds -ComposeProject $ComposeProject
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
