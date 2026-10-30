# One-click upload to GitHub (LastLW/web4api)
# Usage:
#   .\upload_github.ps1          normal push
#   .\upload_github.ps1 -Force   force push (overwrite remote, e.g. remote has a README)
# Token source (first match wins): $env:GITHUB_TOKEN  ->  .github_token  ->  manual prompt
param([switch]$Force)

$RepoUrl = "https://github.com/LastLW/web4api.git"
$User = "LastLW"
Set-Location $PSScriptRoot

# ---- 1. token ----
$token = $env:GITHUB_TOKEN
if (-not $token -and (Test-Path ".github_token")) { $token = (Get-Content ".github_token" -Raw).Trim() }
if (-not $token) {
    Write-Host "[?] GitHub PAT not found. Paste it once (saved to gitignored .github_token):"
    $token = Read-Host "PAT"
    if (-not $token) { Write-Host "[x] No token, abort."; exit 1 }
    Set-Content -Path ".github_token" -Value $token.Trim() -NoNewline -Encoding ascii
}

# ---- 2. init repo / remote if needed ----
if (-not (Test-Path ".git")) {
    git init -b main
}
if (git remote get-url origin 2>$null) { git remote set-url origin $RepoUrl }
else { git remote add origin $RepoUrl }

# ---- 3. commit identity if missing ----
if (-not (git config user.email)) {
    git config user.name $User
    git config user.email "$User@users.noreply.github.com"
}

# ---- 4. stage everything + leak check ----
git add -A
$suspicious = git diff --cached --name-only |
    Select-String -Pattern "(^|/)config\.json$|\.github_token$|browser_data/|cookie\.txt$|\.log$|oplog\.json$|^\.venv/|^build/|^dist/|^\.screenshots/"
if ($suspicious) {
    Write-Host "[x] ABORT: secret/build files staged, fix .gitignore first:" -ForegroundColor Red
    $suspicious | ForEach-Object { Write-Host "    $($_.Line)" }
    exit 1
}

# ---- 5. commit ----
if (git status --porcelain) {
    git commit -m "sync: push local changes"
} else {
    Write-Host "[i] Nothing new to commit."
}

# ---- 6. push (token via HTTP header, never stored in .git/config) ----
$b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${User}:$token"))
$branch = git rev-parse --abbrev-ref HEAD
if ($Force) {
    git -c http.extraheader="Authorization: Basic $b64" push --force origin "${branch}:main"
} else {
    git -c http.extraheader="Authorization: Basic $b64" push -u origin "${branch}:main"
}
if ($LASTEXITCODE -ne 0 -and -not $Force) {
    Write-Host ""
    Write-Host "[!] Push rejected? If the remote repo already has commits (e.g. a README), run:"
    Write-Host "    .\upload_github.ps1 -Force"
    exit 1
}
Write-Host ""
Write-Host "[ok] Done. Repo: $RepoUrl" -ForegroundColor Green
