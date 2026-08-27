# Raven one-line installer for native Windows PowerShell.
#
# Remote:
#   irm https://raven.evermind.ai/install.ps1 | iex
#
# A piped run always installs the published release wheel, even from inside a
# clone. Set RAVEN_LOCAL_SRC=<dir> to force an editable install of a checkout.
#
# Goal: a clean Windows machine ends up able to run `raven` / `raven tui`
# without admin rights. The script is idempotent: it reuses existing tools when
# available and only fills the gaps:
#   1. uv            (Python toolchain + package manager)
#   2. Node.js >= 22 (TUI runtime; installed privately if the system lacks it)
#   3. raven         (installed as a global uv tool)

$ErrorActionPreference = "Stop"

$MinNodeMajor = 22
$RavenHome = if ($env:RAVEN_HOME) { $env:RAVEN_HOME } else { Join-Path $HOME ".raven" }
$NodeRuntimeDir = Join-Path $RavenHome "runtime"

function Write-Info([string]$Message) {
    Write-Host ">" $Message -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "OK" $Message -ForegroundColor Green
}

function Write-Warn([string]$Message) {
    Write-Warning $Message
}

function Fail([string]$Message) {
    Write-Error $Message
    exit 1
}

function Add-ProcessPath([string]$PathToAdd) {
    if (-not $PathToAdd) { return }
    if (-not (Test-Path $PathToAdd)) { return }
    $parts = $env:PATH -split ';'
    if ($parts -notcontains $PathToAdd) {
        $env:PATH = "$PathToAdd;$env:PATH"
    }
}

function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        (Join-Path $HOME ".local\bin\uv.exe"),
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

function Ensure-Uv {
    $uv = Find-Uv
    if ($uv) {
        Write-Ok "uv is installed ($(& $uv --version))"
        Add-ProcessPath (Split-Path $uv -Parent)
        return $uv
    }

    Write-Info "uv not found; installing..."
    Invoke-Expression (Invoke-RestMethod "https://astral.sh/uv/install.ps1")
    $uv = Find-Uv
    if (-not $uv) {
        Fail "uv was installed but is still not available. Check PATH (expected ~/.local/bin)."
    }
    Add-ProcessPath (Split-Path $uv -Parent)
    Write-Ok "uv installed"
    return $uv
}

function Get-NodeArch {
    switch ($env:PROCESSOR_ARCHITECTURE) {
        "ARM64" { return "arm64" }
        "AMD64" { return "x64" }
        default { Fail "Unsupported Windows architecture: $env:PROCESSOR_ARCHITECTURE" }
    }
}

function Test-NodeOk([string]$NodePath) {
    if (-not $NodePath) { return $false }
    if (-not (Test-Path $NodePath)) { return $false }
    try {
        $version = (& $NodePath --version).Trim()
        $major = [int](($version.TrimStart("v") -split "\.")[0])
        return $major -ge $MinNodeMajor
    } catch {
        return $false
    }
}

function Find-PrivateNode {
    $candidates = @()
    $direct = Join-Path $NodeRuntimeDir "node\node.exe"
    $directBin = Join-Path $NodeRuntimeDir "node\bin\node.exe"
    if (Test-Path $direct) { $candidates += $direct }
    if (Test-Path $directBin) { $candidates += $directBin }
    if (Test-Path $NodeRuntimeDir) {
        $candidates += Get-ChildItem $NodeRuntimeDir -Directory -Filter "node-v22*" -ErrorAction SilentlyContinue |
            ForEach-Object {
                @(
                    (Join-Path $_.FullName "node.exe"),
                    (Join-Path $_.FullName "bin\node.exe")
                )
            }
    }
    foreach ($candidate in $candidates) {
        if (Test-NodeOk $candidate) { return $candidate }
    }
    return $null
}

function Get-LatestNodeV22 {
    try {
        $index = Invoke-RestMethod "https://nodejs.org/dist/index.json"
        $entry = $index | Where-Object { $_.version -like "v22.*" } | Select-Object -First 1
        if ($entry -and $entry.version) { return $entry.version }
    } catch {
        Write-Warn "Could not query Node.js release index; falling back to v22.20.0"
    }
    return "v22.20.0"
}

