. "$PSScriptRoot\scripts\bootstrap.ps1"
$Python = Initialize-Task1Environment
Write-Host "Embedding service: configured from environment"
Write-Host "API docs: http://127.0.0.1:8000/docs"
& $Python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
exit $LASTEXITCODE
