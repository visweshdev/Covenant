# Install Covenant with pipx on Windows (its own environment, `covenant` on your PATH).
#   powershell -ExecutionPolicy Bypass -File install.ps1           # the latest release from PyPI
#   powershell -ExecutionPolicy Bypass -File install.ps1 -GitHub   # the newest code from GitHub (main branch)
param([switch]$GitHub)
$ErrorActionPreference = "Stop"
$RepoUrl = "git+https://github.com/visweshdev/Covenant.git"

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) { Write-Host "covenant: Python 3.11 or newer is needed (python.org)"; exit 1 }
& $py.Source -c "import sys; sys.exit(sys.version_info < (3, 11))"
if ($LASTEXITCODE -ne 0) { Write-Host "covenant: Python 3.11 or newer is needed"; exit 1 }

& $py.Source -m pipx --version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing pipx..."
    & $py.Source -m pip install --user --quiet pipx
    & $py.Source -m pipx ensurepath | Out-Null
}

if ($GitHub) { & $py.Source -m pipx install --force $RepoUrl }
else { & $py.Source -m pipx install --force covenant-agents }
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host ""
Write-Host "Done. In a repo:"
Write-Host '  covenant compile --pack safe-refactor "what you want the agent to do"'
Write-Host "  covenant init"
Write-Host "(Open a new terminal if 'covenant' isn't found yet.)"
