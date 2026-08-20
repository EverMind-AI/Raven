# Raven one-line installer for native Windows PowerShell.
#
# Remote:
#   irm https://raven.evermind.ai/install.ps1 | iex
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

function Resolve-RavenWheel {
    if ($env:RAVEN_WHEEL_URL) { return $env:RAVEN_WHEEL_URL }
    Write-Info "Resolving the latest Raven release from GitHub..."
    $release = Invoke-RestMethod "https://api.github.com/repos/EverMind-AI/Raven/releases/latest" -Headers @{ "User-Agent" = "raven-installer" }
    $asset = $release.assets | Where-Object { $_.browser_download_url -match "/raven-[^/]+\.whl$" } | Select-Object -First 1
    if (-not $asset) {
        Fail "Could not resolve the latest Raven release wheel from GitHub. Set RAVEN_WHEEL_URL to a wheel URL."
    }
    return $asset.browser_download_url
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

function Install-Raven([string]$UvPath, [string]$NodePath) {
    $scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
    $pyproject = Join-Path $scriptDir "pyproject.toml"
    if ((Test-Path $pyproject) -and (Select-String -Path $pyproject -Pattern '^name = "raven"' -Quiet)) {
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
    $uv = Ensure-Uv
    $node = Ensure-Node
    Install-Raven $uv $node

    $toolBin = Join-Path $HOME ".local\bin"
    Add-ProcessPath $toolBin

    Write-Host ""
    Write-Ok "All set. Open a new PowerShell window, or continue in this one, then run:"
    Write-Host ""
    Write-Host "    raven            # enter the TUI"
    Write-Host "    raven agent -m `"hello`""
    Write-Host ""
    if (($env:PATH -split ';') -notcontains $toolBin) {
        Write-Warn "Current PATH does not include $toolBin. Restart PowerShell if 'raven' is not found."
    }
}

Main
