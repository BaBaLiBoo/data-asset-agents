. "$PSScriptRoot\scripts\bootstrap.ps1"
$Python = Initialize-Task1Environment
Write-Host "Rebuilding the frozen independent 360-asset / 270-pair V3 dataset..."
& $Python -m scripts.build_independent_dataset
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Running independent pair comparison and Top-K retrieval evaluation..."
& $Python -m scripts.evaluate_independent
exit $LASTEXITCODE
