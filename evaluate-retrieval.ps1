. "$PSScriptRoot\scripts\bootstrap.ps1"
$Python = Initialize-Task1Environment

Write-Host "Step 1/2: generate independent retrieval queries"
& $Python "$PSScriptRoot\scripts\generate_retrieval_dataset.py"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Step 2/2: calibrate on DEV and evaluate once on TEST"
& $Python "$PSScriptRoot\scripts\evaluate_retrieval.py"
exit $LASTEXITCODE
