<#
  Raven one-line installer (Windows / PowerShell).

    Remote (website):  iex (irm https://raven.evermind.ai/install.ps1)
    Local (dev clone):  git clone ... ; cd raven ; .\install.ps1

  Goal: a clean machine ends up able to run `raven` / `raven tui` from any
  directory with no manual steps. The script is idempotent -- it detects what
  is already present and only fills the gaps:
    1. uv            (Python toolchain + package manager)
    2. Node.js >= 22 (TUI runtime; installed privately if the system lacks it)
    3. raven         (installed as a global uv tool -> %USERPROFILE%\.local\bin\raven.exe)

  This is the Windows counterpart of install.sh (macOS / Linux). It mirrors the
  same three-stage, idempotent flow and the same private-Node layout that
  raven's find_node() looks for: %RAVEN_HOME%\runtime\node-vXX-win-<arch>\node.exe
#>

$ErrorActionPreference = 'Stop'

# --- config ----------------------------------------------------------------
$MinNodeMajor = 22
$RavenHome = if ($env:RAVEN_HOME) { $env:RAVEN_HOME } else { Join-Path $env:USERPROFILE '.raven' }
$NodeRuntimeDir = Join-Path $RavenHome 'runtime'

# --- pretty output ---------------------------------------------------------
function Write-Info($m) { Write-Host "> $m" -ForegroundColor Blue }
function Write-Ok($m)   { Write-Host "+ $m" -ForegroundColor Green }
function Write-Warn($m) { Write-Host "! $m" -ForegroundColor Yellow }
function Die($m)        { Write-Host "x $m" -ForegroundColor Red; exit 1 }
function Have($cmd)     { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# --- 0. platform detection -------------------------------------------------
function Get-NodeArch {
  $a = $env:PROCESSOR_ARCHITECTURE
  if ($a -eq 'AMD64' -or ($a -eq 'x86' -and $env:PROCESSOR_ARCHITEW6432 -eq 'AMD64')) { return 'x64' }
  if ($a -eq 'ARM64') { return 'arm64' }
  Die "Unsupported architecture: $a (only x64 / arm64 are supported)"
}

# --- 1. ensure uv ----------------------------------------------------------
function Ensure-Uv {
  if (Have 'uv') { Write-Ok "uv already installed ($(uv --version))"; return }
  Write-Info 'uv not found, installing...'
  # Official uv Windows installer; installs to %USERPROFILE%\.local\bin.
  powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
  # Make uv visible for the rest of this process before the shell is reloaded.
  $localBin = Join-Path $env:USERPROFILE '.local\bin'
  $env:Path = "$localBin;$env:Path"
  if (-not (Have 'uv')) { Die "uv still unavailable after install; check PATH (expected in $localBin)" }
  Write-Ok 'uv installed'
}

# --- 2. ensure Node >= 22 --------------------------------------------------
function Test-SystemNode {
  if (-not (Have 'node')) { return $false }
  try {
    $v = (node --version) -replace '^v', '' -replace '\..*', ''
    return ([int]$v -ge $MinNodeMajor)
  } catch { return $false }
}

function Get-LatestNodeV22 {
  try {
    $idx = Invoke-RestMethod -Uri 'https://nodejs.org/dist/index.json' -TimeoutSec 20
    $v = ($idx | Where-Object { $_.version -like 'v22.*' } | Select-Object -First 1).version
    if ($v) { return $v }
  } catch {}
  return 'v22.20.0'  # pinned fallback if the index can't be reached
}

# Return the path to a Raven-provisioned private node.exe, or $null. Actually
# run it: a half-extracted / corrupt binary exists but won't run and must not
# be mistaken for a ready runtime (else we'd never re-download).
function Get-PrivateNodeBin {
  if (-not (Test-Path $NodeRuntimeDir)) { return $null }
  $cands = Get-ChildItem -Path $NodeRuntimeDir -Filter 'node-v22*' -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { Join-Path $_.FullName 'node.exe' }
  foreach ($n in $cands) {
    if (Test-Path $n) {
      try { & $n --version *> $null; if ($LASTEXITCODE -eq 0) { return $n } } catch {}
    }
  }
  return $null
}

function Ensure-Node {
  if (Test-SystemNode) { Write-Ok "Node.js already meets requirement ($(node --version))"; return }
  $pn = Get-PrivateNodeBin
  if ($pn) { Write-Ok "Raven private Node already present ($pn)"; return }

  Write-Info "Node.js >= $MinNodeMajor not found; downloading a private runtime (does not touch the system)..."
  $arch = Get-NodeArch
  $ver = Get-LatestNodeV22
  $pkg = "node-$ver-win-$arch"
  $url = "https://nodejs.org/dist/$ver/$pkg.zip"
  New-Item -ItemType Directory -Force -Path $NodeRuntimeDir | Out-Null
  $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("raven-node-" + [System.Guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  $zip = Join-Path $tmp 'node.zip'
  Write-Info "  $url"
  try { Invoke-WebRequest -Uri $url -OutFile $zip -TimeoutSec 120 } catch { Die "Node download failed: $url" }

  # Supply-chain integrity: verify against the official SHASUMS256.txt.
  try {
    $shas = (Invoke-WebRequest -Uri "https://nodejs.org/dist/$ver/SHASUMS256.txt" -TimeoutSec 30).Content
    $expected = ($shas -split "`n" | Where-Object { $_ -match "  $pkg\.zip$" } | Select-Object -First 1) -split '\s+' | Select-Object -First 1
    if ($expected) {
      $actual = (Get-FileHash -Algorithm SHA256 -Path $zip).Hash.ToLower()
      if ($actual -ne $expected.ToLower()) {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
        Die "Node checksum mismatch (expected $expected, got $actual)"
      }
      Write-Ok 'Node zip SHA256 verified'
    } else { Write-Warn "SHASUMS256.txt did not list $pkg.zip; skipping verification" }
  } catch { Write-Warn 'Could not fetch SHASUMS256.txt; skipping integrity check' }

  Expand-Archive -Path $zip -DestinationPath $NodeRuntimeDir -Force
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
  $nodeExe = Join-Path $NodeRuntimeDir "$pkg\node.exe"
  if (-not (Test-Path $nodeExe)) { Die 'Node executable not found after extraction' }
  & $nodeExe --version *> $null
  if ($LASTEXITCODE -ne 0) { Die "Downloaded Node cannot run on this machine. Install Node >= $MinNodeMajor manually." }
  Write-Ok "Node private runtime ready: $(Join-Path $NodeRuntimeDir $pkg)"
  # raven's find_node() globs %RAVEN_HOME%\runtime\node-*\node.exe automatically,
  # so no PATH change is needed for `raven tui` to find it.
}

# --- 3. install raven ------------------------------------------------------
function Install-Raven {
  $scriptDir = $PSScriptRoot
  $pyproject = if ($scriptDir) { Join-Path $scriptDir 'pyproject.toml' } else { $null }
  $isLocal = $pyproject -and (Test-Path $pyproject) -and `
             (Select-String -Path $pyproject -Pattern '^name = "raven"' -Quiet -ErrorAction SilentlyContinue)

  if ($isLocal) {
    Write-Info "Local raven source detected; editable install: $scriptDir"
    $entry = Join-Path $scriptDir 'ui-tui\dist\entry.js'
    if (-not (Test-Path $entry)) {
      # Build the TUI bundle now (dev checkout doesn't commit it).
      $nodeBin = (Get-Command node -ErrorAction SilentlyContinue).Source
      if (-not $nodeBin) { $nodeBin = Get-PrivateNodeBin }
      if ($nodeBin -and (Test-Path $nodeBin)) {
        $nodeDir = Split-Path $nodeBin
        $npm = Join-Path $nodeDir 'npm.cmd'
        if (Test-Path $npm) {
          Write-Info 'Building the TUI bundle (ui-tui\dist\entry.js)...'
          # npm runs lifecycle scripts that spawn `node` from PATH. When the only
          # Node is the private runtime (the case that triggered the download),
          # it is not on PATH, so prepend its dir for the build -- parity with
          # install.sh, which exports PATH="$node_dir:$PATH" around the npm calls.
          $savedPath = $env:Path
          $env:Path = "$nodeDir;$env:Path"
          Push-Location (Join-Path $scriptDir 'ui-tui')
          try { & $npm ci; & $npm run build } finally { Pop-Location; $env:Path = $savedPath }
        } else { Write-Warn 'Found node but not npm; skipping TUI build; raven tui may not work' }
      } else { Write-Warn 'No usable node found; skipping TUI build; raven tui may not work' }
    }
    uv tool install --force -e "$scriptDir"
  } else {
    # Remote mode: install the latest published release wheel, which bundles the
    # prebuilt ui-tui/dist/entry.js (built by CI). We deliberately do NOT install
    # from git -- the TUI bundle is a gitignored build artifact, so a git install
    # would yield a raven whose `raven tui` cannot start. Override RAVEN_WHEEL_URL
    # to pin a specific wheel.
    $wheelUrl = $env:RAVEN_WHEEL_URL
    if (-not $wheelUrl) {
      Write-Info 'Resolving the latest raven release from GitHub...'
      try {
        $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/EverMind-AI/raven/releases/latest' `
                 -Headers @{ 'User-Agent' = 'raven-installer' } -TimeoutSec 30
        $wheelUrl = ($rel.assets | Where-Object { $_.name -like '*.whl' } | Select-Object -First 1).browser_download_url
      } catch {}
    }
    if (-not $wheelUrl) { Die 'Could not resolve the latest raven release wheel from GitHub (check network, or set RAVEN_WHEEL_URL to a wheel URL).' }
    Write-Info "  installing $wheelUrl"
    uv tool install --force "$wheelUrl"
  }
  # Ensure the uv tool bin dir is on PATH for future shells.
  try { uv tool update-shell } catch {}
  Write-Ok 'raven installed'
}

# --- main ------------------------------------------------------------------
function Main {
  Get-NodeArch | Out-Null   # fail fast on unsupported arch
  Ensure-Uv
  Ensure-Node
  Install-Raven

  Write-Host ''
  Write-Ok 'All set! Open a new terminal (so PATH refreshes), then run:'
  Write-Host ''
  Write-Host '    raven            # enter the TUI' -ForegroundColor White
  Write-Host '    raven agent -m "hello"' -ForegroundColor White
  Write-Host ''
  $localBin = Join-Path $env:USERPROFILE '.local\bin'
  if ($env:Path -notlike "*$localBin*") {
    Write-Warn "Your current PATH does not include $localBin yet -- open a new terminal, or run: `$env:Path = `"$localBin;`$env:Path`""
  }
}

Main
