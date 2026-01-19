param(
  [Parameter(Mandatory = $true)]
  [string]$RepoPath,

  [Parameter(Mandatory = $true)]
  [string]$RemoteUrl,

  [string]$Branch = "main",
  [string]$CommitMessage = "Sync from HA Samba",
  [switch]$NoPush
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-Cmd([string]$name) {
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if (-not $cmd) { throw "Missing command: $name (install it and ensure it's in PATH)" }
}

Assert-Cmd "git"

$repo = (Resolve-Path -LiteralPath $RepoPath).Path
Set-Location -LiteralPath $repo

if (-not (Test-Path -LiteralPath (Join-Path $repo ".git"))) {
  Write-Host "[git] init" -ForegroundColor Cyan
  git init | Out-Host
}

Write-Host "[git] ensure branch=$Branch" -ForegroundColor Cyan
git branch -M $Branch | Out-Host

Write-Host "[git] set remote origin=$RemoteUrl" -ForegroundColor Cyan
$hasOrigin = $false
try {
  $remotes = (git remote) -split "\r?\n"
  $hasOrigin = $remotes -contains "origin"
} catch {
  $hasOrigin = $false
}
if ($hasOrigin) {
  git remote set-url origin $RemoteUrl | Out-Host
} else {
  git remote add origin $RemoteUrl | Out-Host
}

Write-Host "[git] add ." -ForegroundColor Cyan
git add . | Out-Host

Write-Host "[git] commit (if needed)" -ForegroundColor Cyan
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
  git commit -m $CommitMessage | Out-Host
} else {
  Write-Host "[git] nothing to commit" -ForegroundColor DarkGray
}

if ($NoPush) {
  Write-Host "[git] skip push (-NoPush)" -ForegroundColor Yellow
  exit 0
}

Write-Host "[git] push -u origin $Branch" -ForegroundColor Cyan
git push -u origin $Branch | Out-Host

