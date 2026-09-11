# Raven one-line installer for native Windows PowerShell.
#
# Remote:
#   irm https://raven.evermind.ai/install.ps1 | iex
#
# A piped run always installs the published release wheel, even from inside a
# clone. Set RAVEN_LOCAL_SRC=<dir> to force an editable install of a checkout.
# Set RAVEN_MINIMAL=1 to skip the chromium download and the LibreOffice offer;
# the wheel install itself is unchanged.
#
# Goal: a clean Windows machine ends up able to run `raven` / `raven tui`
# without admin rights. The script is idempotent: it reuses existing tools when
# available and only fills the gaps:
#   1. uv            (Python toolchain + package manager)
#   2. Node.js >= 22 (TUI runtime; installed privately if the system lacks it)
#   3. raven         (installed as a global uv tool)
#   4. chromium      (browser-tool runtime; downloaded by playwright)
#   5. LibreOffice   (deck preview; offered via winget)

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
        # Kept for Resolve-RavenPluginWheel, which reads the same asset list.
        # The GitHub API caps unauthenticated callers at 60 requests/hour per
        # IP, so a shared egress can exhaust it -- one lookup serves them all.
        $script:RavenRelease = $release
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

function Resolve-RavenPluginWheel([string]$FilePrefix) {
    # A product engine's wheel from the same release, or $null. Absent is not a
    # failure: the release still installs and the roster reports the product
    # disabled, which is what it already does when the package is missing.
    # Reads the asset list Resolve-RavenWheel cached rather than calling the
    # API again; a release resolved through the page fallback leaves no cache,
    # and the engine is then treated as absent because nothing lists it.
    if (-not $script:RavenRelease) { return $null }
    $asset = $script:RavenRelease.assets |
        Where-Object { $_.browser_download_url -match "/$FilePrefix-[^/]+\.whl$" } |
        Select-Object -First 1
    if ($asset) { return $asset.browser_download_url }
    return $null
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

# ui-tui\dist\entry.js (the TUI bundle) and ui\dist\index.html (the page
# `raven web` serves) are both gitignored build artifacts. A release wheel
# carries them; an editable install of a checkout gets neither, so without this
# a clone install has no TUI and no page. Both must exist before first run.
function Build-WebAssets([string]$ScriptDir, [string]$NodePath, [string]$UvPath) {
    $needTui = -not (Test-Path (Join-Path $ScriptDir "ui-tui\dist\entry.js"))
    $needPage = -not (Test-Path (Join-Path $ScriptDir "ui-web\dist\index.html"))
    if (-not ($needTui -or $needPage)) { return }

    # One probe for both builds. npm ships alongside node, but verify it
    # explicitly before relying on it.
    Add-ProcessPath (Split-Path $NodePath -Parent)
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npm) {
        if ($needTui) { Write-Warn "Found node but not npm; skipping TUI bundle build" }
        if ($needPage) { Write-Warn "Found node but not npm; skipping served-page build; raven web will not start" }
        return
    }

    if ($needTui) {
        Write-Info "Building TUI bundle (ui-tui/dist/entry.js)..."
        Push-Location (Join-Path $ScriptDir "ui-tui")
        # Fatal, and each exit code read back for the same reason the page below
        # reads its own: $ErrorActionPreference does not cover native commands,
        # so an unchecked npm failure returns here, runs the next npm anyway, and
        # the install goes on to report success with no bundle -- leaving bare
        # `raven`, which opens the TUI, unusable. install.sh aborts here under
        # `set -e`; this is the same abort, and `Fail` raises a terminating error
        # rather than exiting the process, so `irm | iex` does not close the
        # caller's shell and Pop-Location still runs.
        try {
            & $npm.Source ci
            if ($LASTEXITCODE -ne 0) { Fail "TUI bundle build failed: npm ci exited $LASTEXITCODE" }
            & $npm.Source run build
            if ($LASTEXITCODE -ne 0) { Fail "TUI bundle build failed: npm run build exited $LASTEXITCODE" }
        } finally {
            Pop-Location
        }
    }

    if ($needPage) {
        Write-Info "Building served page (ui-web/dist/index.html)..."
        # Warned rather than propagated, unlike the bundle above: bare `raven`
        # opens the TUI, so a machine that cannot build the page still gets the
        # surface this script exists to deliver. Each exit code is read back
        # because $ErrorActionPreference does not cover native commands, so a
        # failed npm ci would otherwise run on into the page assembler.
        try {
            Push-Location (Join-Path $ScriptDir "ui-web")
            try {
                & $npm.Source ci
                if ($LASTEXITCODE -ne 0) { throw "npm ci exited $LASTEXITCODE" }
                & $npm.Source run build
                if ($LASTEXITCODE -ne 0) { throw "npm run build exited $LASTEXITCODE" }
            } finally {
                Pop-Location
            }
            # Vite emits ui-web/.modern/modern.iife.js, then ui-web/build.py inlines
            # with the page sources and the shared i18n catalogue. Python comes
            # from uv, already a hard requirement here, rather than from a bare
            # `python` -- on Windows that name is usually the Microsoft Store
            # stub, which opens the Store instead of running the script.
            & $UvPath run --no-project python (Join-Path $ScriptDir "ui-web\build.py")
            if ($LASTEXITCODE -ne 0) { throw "ui-web/build.py exited $LASTEXITCODE" }
        } catch {
            Write-Warn "The served page did not build ($_); raven web will not start"
        }
    }
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
        Build-WebAssets $scriptDir $NodePath $UvPath
        # Pin to the locked dependency set so an install matches what we test.
        $constraints = Join-Path ([IO.Path]::GetTempPath()) ("raven-constraints-" + [guid]::NewGuid().ToString("N") + ".txt")
        & $UvPath export --directory "$scriptDir" --frozen --all-extras --no-hashes --no-emit-workspace -o "$constraints"
        # Raven-Design and Raven-PPT keep their harness in their own
        # distributions, and the roster gates on them: discovery reads the
        # `engine` block in each agents/<product>/subagent.json and disables the
        # row when that package is not importable where raven runs. Without
        # these two the products are listed and cannot be dispatched to.
        $enginePlugins = @(
            "--with-editable", (Join-Path $scriptDir "plugins-dist\design-engine"),
            "--with-editable", (Join-Path $scriptDir "plugins-dist\ppt-engine")
        )
        # Install all channel adapters by default; fall back to base raven if
        # the umbrella extra fails to build on this platform, so one broken
        # channel SDK cannot block the whole install. The engines fall before
        # raven itself for the same reason: they carry native builds a platform
        # can refuse on its own.
        try {
            & $UvPath tool install --force -c "$constraints" @enginePlugins -e "$scriptDir[channels]"
            if ($LASTEXITCODE -ne 0) { throw "channel extras install failed" }
        } catch {
            Write-Warn "Channel dependencies failed to install; retrying with base raven. Some channels stay unavailable (see: raven channels list)."
            & $UvPath tool install --force -c "$constraints" @enginePlugins -e "$scriptDir"
            if ($LASTEXITCODE -ne 0) {
                Write-Warn "A product engine failed to build; Raven-Design and Raven-PPT stay disabled (raven doctor explains)."
                & $UvPath tool install --force -c "$constraints" -e "$scriptDir"
                if ($LASTEXITCODE -ne 0) { Fail "Raven install failed." }
            }
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
        # The product engines ship as their own wheels from the same release.
        # Absent ones are a warning, not a failure: the release still installs
        # and Raven-Design / Raven-PPT stay disabled the way discovery already
        # reports them.
        $pArgs = @()
        foreach ($engine in @(
            @{ Prefix = "design_engine"; Package = "design-engine"; Label = "design engine"; Product = "Raven-Design" },
            @{ Prefix = "ppt_engine";    Package = "ppt-engine";    Label = "deck engine";   Product = "Raven-PPT" }
        )) {
            $url = Resolve-RavenPluginWheel $engine.Prefix
            if ($url) {
                $pArgs += @("--with", "$($engine.Package)@$url")
                Write-Info "  with $($engine.Label) $url"
            } else {
                Write-Warn "This release carries no $($engine.Package) wheel; $($engine.Product) stays disabled (raven doctor explains)."
            }
        }
        try {
            & $UvPath tool install --force @cArgs @pArgs "raven[channels] @ $wheelUrl"
            if ($LASTEXITCODE -ne 0) { throw "channel extras install failed" }
        } catch {
            Write-Warn "Channel dependencies failed to install; retrying with base raven. Some channels stay unavailable (see: raven channels list)."
            & $UvPath tool install --force @cArgs @pArgs $wheelUrl
            if ($LASTEXITCODE -ne 0) {
                Write-Warn "A product engine failed to install; Raven-Design and Raven-PPT stay disabled (raven doctor explains)."
                & $UvPath tool install --force @cArgs $wheelUrl
                if ($LASTEXITCODE -ne 0) { Fail "Raven install failed." }
            }
        }
    }
    & $UvPath tool update-shell | Out-Null
    Write-Ok "Raven installed"
}

