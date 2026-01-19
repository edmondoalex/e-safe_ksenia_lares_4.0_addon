param(
  [Parameter(Mandatory = $true)]
  [string]$Source,

  [Parameter(Mandatory = $true)]
  [string]$Dest,

  [switch]$DryRun
)

$src = (Resolve-Path -LiteralPath $Source).Path
$dst = $Dest

if (-not (Test-Path -LiteralPath $dst)) {
  New-Item -ItemType Directory -Path $dst | Out-Null
}

$excludeDirs = @(
  ".git",
  "__pycache__",
  "app\\__pycache__",
  "tools\\__pycache__",
  "data"
)

$excludeFiles = @(
  "*.pyc",
  "*.pyo",
  ".DS_Store",
  "Thumbs.db"
)

$args = @(
  "`"$src`"",
  "`"$dst`"",
  "/MIR",
  "/FFT",
  "/Z",
  "/R:2",
  "/W:2",
  "/MT:16",
  "/XD"
) + $excludeDirs + @(
  "/XF"
) + $excludeFiles

if ($DryRun) {
  $args += "/L"
  Write-Host "[DRYRUN] robocopy $($args -join ' ')" -ForegroundColor Yellow
} else {
  Write-Host "[RUN] robocopy $($args -join ' ')" -ForegroundColor Cyan
}

& robocopy @args | Out-Host
$code = $LASTEXITCODE

# Robocopy exit codes: 0-7 are success (with different meanings).
if ($code -le 7) {
  Write-Host "OK (robocopy exit code=$code)"
  exit 0
}

Write-Error "FAIL (robocopy exit code=$code)"
exit $code

