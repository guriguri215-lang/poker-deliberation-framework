$ErrorActionPreference = "Stop"

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $workspaceRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Expected workspace virtual environment Python at $python"
}

Push-Location $workspaceRoot
try {
    & $python -m pytest
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    & $python -m ruff check .
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    & $python -m ruff format --check .
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    & $python -m mypy src
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    Pop-Location
}
