# One-click pull latest code from GitHub (LastLW/web4api)
# Usage: .\pull_github.ps1
# Local credentials are safe: every config.json / browser_data / log is gitignored, pull never touches them.
Set-Location $PSScriptRoot

if (-not (Test-Path ".git")) {
    Write-Host "[x] This folder is not a git repo (probably downloaded as ZIP)." -ForegroundColor Red
    Write-Host "    Clone it instead so future updates work:"
    Write-Host "    git clone https://github.com/LastLW/web4api.git"
    exit 1
}

# Public repo: no token needed. Private fork: run `git config credential.helper manager` once.
$before = (git rev-parse HEAD).Trim()
git pull --ff-only origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[!] Pull failed: local changes or divergent commits." -ForegroundColor Yellow
    Write-Host "    Keep your edits aside, then retry:"
    Write-Host "      git stash"
    Write-Host "      .\pull_github.ps1"
    Write-Host "    (your native config.json is gitignored, so a normal pull never conflicts)"
    exit 1
}
$after = (git rev-parse HEAD).Trim()

if ($before -eq $after) {
    Write-Host "[i] Already up to date: $($after.Substring(0, 7))"
} else {
    Write-Host "[ok] Updated $($before.Substring(0, 7)) -> $($after.Substring(0, 7))" -ForegroundColor Green
    git log --oneline "$before..$after"
    Write-Host ""
    Write-Host "[!] Restart manager.py to load the new code (services keep their own state)."
}