# Both optional installs are best-effort: raven itself is already installed by
# the time they run, so a failed download or a declined offer must never abort
# a completed install. RAVEN_MINIMAL skips both.
function Install-Browser([string]$UvPath) {
    # The browser tool drives chromium through the playwright library inside
    # the raven tool venv, so both the probe and the download must use that
    # venv's python -- the system python knows nothing about this install.
    $toolDir = ""
    try { $toolDir = [string](& $UvPath tool dir 2>$null) } catch { $toolDir = "" }
    $py = if ($toolDir) { Join-Path $toolDir "raven\Scripts\python.exe" } else { $null }
    if (-not $py -or -not (Test-Path $py)) {
        Write-Warn "raven tool venv python not found; skipping the chromium download."
        return
    }
    # A pinned RAVEN_WHEEL_URL and the release-page fallback install no engine
    # wheels, so playwright can be absent even after a green install. Windows
    # PowerShell turns redirected native stderr into a terminating error under
    # $ErrorActionPreference = "Stop", so a failed import lands in the catch.
    try {
        & $py -c "import playwright" 2>$null
        if ($LASTEXITCODE -ne 0) { throw "playwright is not importable" }
    } catch {
        Write-Warn "This install carries no browser library (a pinned wheel URL or the release-page fallback installs no engines); the browser tool stays off."
        return
    }
    Write-Info "Downloading chromium for the browser tool..."
    try {
        & $py -m playwright install chromium
        if ($LASTEXITCODE -ne 0) { throw "playwright install chromium exited $LASTEXITCODE" }
    } catch {
        Write-Warn "Chromium download failed; the browser tool stays off. Retry later with: $py -m playwright install chromium"
    }
}

