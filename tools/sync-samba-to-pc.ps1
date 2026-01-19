param(
  # Path della cartella add-on su Home Assistant (Samba)
  [string]$Source = "\\192.168.3.24\addons\ksenia_lares_addon",

  # Cartella repo locale (default: root del repo che contiene questo script)
  [string]$RepoPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path,

  # Crea commit se ci sono modifiche dopo la sync
  [switch]$AutoCommit,

  # Esegue anche push su origin/main (richiede credenziali già configurate)
  [switch]$Push,

  [string]$Branch = "main",
  [string]$CommitMessage = "Sync from HA Samba"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-Cmd([string]$name) {
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if (-not $cmd) { throw "Missing command: $name (install it and ensure it's in PATH)" }
}

Assert-Cmd "robocopy"
Assert-Cmd "git"

$src = (Resolve-Path -LiteralPath $Source).Path
$repo = (Resolve-Path -LiteralPath $RepoPath).Path

Write-Host "[sync] $src -> $repo" -ForegroundColor Cyan
& (Join-Path $PSScriptRoot "sync-addon.ps1") -Source $src -Dest $repo | Out-Host

if (-not $AutoCommit) {
  Write-Host "[git] skip commit (-AutoCommit not set)" -ForegroundColor DarkGray
  exit 0
}

Write-Host "[git] ensure branch=$Branch" -ForegroundColor Cyan
git -C $repo checkout -B $Branch | Out-Host

Write-Host "[git] add -A" -ForegroundColor Cyan
git -C $repo add -A | Out-Host

Write-Host "[git] commit (if needed)" -ForegroundColor Cyan
git -C $repo diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
  git -C $repo commit -m $CommitMessage | Out-Host
} else {
  Write-Host "[git] nothing to commit" -ForegroundColor DarkGray
}

if (-not $Push) {
  Write-Host "[git] skip push (-Push not set)" -ForegroundColor DarkGray
  exit 0
}

Write-Host "[git] push origin $Branch" -ForegroundColor Cyan
git -C $repo push origin $Branch | Out-Host

