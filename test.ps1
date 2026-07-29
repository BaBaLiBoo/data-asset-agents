. "$PSScriptRoot\scripts\bootstrap.ps1"
$Python = Initialize-Task1Environment
$WorkspaceRoot = Split-Path -Parent $PSScriptRoot
$RunId = [Guid]::NewGuid().ToString("N")
$TestTemp = Join-Path $WorkspaceRoot "work\pytest-task1-v1-$RunId"
New-Item -ItemType Directory -Force -Path $TestTemp | Out-Null
Write-Host "Embedding test: real API call with cache enabled"
& $Python -m pytest -p no:cacheprovider --basetemp $TestTemp -q
exit $LASTEXITCODE