function Install-Office {
    # soffice and libreoffice are the two launcher names the runtime resolves
    # (raven/utils/office.py); either one means deck preview already works. The
    # winget MSI registers no PATH entry, so also probe the install roots
    # find_soffice reads before deciding LibreOffice is absent.
    if (Get-Command soffice, libreoffice -ErrorAction SilentlyContinue) { return }
    foreach ($root in @($env:ProgramFiles, ${env:ProgramW6432}, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if ($root -and (Test-Path (Join-Path $root "LibreOffice\program\soffice.exe"))) { return }
    }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Warn "LibreOffice not found; deck preview stays off. Install it later with: winget install TheDocumentFoundation.LibreOffice"
        return
    }
    # Installing can raise a UAC prompt, so ask first -- and only when a real
    # console is attached: under `irm | iex` Read-Host still reads the console,
    # but CI has none, and a prompt there must skip cleanly, never hang.
    if (-not ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected)) {
        Write-Warn "LibreOffice not found; deck preview stays off. Install it later with: winget install TheDocumentFoundation.LibreOffice"
        return
    }
    $answer = Read-Host "Install LibreOffice for deck preview (may raise a UAC prompt)? [y/N]"
    if ($answer -notmatch "^[yY]$") {
        Write-Warn "Skipping LibreOffice; deck preview stays off. Install it later with: winget install TheDocumentFoundation.LibreOffice"
        return
    }
    try {
        winget install TheDocumentFoundation.LibreOffice
        if ($LASTEXITCODE -ne 0) { throw "winget exited $LASTEXITCODE" }
    } catch {
        Write-Warn "LibreOffice install failed; deck preview stays off. Retry later with: winget install TheDocumentFoundation.LibreOffice"
    }
}

# One mouth for what actually landed: `raven doctor --install-summary` reads
# only what is importable/installed, needs no config, and always exits 0. The
# raven shim lands in `uv tool dir --bin`, which this session's PATH may not
# carry yet, so invoke it by absolute path. Purely informational -- every
# branch degrades to a warning so it can never fail a completed install.
function Show-CapabilitySummary([string]$UvPath) {
    $binDir = ""
    try { $binDir = [string](& $UvPath tool dir --bin 2>$null) } catch { $binDir = "" }
    $bin = if ($binDir) { Join-Path $binDir "raven.exe" } else { $null }
    if (-not $bin -or -not (Test-Path $bin)) { $bin = Join-Path $HOME ".local\bin\raven.exe" }
    if (-not (Test-Path $bin)) { return }
    Write-Host ""
    Write-Info "Capabilities:"
    try {
        & $bin doctor --install-summary
        if ($LASTEXITCODE -ne 0) { throw "raven doctor exited $LASTEXITCODE" }
    } catch {
        Write-Warn "capability summary unavailable (raven doctor failed)"
    }
}

function Main {
    # Read before installing so the closing hint can tell a first run from an
    # upgrade; the install itself never writes config.json (the wizard does).
    $hadConfig = Test-Path (Join-Path $RavenHome "config.json")

    $uv = Ensure-Uv
    $node = Ensure-Node
    Install-Raven $uv $node

    # The summary is not gated: a minimal install still sees what it skipped.
    if (-not $env:RAVEN_MINIMAL) { Install-Browser $uv }
    if (-not $env:RAVEN_MINIMAL) { Install-Office }
    Show-CapabilitySummary $uv

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
