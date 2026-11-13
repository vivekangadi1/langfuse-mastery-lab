# One-time setup on a new Windows machine. From the repo root:
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
# Creates the Python venv, installs the pinned packages, registers the Jupyter kernel the notebooks use,
# and creates .env with generated Langfuse secrets (if it doesn't exist yet).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venv = "ch09\.venv-langfuse"
$py = "$venv\Scripts\python.exe"
$hasUv = [bool](Get-Command uv -ErrorAction SilentlyContinue)

if (-not (Test-Path $py)) {
    Write-Host "Creating $venv (Python 3.13)..."
    if ($hasUv) { uv venv $venv --python 3.13 } else { py -3.13 -m venv $venv }
}

Write-Host "Installing packages..."
if ($hasUv) {
    uv pip install --python $py -r ch09/langfuse_lab/requirements.txt
} else {
    & $py -m pip install --upgrade pip
    & $py -m pip install -r ch09/langfuse_lab/requirements.txt
}

Write-Host "Registering the Jupyter kernel 'Python (ch09 Langfuse v5)'..."
& $py -m ipykernel install --user --name ch09-langfuse-v5 --display-name "Python (ch09 Langfuse v5)"

if (-not (Test-Path ".env")) { & $py make_env.py } else { Write-Host ".env already exists: left unchanged." }

Write-Host ""
Write-Host "Next:"
Write-Host "  1. Edit .env: AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT, EVAL_JUDGE_MODEL"
Write-Host "  2. Start Docker Desktop, then:"
Write-Host "     docker compose --env-file .env -f ch09/docker-compose.langfuse.yml up -d"
Write-Host "  3. Open http://localhost:3000 (login = LANGFUSE_INIT_USER_EMAIL / PASSWORD in .env)"
Write-Host "  4. Open ch09/langfuse_mastery/00_setup.ipynb with the kernel 'Python (ch09 Langfuse v5)'"