function Ensure-Node {
    $systemNode = Get-Command node -ErrorAction SilentlyContinue
    if ($systemNode -and (Test-NodeOk $systemNode.Source)) {
        Write-Ok "Node.js meets requirements ($(& $systemNode.Source --version))"
        return $systemNode.Source
    }

    $privateNode = Find-PrivateNode
    if ($privateNode) {
        Write-Ok "Existing Raven private Node found ($privateNode)"
        Add-ProcessPath (Split-Path $privateNode -Parent)
        return $privateNode
    }

    Write-Info "Node.js >= $MinNodeMajor not found; downloading private runtime..."
    $arch = Get-NodeArch
    $version = Get-LatestNodeV22
    $pkg = "node-$version-win-$arch"
    $url = "https://nodejs.org/dist/$version/$pkg.zip"
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("raven-node-" + [guid]::NewGuid().ToString("N"))
    $zipPath = Join-Path $tmp "node.zip"

    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    New-Item -ItemType Directory -Path $NodeRuntimeDir -Force | Out-Null

    try {
        Write-Info "  $url"
        Invoke-WebRequest $url -OutFile $zipPath

        try {
            $sums = (Invoke-WebRequest "https://nodejs.org/dist/$version/SHASUMS256.txt").Content
            $line = ($sums -split "`n") | Where-Object { $_ -match "\s+$([regex]::Escape("$pkg.zip"))$" } | Select-Object -First 1
            if ($line) {
                $expected = (($line.Trim()) -split "\s+")[0].ToLowerInvariant()
                $actual = (Get-FileHash $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
                if ($expected -ne $actual) {
                    Fail "Node checksum mismatch (expected $expected, got $actual)."
                }
                Write-Ok "Node zip SHA256 verified"
            } else {
                Write-Warn "SHASUMS256.txt did not list $pkg.zip; skipping checksum verification"
            }
        } catch {
            Write-Warn "Could not verify Node checksum; continuing"
        }

        Expand-Archive $zipPath -DestinationPath $tmp -Force
        $src = Join-Path $tmp $pkg
        $dest = Join-Path $NodeRuntimeDir $pkg
        if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
        Move-Item $src $dest

        $node = Join-Path $dest "node.exe"
        if (-not (Test-NodeOk $node)) {
            Fail "Downloaded Node runtime is not usable on this machine."
        }
        Add-ProcessPath $dest
        Write-Ok "Node private runtime ready: $dest"
        return $node
    } finally {
        if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

# Reads the latest stable tag off the release page redirect. The GitHub API caps
# unauthenticated callers at 60 requests/hour per IP, which a shared egress can
# exhaust; the release page carries no API quota. Returns "" when the redirect is
# missing or does not name a stable tag, so the caller can fail with its own message.
function Resolve-RavenLatestVersion {
    $target = ""
    try {
        $response = Invoke-WebRequest "https://github.com/EverMind-AI/Raven/releases/latest" -MaximumRedirection 0 -UseBasicParsing -ErrorAction Stop
        $target = [string]$response.Headers.Location
    } catch {
        # Windows PowerShell raises on an unfollowed redirect; the Location header
        # still rides on the exception's response.
        $failed = $_.Exception.Response
        if ($failed) {
            try { $target = [string]$failed.Headers.Location } catch { $target = "" }
            if (-not $target) {
                try { $target = [string]$failed.Headers.GetValues("Location")[0] } catch { $target = "" }
            }
        }
    }
    if ($target -match "^https://github\.com/EverMind-AI/Raven/releases/tag/v([0-9]+\.[0-9]+\.[0-9]+)$") {
        return $Matches[1]
    }
    return ""
}

function Resolve-RavenWheel {
    if ($env:RAVEN_WHEEL_URL) { return $env:RAVEN_WHEEL_URL }
    Write-Info "Resolving the latest Raven release from GitHub..."
    try {
        $release = Invoke-RestMethod "https://api.github.com/repos/EverMind-AI/Raven/releases/latest" -Headers @{ "User-Agent" = "raven-installer" }
        $asset = $release.assets | Where-Object { $_.browser_download_url -match "/raven-[^/]+\.whl$" } | Select-Object -First 1
        if ($asset) { return $asset.browser_download_url }
        Write-Warn "GitHub API returned no release wheel; falling back to the release page."
    } catch {
        Write-Warn "GitHub API lookup failed ($($_.Exception.Message)); falling back to the release page."
    }
    $version = Resolve-RavenLatestVersion
    if (-not $version) {
        Fail "Could not resolve the latest Raven release wheel from GitHub. Retry later, or set RAVEN_WHEEL_URL to a wheel URL."
    }
    return "https://github.com/EverMind-AI/Raven/releases/download/v$version/raven-$version-py3-none-any.whl"
}

function Resolve-RavenConstraints([string]$WheelUrl) {
    # Derive the locked-constraints URL from the wheel URL (same release dir) so
    # the constraints always match the wheel being installed -- including when
    # RAVEN_WHEEL_URL pins an older wheel. Returns a local temp-file path, or
    # $null when the asset is absent (release predates it) or the download fails,
    # so the installer degrades to an unconstrained install rather than failing.
    $url = $env:RAVEN_CONSTRAINTS_URL
    if (-not $url) {
        if ($WheelUrl -notmatch "/[^/]+\.whl$") { return $null }
        $url = $WheelUrl -replace "/[^/]+\.whl$", "/raven-constraints.txt"
    }
    $dest = Join-Path ([IO.Path]::GetTempPath()) ("raven-constraints-" + [guid]::NewGuid().ToString("N") + ".txt")
    try {
        Invoke-WebRequest $url -OutFile $dest
    } catch {
        Write-Warn "Could not download locked constraints; installing without version pinning."
        return $null
    }
    return $dest
}

function Test-RavenSource([string]$Dir) {
    if (-not $Dir) { return $false }
    $pyproject = Join-Path $Dir "pyproject.toml"
    return (Test-Path $pyproject) -and (Select-String -Path $pyproject -Pattern '^name = "raven"' -Quiet)
}

function Install-Raven([string]$UvPath, [string]$NodePath) {
    # $PSScriptRoot is set only when this script runs as a file. Piped through
    # `irm ... | iex` it is empty, and falling back to the current directory
    # turns a one-line install started from inside a clone into a silent
    # editable install of that working tree. So local mode requires
    # $PSScriptRoot; RAVEN_LOCAL_SRC is the explicit opt-in for a piped run.
    $scriptDir = $null
    if ($env:RAVEN_LOCAL_SRC) {
        $resolved = Resolve-Path -LiteralPath $env:RAVEN_LOCAL_SRC -ErrorAction SilentlyContinue
        if (-not $resolved) { Fail "RAVEN_LOCAL_SRC is not a directory: $($env:RAVEN_LOCAL_SRC)" }
        $scriptDir = $resolved.Path
        if (-not (Test-RavenSource $scriptDir)) { Fail "RAVEN_LOCAL_SRC is not a Raven source checkout: $scriptDir" }
    } elseif ($PSScriptRoot -and (Test-RavenSource $PSScriptRoot)) {
        $scriptDir = $PSScriptRoot
    }
    if ($scriptDir) {
        Write-Info "Detected local Raven source checkout; installing editable: $scriptDir"
        $entry = Join-Path $scriptDir "ui-tui\dist\entry.js"
        if (-not (Test-Path $entry)) {
            $nodeDir = Split-Path $NodePath -Parent
            Add-ProcessPath $nodeDir
            $npm = Get-Command npm -ErrorAction SilentlyContinue
            if ($npm) {
                Write-Info "Building TUI bundle (ui-tui/dist/entry.js)..."
                Push-Location (Join-Path $scriptDir "ui-tui")
                try {
                    & $npm.Source ci
                    & $npm.Source run build
                } finally {
                    Pop-Location
                }
            } else {
                Write-Warn "Found node but not npm; skipping TUI bundle build"
            }
        }
        # Pin to the locked dependency set so an install matches what we test.
        $constraints = Join-Path ([IO.Path]::GetTempPath()) ("raven-constraints-" + [guid]::NewGuid().ToString("N") + ".txt")
        & $UvPath export --directory "$scriptDir" --frozen --all-extras --no-hashes --no-emit-project -o "$constraints"
        # Install all channel adapters by default; fall back to base raven if
        # the umbrella extra fails to build on this platform, so one broken
        # channel SDK cannot block the whole install.
        try {
            & $UvPath tool install --force -c "$constraints" -e "$scriptDir[channels]"
            if ($LASTEXITCODE -ne 0) { throw "channel extras install failed" }
        } catch {
            Write-Warn "Channel dependencies failed to install; installed base raven only. Some channels stay unavailable (see: raven channels list)."
            & $UvPath tool install --force -c "$constraints" -e "$scriptDir"
            if ($LASTEXITCODE -ne 0) { Fail "Raven install failed." }
        }
    } else {
        $wheelUrl = Resolve-RavenWheel
        $constraints = Resolve-RavenConstraints $wheelUrl
        if ($constraints) {
            $cArgs = @("-c", $constraints)
        } else {
            Write-Warn "Release has no locked-constraints asset; installing without version pinning."
            $cArgs = @()
        }
        Write-Info "  installing $wheelUrl"
        try {
            & $UvPath tool install --force @cArgs "raven[channels] @ $wheelUrl"
            if ($LASTEXITCODE -ne 0) { throw "channel extras install failed" }
        } catch {
            Write-Warn "Channel dependencies failed to install; installed base raven only. Some channels stay unavailable (see: raven channels list)."
            & $UvPath tool install --force @cArgs $wheelUrl
            if ($LASTEXITCODE -ne 0) { Fail "Raven install failed." }
        }
    }
    & $UvPath tool update-shell | Out-Null
    Write-Ok "Raven installed"
}

function Main {
    # Read before installing so the closing hint can tell a first run from an
    # upgrade; the install itself never writes config.json (the wizard does).
    $hadConfig = Test-Path (Join-Path $RavenHome "config.json")

    $uv = Ensure-Uv
    $node = Ensure-Node
    Install-Raven $uv $node

    $toolBin = Join-Path $HOME ".local\bin"
    Add-ProcessPath $toolBin

    Write-Host ""
    if ($hadConfig) {
        Write-Ok "Raven updated. Your config in $RavenHome is unchanged."
        Write-Host ""
        Write-Host "    raven    # continue where you left off"
        Write-Host ""
        Write-Host "  tip: next time you can upgrade in place with 'raven upgrade'"
        Write-Host ""
    } else {
        Write-Ok "All set. Open a new PowerShell window, or continue in this one, then run:"
        Write-Host ""
        Write-Host "    raven    # sets you up on first run, then opens the TUI"
        Write-Host ""
    }
    if (($env:PATH -split ';') -notcontains $toolBin) {
        Write-Warn "Current PATH does not include $toolBin. Restart PowerShell if 'raven' is not found."
    }
}

Main
