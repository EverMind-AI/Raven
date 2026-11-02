# Raven one-line installer for native Windows PowerShell.
#
# Remote:
#   irm https://raven.evermind.ai/install.ps1 | iex
#
# A piped run always installs the published release wheel, even from inside a
# clone. Set RAVEN_LOCAL_SRC=<dir> to force an editable install of a checkout.
# Set RAVEN_MINIMAL=1 to skip the chromium download and the LibreOffice offer;
# the wheel install itself is unchanged. Set RAVEN_NO_LAUNCH=1 to skip the
# closing `raven web` (CI), so the script returns. A download whose host cannot
# be reached at all is fetched from a mirror in mainland China instead; set
# RAVEN_NO_MIRROR=1 to keep every download on its default host.
#
# Goal: a clean Windows machine ends up able to run `raven` / `raven tui`
# without admin rights. The script is idempotent: it reuses existing tools when
# available and only fills the gaps:
#   1. uv            (Python toolchain + package manager)
#   2. Node.js >= 22 (TUI runtime; installed privately if the system lacks it)
#   3. raven         (installed as a global uv tool)
#   4. chromium      (browser-tool runtime; downloaded by playwright)
#   5. LibreOffice   (deck preview; offered via winget)
#
# It then ends in the product: `raven web` opens the page in a browser and holds
# this session, so the install finishes on something running rather than on a
# hint to go and start it.
#
# Probe rule: this script is served from main and installs the latest release,
# which can predate a subcommand main already knows about. Every `raven <sub>`
# call below is preceded by `raven <sub> --help`; when the probe fails, the
# script finishes on the one command every release has.

$ErrorActionPreference = "Stop"

$MinNodeMajor = 22
$RavenHome = if ($env:RAVEN_HOME) { $env:RAVEN_HOME } else { Join-Path $HOME ".raven" }
$NodeRuntimeDir = Join-Path $RavenHome "runtime"

# Where a download goes when its default host cannot be reached at all. From a
# mainland-China line with no proxy, GitHub and huggingface.co time out more
# often than they answer, and astral.sh, PyPI, nodejs.org or the npm registry
# can fail the same way. Only a connection that never completed moves a
# download (Test-Reachable); an HTTP error is the host's answer and stays final.
# A host the user chose is never replaced, and RAVEN_NO_MIRROR=1 keeps every
# download on its default host. raven/utils/mirrors.py names the same hosts for
# the runtime; a test holds the copies equal.
$NodeDist = "https://nodejs.org/dist"
$NodeMirror = "https://cdn.npmmirror.com/binaries/node"
$PypiIndex = "https://pypi.org/simple/"
$PypiMirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
$PythonDownloads = "https://releases.astral.sh/"
$PythonMirror = "https://registry.npmmirror.com/-/binary/python-build-standalone"
$NpmRegistry = "https://registry.npmjs.org/"
$NpmMirror = "https://registry.npmmirror.com"
$PlaywrightMirror = "https://cdn.npmmirror.com/binaries/playwright"
# uv's installer, the PowerShell uv documents running it in, and where it
# downloads uv from besides releases.astral.sh ($PythonDownloads): GitHub's
# release files. Asked only to tell why the installer left no uv
# (Test-UvUnreachable).
$UvInstaller = "https://astral.sh/uv/install.ps1"
$UvInstallShell = "powershell.exe"
$UvGithubRelease = "https://github.com/astral-sh/uv/releases/latest/download/uv-installer.ps1"
# Where playwright downloads Chromium from, in its own order (Install-Chromium).
$PlaywrightHosts = @("https://cdn.playwright.dev/dbazure/download/playwright", "https://playwright.download.prss.microsoft.com/dbazure/download/playwright")

# Every variable a fallback set, with the value it replaced: under `irm | iex`
# this is the reader's own session, which gets its environment back when the
# install ends (Restore-MirrorEnv).
$script:MirrorSaved = @{}
$script:NodeDistChosen = $null
# Whether the npm registry answers at all: $null until it was asked.
$script:NpmAnswers = $null

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

# Read the way raven's own opt-outs read it, so this script and the raven it
# installs agree on what a value means.
function Test-MirrorsAllowed {
    return -not ($env:RAVEN_NO_MIRROR -match '^\s*(1|true|yes|on)\s*$')
}

# False only when no connection to $Url completed: the name did not resolve, no
# route led to the host, the connection was refused, reset or timed out, or TLS
# never got going. Any HTTP answer, an error status included, carries a
# response and is the host being there, and so is a certificate that fails
# verification: whatever re-signs this host's traffic re-signs a mirror's too.
# A proxy's answer to the request for a tunnel speaks for the host: a 5xx says
# it could not reach it, while any other refusal is final -- the mirror would be
# asked through the same proxy, and would only route around its rules. A SOCKS
# proxy that could not connect gives no status, and counts as not reached. The
# rule raven/utils/mirrors.py states in full; a test runs every copy of it
# against the same hosts and proxies.
function Test-Reachable([string]$Url) {
    try {
        $null = Invoke-WebRequest $Url -Method Head -UseBasicParsing -TimeoutSec 10
        return $true
    } catch {
        $tunnel = Get-TunnelStatus $_.Exception $Url
        if ($null -ne $tunnel) { return ($tunnel -gt 0 -and $tunnel -lt 500) }
        if ($_.Exception.Response) { return $true }
        # Windows PowerShell names a refused certificate outright.
        if ("$($_.Exception.Status)" -eq "TrustFailure") { return $true }
        # PowerShell 7 reports one as it does any handshake that failed, so the
        # host is asked again without the check: an answer then means the
        # certificate was all that failed.
        if ("$($_.Exception.HttpRequestError)" -ne "SecureConnectionError") { return $false }
    }
    try {
        $null = Invoke-WebRequest $Url -Method Head -UseBasicParsing -TimeoutSec 10 -SkipCertificateCheck
        return $true
    } catch {
        return [bool]$_.Exception.Response
    }
}

# What a proxy refused the tunnel to $Url with, when that refusal is why
# $Failure was raised: its status, or 0 from a SOCKS proxy, which gives none;
# $null for any other failure. PowerShell 7.4 and later name the failure and
# carry an HTTP proxy's status. Windows PowerShell hands back the proxy's own
# response, which comes from the proxy's address rather than $Url's.
function Get-TunnelStatus($Failure, [string]$Url) {
    if ("$($Failure.HttpRequestError)" -eq "ProxyTunnelError") {
        if ($Failure.StatusCode) { return [int]$Failure.StatusCode }
        return 0
    }
    $response = $Failure.Response
    if ($response -and $response.ResponseUri) {
        $target = [uri]$Url
        $proxy = [System.Net.WebRequest]::DefaultWebProxy
        $through = if ($proxy) { $proxy.GetProxy($target) } else { $null }
        if ($through -and $through.Authority -ne $target.Authority -and $response.ResponseUri.Authority -eq $through.Authority) {
            return [int]$response.StatusCode
        }
    }
    return $null
}

function Set-MirrorEnv([string]$Name, [string]$Value) {
    if (-not $script:MirrorSaved.ContainsKey($Name)) {
        $script:MirrorSaved[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
}

function Restore-MirrorEnv {
    foreach ($name in @($script:MirrorSaved.Keys)) {
        [Environment]::SetEnvironmentVariable($name, $script:MirrorSaved[$name], "Process")
    }
    $script:MirrorSaved = @{}
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
    # Fetched here rather than by the installer's own PowerShell, so a host that
    # answered with an error is told apart from one that could not be reached.
    $installer = Join-Path ([IO.Path]::GetTempPath()) ("raven-uv-installer-" + [guid]::NewGuid().ToString("N") + ".ps1")
    $fetched = $false
    try {
        Invoke-WebRequest $UvInstaller -OutFile $installer -UseBasicParsing
        $fetched = $true
    } catch {
        Write-Warn "uv's installer could not be fetched from $UvInstaller ($_)."
    }
    if ($fetched) {
        # In a Windows PowerShell of its own, as uv documents: the installer
        # ends a failed download with `exit 1`, which run inside this script
        # would end the install too -- and, under `irm | iex`, the reader's
        # whole session -- before the mirror below is tried. What it prints
        # goes to the console: left in this function's output it would come
        # back beside uv's path.
        $run = "Invoke-Expression ([IO.File]::ReadAllText('" + ($installer -replace "'", "''") + "'))"
        try {
            & $UvInstallShell -NoProfile -ExecutionPolicy Bypass -Command $run | Out-Host
            if ($LASTEXITCODE -ne 0) { Write-Warn "uv's installer did not finish (exit code $LASTEXITCODE)." }
        } catch {
            Write-Warn "uv's installer did not start ($_)."
        }
    }
    Remove-Item $installer -Force -ErrorAction SilentlyContinue
    $uv = Find-Uv
    if (-not $uv -and (Test-MirrorsAllowed) -and -not (Test-UvHostChosen) -and (Test-UvUnreachable $fetched)) {
        $uv = Install-UvFromMirror
    }
    if (-not $uv) {
        Fail "uv could not be installed, or is not where it was put. Check PATH (expected ~/.local/bin)."
    }
    Add-ProcessPath (Split-Path $uv -Parent)
    Write-Ok "uv installed"
    return $uv
}

# Whether the user chose where uv's installer downloads uv from. It reads these
# four, in this order, before its own two hosts, and the host chosen keeps its
# say: its answer, or its silence, is final, and the mirror never replaces it.
function Test-UvHostChosen {
    return [bool]($env:UV_DOWNLOAD_URL -or $env:INSTALLER_DOWNLOAD_URL -or $env:UV_INSTALLER_GHE_BASE_URL -or $env:UV_INSTALLER_GITHUB_BASE_URL)
}

# Whether uv's installer failed for want of a connection, the one failure a
# mirror stands in for: astral.sh could not be reached for the installer, or
# the installer ran and neither host it downloads uv from could be reached. Any
# answer -- an HTTP error, a certificate that fails verification, a failure
# inside the installer -- is final, and installing uv from another host would
# hide it.
function Test-UvUnreachable([bool]$Fetched) {
    if (-not $Fetched) { return -not (Test-Reachable $UvInstaller) }
    return (-not (Test-Reachable $PythonDownloads)) -and (-not (Test-Reachable $UvGithubRelease))
}

# uv publishes the binary its installer fetches to PyPI too, as a wheel, and a
# wheel is a zip. When uv's own installer could not reach its hosts
# (Test-UvUnreachable), uv.exe and uvx.exe are unpacked from the newest Windows
# wheel on the indexes the user named for uv, asked in uv's order
# (Get-UvNamedIndexes), or on the PyPI mirror when they named none, and checked
# against the sha256 the index publishes beside the link. An index named in a
# uv.toml this cannot read keeps the mirror away all the same. Returns the uv
# now found, if any.
function Install-UvFromMirror {
    $tag = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "win_arm64" } else { "win_amd64" }
    $indexes = @(Get-UvNamedIndexes)
    if ($indexes.Count -eq 0) {
        if (Test-UvIndexChosen) {
            Write-Warn "uv's installer could not reach its hosts, and the index a uv.toml names could not be read here, so uv does not come from the mirror; set UV_DEFAULT_INDEX to that index to fetch uv from it."
            return $null
        }
        $indexes = @($PypiMirror)
    }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("raven-uv-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    try {
        # The newest release, compared field by field: an index lists files in
        # name order, where 0.9.9 sorts after 0.13.4.
        $best = $null
        $link = 'href="([^"#]*?uv-(\d+)\.(\d+)\.(\d+)-py3-none-' + $tag + '\.whl)(?:#sha256=([0-9a-f]{64}))?"'
        foreach ($index in $indexes) {
            $page = $index.TrimEnd("/") + "/uv/"
            Write-Warn "uv's installer could not reach its hosts; fetching uv from $page instead (RAVEN_NO_MIRROR=1 turns this off)."
            try {
                $html = (Invoke-WebRequest $page -UseBasicParsing).Content
            } catch {
                Write-Warn "Could not read $page ($_)."
                continue
            }
            foreach ($match in [regex]::Matches($html, $link)) {
                $version = [version]("{0}.{1}.{2}" -f $match.Groups[2].Value, $match.Groups[3].Value, $match.Groups[4].Value)
                if (-not $best -or $version -gt $best.Version) {
                    $best = @{ Version = $version; Href = $match.Groups[1].Value; Sha = $match.Groups[5].Value }
                }
            }
            if ($best) { break }
        }
        if (-not $best) { throw "no uv wheel for $tag is listed" }
        $wheel = Join-Path $tmp "uv.zip"
        Invoke-WebRequest ([Uri]::new([Uri]$page, $best.Href).AbsoluteUri) -OutFile $wheel -UseBasicParsing
        if ($best.Sha -and (Get-FileHash $wheel -Algorithm SHA256).Hash.ToLowerInvariant() -ne $best.Sha) {
            throw "the wheel does not match the sha256 the index publishes"
        }
        Expand-Archive $wheel -DestinationPath $tmp -Force
        $bin = Join-Path $HOME ".local\bin"
        New-Item -ItemType Directory -Path $bin -Force | Out-Null
        foreach ($exe in @("uv.exe", "uvx.exe")) {
            $found = Get-ChildItem $tmp -Recurse -Filter $exe | Where-Object { $_.Directory.Name -eq "scripts" } | Select-Object -First 1
            if ($found) { Copy-Item $found.FullName (Join-Path $bin $exe) -Force }
        }
    } catch {
        Write-Warn "Could not fetch uv from $page ($_)."
    } finally {
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
    return Find-Uv
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

function Get-LatestNodeV22([string]$Dist) {
    try {
        $index = Invoke-RestMethod "$Dist/index.json"
        $entry = $index | Where-Object { $_.version -like "v22.*" } | Select-Object -First 1
        if ($entry -and $entry.version) { return $entry.version }
    } catch {
        Write-Warn "Could not query Node.js release index; falling back to v22.20.0"
    }
    return "v22.20.0"
}

# Where Node comes from: nodejs.org, or its mirror when nodejs.org cannot be
# reached at all. Decided once, so a host that does not answer is waited on once.
function Get-NodeDist {
    if ($null -eq $script:NodeDistChosen) {
        $script:NodeDistChosen = $NodeDist
        if ((Test-MirrorsAllowed) -and -not (Test-Reachable "$NodeDist/index.json")) {
            Write-Warn "nodejs.org could not be reached; downloading Node from $NodeMirror instead (RAVEN_NO_MIRROR=1 turns this off)."
            $script:NodeDistChosen = $NodeMirror
        }
    }
    return $script:NodeDistChosen
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
    return Install-PrivateNode
}

# Download, verify and extract a private Node runtime into $NodeRuntimeDir, and
# return the node.exe in it. Split out of Ensure-Node because Build-WebAssets
# needs it on a second path: a node packaged without npm satisfies Ensure-Node
# and leaves the build with no npm to call. Failures here raise (via Fail), so a
# caller that must not abort on a failed download wraps this in try/catch.
function Install-PrivateNode {
    $arch = Get-NodeArch
    $from = Get-NodeDist
    $version = Get-LatestNodeV22 $from
    $pkg = "node-$version-win-$arch"
    $url = "$from/$version/$pkg.zip"
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("raven-node-" + [guid]::NewGuid().ToString("N"))
    $zipPath = Join-Path $tmp "node.zip"

    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    New-Item -ItemType Directory -Path $NodeRuntimeDir -Force | Out-Null

    try {
        Write-Info "  $url"
        Invoke-WebRequest $url -OutFile $zipPath

        # A zip that cannot be verified is treated like one that fails
        # verification: it is never extracted. The checksums come from the host
        # the zip came from, so the check catches a corrupt or truncated
        # download. Fail raises under this script's ErrorActionPreference, so
        # only the fetch sits inside the try below; a Fail inside it would be
        # caught there and the zip extracted anyway.
        $hint = "Install Node.js >= $MinNodeMajor with npm yourself and re-run; the installer then uses it instead of downloading one."
        try {
            $sums = (Invoke-WebRequest "$from/$version/SHASUMS256.txt" -UseBasicParsing).Content
        } catch {
            Fail "Could not fetch SHASUMS256.txt for Node $version, so the download cannot be verified. $hint"
        }
        $line = ($sums -split "`n") | Where-Object { $_ -match "\s+$([regex]::Escape("$pkg.zip"))$" } | Select-Object -First 1
        if (-not $line) {
            Fail "SHASUMS256.txt for Node $version does not list $pkg.zip, so the download cannot be verified. $hint"
        }
        $expected = (($line.Trim()) -split "\s+")[0].ToLowerInvariant()
        $actual = (Get-FileHash $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($expected -ne $actual) {
            Fail "Node checksum mismatch (expected $expected, got $actual)."
        }
        Write-Ok "Node zip SHA256 verified"

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
        # Windows PowerShell 5.1 hands back the unfollowed redirect as the
        # response and reports the exceeded redirect count as an error with no
        # response attached, so that error is ignored rather than caught.
        $response = Invoke-WebRequest "https://github.com/EverMind-AI/Raven/releases/latest" -MaximumRedirection 0 -UseBasicParsing -ErrorAction Ignore
        $target = [string]$response.Headers.Location
    } catch {
        # PowerShell 7 raises on an unfollowed redirect whatever -ErrorAction
        # says; the Location header rides on the exception's response.
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
    # One discovery step, no GitHub API: the release page redirect names the
    # latest stable tag, and the wheel, the locked constraints and the plugin
    # list are all derived from it, since they sit in one release directory.
    if ($env:RAVEN_WHEEL_URL) { return $env:RAVEN_WHEEL_URL }
    Write-Info "Resolving the latest Raven release from GitHub..."
    $version = Resolve-RavenLatestVersion
    if (-not $version) {
        Fail "Could not resolve the latest Raven release wheel from GitHub. Retry later, or set RAVEN_WHEEL_URL to a wheel URL."
    }
    return "https://github.com/EverMind-AI/Raven/releases/download/v$version/raven-$version-py3-none-any.whl"
}

function Resolve-RavenPluginList([string]$WheelUrl) {
    # The plugin list from the same release directory: the wheels the release
    # ships beside raven, one `name @ url` line each, written by the release
    # workflow from what it built. What a complete install is made of lives
    # there, not here, and `raven upgrade` installs from the same file. Returns
    # a local temp-file path, or $null when the release carries none (0.1.13
    # and older), which installs raven alone as it always did.
    if ($WheelUrl -notmatch "/[^/]+\.whl$") { return $null }
    $url = $WheelUrl -replace "/[^/]+\.whl$", "/raven-plugins.txt"
    $dest = Join-Path ([IO.Path]::GetTempPath()) ("raven-plugins-" + [guid]::NewGuid().ToString("N") + ".txt")
    try {
        Invoke-WebRequest $url -OutFile $dest
    } catch {
        return $null
    }
    if (-not (Get-Content $dest | Where-Object { $_.Trim() })) { return $null }
    return $dest
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

# True when the artifact is missing, or any source under the named directories
# is newer than it. Missing is not the only reason to build: the install is
# editable, so Python tracks the checkout with no further help, but these two
# artifacts are compiled and nothing relinks them -- checked only for existence
# they keep serving whatever the tree held at first install while every .py
# beside them moves on. mtime is the right question: git stamps every file it
# rewrites with the time it wrote it, so a pull that touched the frontend sorts
# after the artifact and one that did not leaves it alone.
#
# The prune is one level deep, where node_modules, dist and .modern actually
# sit; install.sh prunes them at any depth. Deeper is reachable here only by
# walking node_modules to decide to ignore it, which is the one directory worth
# not walking.
function Test-AssetStale([string]$Artifact, [string[]]$Sources) {
    if (-not (Test-Path $Artifact)) { return $true }
    $builtAt = (Get-Item $Artifact).LastWriteTimeUtc
    $pruned = @("node_modules", "dist", ".modern")
    foreach ($dir in $Sources) {
        if (-not (Test-Path $dir)) { continue }
        if (-not (Test-Path $dir -PathType Container)) {
            if ((Get-Item $dir).LastWriteTimeUtc -gt $builtAt) { return $true }
            continue
        }
        $entries = @(Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue |
            Where-Object { -not ($_.PSIsContainer -and $pruned -contains $_.Name) })
        foreach ($entry in $entries) {
            if (-not $entry.PSIsContainer) {
                if ($entry.LastWriteTimeUtc -gt $builtAt) { return $true }
                continue
            }
            $newer = Get-ChildItem -LiteralPath $entry.FullName -Recurse -Force -ErrorAction SilentlyContinue |
                Where-Object { -not $_.PSIsContainer -and $_.LastWriteTimeUtc -gt $builtAt } |
                Select-Object -First 1
            if ($newer) { return $true }
        }
    }
    return $false
}

# ui-tui\dist\entry.js (the TUI bundle) and ui\dist\index.html (the page
# `raven web` serves) are both gitignored build artifacts. A release wheel
# carries them; an editable install of a checkout gets neither, so without this
# a clone install has no TUI and no page. Both must exist, and both must be no
# older than the sources they were built from, before first run.
function Build-WebAssets([string]$ScriptDir, [string]$NodePath, [string]$UvPath) {
    $needTui = Test-AssetStale (Join-Path $ScriptDir "ui-tui\dist\entry.js") @((Join-Path $ScriptDir "ui-tui"))
    # The page inlines the shared catalogues -- the bundle reads
    # i18n/messages.json and ui-shared/*.json -- so a catalogue-only change is
    # a page change.
    $needPage = Test-AssetStale (Join-Path $ScriptDir "ui-web\dist\index.html") @(
        (Join-Path $ScriptDir "ui-web"),
        (Join-Path $ScriptDir "i18n"),
        (Join-Path $ScriptDir "ui-shared")
    )
    if (-not ($needTui -or $needPage)) { return }

    # One probe for both builds. npm ships alongside node, but verify it
    # explicitly rather than assume -- a node packaged without npm satisfies
    # Ensure-Node, so the build can arrive here with no npm and no private
    # runtime ever fetched. The official archive carries npm beside node, so
    # fetch one at that point rather than skip both builds on a machine one
    # download away from running them. try/catch because that fetch raises on
    # failure and this caller must not: the node already here still runs
    # `raven tui`, so a failed download is a skipped build, not a failed
    # install.
    Add-ProcessPath (Split-Path $NodePath -Parent)
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npm) {
        $privateNode = Find-PrivateNode
        if (-not $privateNode) {
            Write-Info "Found node but not npm; fetching a private Node runtime that carries both..."
            try { $privateNode = Install-PrivateNode } catch { $privateNode = $null }
        }
        if ($privateNode) {
            Add-ProcessPath (Split-Path $privateNode -Parent)
            $npm = Get-Command npm -ErrorAction SilentlyContinue
        }
    }
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
            # Asked here, in the directory the install runs in: npm reads the
            # .npmrc of the project beside it, and the page below may name
            # another registry.
            Use-NpmMirror (Get-NpmRegistry $npm)
            & $npm.Source ci
            if ($LASTEXITCODE -ne 0) { Fail "TUI bundle build failed: npm ci exited $LASTEXITCODE" }
            & $npm.Source run build
            if ($LASTEXITCODE -ne 0) { Fail "TUI bundle build failed: npm run build exited $LASTEXITCODE" }
        } finally {
            Restore-NpmMirror
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
                # Asked here, in the directory the install runs in: npm reads
                # the .npmrc of the project beside it, and the bundle above may
                # have named another registry.
                Use-NpmMirror (Get-NpmRegistry $npm)
                & $npm.Source ci
                if ($LASTEXITCODE -ne 0) { throw "npm ci exited $LASTEXITCODE" }
                & $npm.Source run build
                if ($LASTEXITCODE -ne 0) { throw "npm run build exited $LASTEXITCODE" }
            } finally {
                Restore-NpmMirror
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

# What $npm will install from, or $null when it cannot say: an npm that fails
# may have printed its default all the same. Asked in the directory the
# install runs in: npm reads the .npmrc of the project beside it.
function Get-NpmRegistry($npm) {
    try {
        $answer = [string](& $npm.Source config get registry 2>$null)
        if ($LASTEXITCODE -eq 0) { return $answer.Trim() }
    } catch { }
    return $null
}

# Whether the npm registry answers at all: one probe for the install, since
# both builds read the same registry.
function Test-NpmRegistryReachable {
    if ($null -eq $script:NpmAnswers) { $script:NpmAnswers = Test-Reachable $NpmRegistry }
    return $script:NpmAnswers
}

# `npm ci` reads every package from the registry. One that cannot be reached at
# all is swapped for its mirror, unless the user named a registry. $Registry is
# the one npm says it reads, and $null when it could not say, which is no proof
# that the choice is npm's default. Untyped, since [string] turns $null into "".
# The fallback is set for the build that asked for it, and put back after it:
# the two builds may name different registries, and neither may reach the other.
function Use-NpmMirror($Registry) {
    if (-not (Test-MirrorsAllowed) -or $env:npm_config_registry) { return }
    if ($null -eq $Registry -or $Registry.TrimEnd("/") -ne $NpmRegistry.TrimEnd("/")) { return }
    if (Test-NpmRegistryReachable) { return }
    Write-Warn "registry.npmjs.org could not be reached; installing packages from $NpmMirror instead (RAVEN_NO_MIRROR=1 turns this off)."
    Set-MirrorEnv "npm_config_registry" $NpmMirror
}

# Put back what the npm fallback replaced, for that build only.
function Restore-NpmMirror {
    if (-not $script:MirrorSaved.ContainsKey("npm_config_registry")) { return }
    [Environment]::SetEnvironmentVariable("npm_config_registry", $script:MirrorSaved["npm_config_registry"], "Process")
    $script:MirrorSaved.Remove("npm_config_registry")
}

# The uv.toml files uv reads: the one UV_CONFIG_FILE names, or the user's,
# then the machine's.
function Get-UvConfigFiles {
    if ($env:UV_CONFIG_FILE) { return @($env:UV_CONFIG_FILE) }
    return @($env:APPDATA, $env:PROGRAMDATA) | Where-Object { $_ } | ForEach-Object { Join-Path $_ "uv\uv.toml" }
}

# The text of a uv.toml, as uv reads it: a byte-order mark a Windows editor
# may have written is not part of the first key, and uv reads past it.
# Windows PowerShell 5.1 keeps that mark in the text it reads.
function Get-UvConfigText([string]$Path) {
    $text = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if ($text -and $text[0] -eq [char]0xFEFF) { $text = $text.Substring(1) }
    return $text
}

# The lines of a uv.toml, as uv reads them.
function Get-UvConfigLines([string]$Path) {
    $text = Get-UvConfigText $Path
    if (-not $text) { return @() }
    return @($text -split "`r?`n")
}

# Whether a uv.toml uv reads -- the user's, or the machine's -- matches $Pattern.
# A key may be quoted, as TOML allows: "index-url" = ... is the same key as
# index-url = ...
function Test-UvConfigHas([string]$Pattern) {
    foreach ($path in @(Get-UvConfigFiles)) {
        if ((Test-Path -LiteralPath $path) -and ((Get-UvConfigText $path) -match $Pattern)) { return $true }
    }
    return $false
}

# The address of the default index a uv.toml uv reads names: its index-url,
# or the url of the index marked default = true, written as a table or inline.
# The first file that names one decides, as it does for uv. A key may be
# quoted, as TOML allows.
function Get-UvConfigIndex {
    $quoted = '\s*=\s*(?:"([^"]*)"|''([^'']*)'')'
    foreach ($path in @(Get-UvConfigFiles)) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $table = $false
        $url = $null
        $isDefault = $false
        foreach ($line in @(Get-UvConfigLines $path)) {
            if ($line -match '^\s*\[') {
                $table = $line -match '^\s*\[\[\s*"?index"?\s*\]\]'
                $url = $null
                $isDefault = $false
                continue
            }
            if ($line -match ('^\s*"?index-url"?' + $quoted)) { return $Matches[1] + $Matches[2] }
            if (($line -match '[{,]\s*"?default"?\s*=\s*true') -and ($line -match ('[{,]\s*"?url"?' + $quoted))) {
                return $Matches[1] + $Matches[2]
            }
            if ($table -and ($line -match ('^\s*"?url"?' + $quoted))) { $url = $Matches[1] + $Matches[2] }
            if ($table -and ($line -match '^\s*"?default"?\s*=\s*true')) { $isDefault = $true }
            if ($table -and $isDefault -and $url) { return $url }
        }
    }
    return $null
}

function Test-UvIndexChosen {
    if ($env:UV_DEFAULT_INDEX -or $env:UV_INDEX_URL -or $env:UV_INDEX) { return $true }
    # A default index is marked default = true, in an [[index]] table or inline.
    return (Test-UvConfigHas '(?m)^\s*"?index-url"?\s*=') -or (Test-UvConfigHas '(?m)(^|[{,\s])"?default"?\s*=\s*true')
}

# The indexes the user named for uv, in the order uv asks them: each of
# UV_INDEX's, then the default -- UV_DEFAULT_INDEX or UV_INDEX_URL, or else
# the one a uv.toml names.
function Get-UvNamedIndexes {
    $named = @()
    if ($env:UV_INDEX) {
        $named += @($env:UV_INDEX -split '\s+' | Where-Object { $_ } | ForEach-Object { $_ -replace '^[A-Za-z0-9._-]*=', '' })
    }
    if ($env:UV_DEFAULT_INDEX) {
        $named += $env:UV_DEFAULT_INDEX
    } elseif ($env:UV_INDEX_URL) {
        $named += $env:UV_INDEX_URL
    } else {
        $fromConfig = Get-UvConfigIndex
        if ($fromConfig) { $named += $fromConfig }
    }
    return $named
}

# uv resolves Raven's dependencies from PyPI, and fetches a Python when the
# machine has none it can use. Either host that cannot be reached at all is
# swapped for its mirror -- unless the user named one -- for this install's uv.
function Use-PackageMirrors {
    if (-not (Test-MirrorsAllowed)) { return }
    if (-not (Test-UvIndexChosen) -and -not (Test-Reachable $PypiIndex)) {
        Write-Warn "pypi.org could not be reached; installing packages from $PypiMirror instead (RAVEN_NO_MIRROR=1 turns this off)."
        Set-MirrorEnv "UV_DEFAULT_INDEX" $PypiMirror
    }
    # A Python source the user named counts in each form uv reads: a mirror it
    # is given outranks a copy of releases.astral.sh, and fails outright beside
    # a list of downloads that live elsewhere.
    $pythonNamed = $env:UV_PYTHON_INSTALL_MIRROR -or $env:UV_PYTHON_DOWNLOADS_JSON_URL -or $env:UV_ASTRAL_MIRROR_URL
    $pythonChosen = $pythonNamed -or (Test-UvConfigHas '(?m)^\s*"?python-(install-mirror|downloads-json-url)"?\s*=')
    if (-not $pythonChosen -and -not (Test-Reachable $PythonDownloads)) {
        Write-Warn "releases.astral.sh could not be reached; fetching any Python uv needs from $PythonMirror instead (RAVEN_NO_MIRROR=1 turns this off)."
        Set-MirrorEnv "UV_PYTHON_INSTALL_MIRROR" $PythonMirror
    }
}

function Install-Raven([string]$UvPath, [string]$NodePath) {
    Use-PackageMirrors
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
        # The default config names the everos memory backend, and a user can
        # point memory at the hosted EverOS instead; each ships as its own
        # distribution beside the wheel -- carry both, and degrade loudly (the
        # host boots memoryless and `raven doctor` says why) if they cannot
        # build.
        $memoryPlugin = @(
            "--with-editable", (Join-Path $scriptDir "plugins-dist\everos-memory"),
            "--with-editable", (Join-Path $scriptDir "plugins-dist\everos-cloud-memory")
        )
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
        # channel SDK cannot block the whole install. Four rungs, dropping one
        # capability each: the engines carry native builds (PDF, raster,
        # plotting) a platform can refuse on its own, so they fall before the
        # memory plugins rather than taking them down too.
        try {
            & $UvPath tool install --force -c "$constraints" @memoryPlugin @enginePlugins -e "$scriptDir[channels]"
            if ($LASTEXITCODE -ne 0) { throw "channel extras install failed" }
        } catch {
            Write-Warn "Channel dependencies failed to install; retrying with base raven. Some channels stay unavailable (see: raven channels list)."
            & $UvPath tool install --force -c "$constraints" @memoryPlugin @enginePlugins -e "$scriptDir"
            if ($LASTEXITCODE -ne 0) {
                Write-Warn "A product engine failed to build; Raven-Design and Raven-PPT stay disabled (raven doctor explains)."
                & $UvPath tool install --force -c "$constraints" @memoryPlugin -e "$scriptDir"
                if ($LASTEXITCODE -ne 0) {
                    Write-Warn "The EverOS memory plugins failed to install; long-term memory stays off (raven doctor explains)."
                    & $UvPath tool install --force -c "$constraints" -e "$scriptDir"
                    if ($LASTEXITCODE -ne 0) { Fail "Raven install failed." }
                }
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
        $pArgs = @()
        $mArgs = @()
        $plugins = Resolve-RavenPluginList $wheelUrl
        if ($plugins) {
            Write-Info "  with the release's plugins:"
            Get-Content $plugins | ForEach-Object { Write-Info "    $_" }
            $pArgs = @("--with-requirements", $plugins)
            $allLines = @(Get-Content $plugins | Where-Object { $_.Trim() })
            $memoryLines = @($allLines | Where-Object { $_ -match "^(everos-memory|everos-cloud-memory) " })
            if ($memoryLines.Count -gt 0 -and $memoryLines.Count -lt $allLines.Count) {
                $memoryOnly = Join-Path ([IO.Path]::GetTempPath()) ("raven-plugins-memory-" + [guid]::NewGuid().ToString("N") + ".txt")
                Set-Content -Path $memoryOnly -Value $memoryLines -Encoding ascii
                $mArgs = @("--with-requirements", $memoryOnly)
            }
        } else {
            Write-Warn "This release carries no plugin list; long-term memory, Raven-Design and Raven-PPT stay off (raven doctor explains)."
        }
        Write-Info "  installing $wheelUrl"
        # Two independent things can fail: the channel extras, and the plugins
        # (the engines' native builds first, the memory plugin after). A failed
        # attempt does not say which, so the rungs walk both axes and stop at
        # the first that lands -- the largest install this machine can build --
        # and warn about exactly what that rung lacks:
        #   1 channels + all plugins      4 base + memory plugin
        #   2 base + all plugins          5 channels, no plugins
        #   3 channels + memory plugin    6 base, no plugins
        $lostChannels = "Channel dependencies failed to install; some channels stay unavailable (see: raven channels list)."
        $lostEngines = "A product engine failed to install; Raven-Design and Raven-PPT stay disabled (raven doctor explains)."
        $lostPlugins = "No plugin could be installed; long-term memory, Raven-Design and Raven-PPT stay off (raven doctor explains)."
        $landed = $null
        & $UvPath tool install --force @cArgs @pArgs "raven[channels] @ $wheelUrl"
        if ($LASTEXITCODE -eq 0) { $landed = @() }
        if ($null -eq $landed) {
            & $UvPath tool install --force @cArgs @pArgs $wheelUrl
            if ($LASTEXITCODE -eq 0) { $landed = @($lostChannels) }
        }
        if ($null -eq $landed -and $mArgs.Count -gt 0) {
            & $UvPath tool install --force @cArgs @mArgs "raven[channels] @ $wheelUrl"
            if ($LASTEXITCODE -eq 0) { $landed = @($lostEngines) }
        }
        if ($null -eq $landed -and $mArgs.Count -gt 0) {
            & $UvPath tool install --force @cArgs @mArgs $wheelUrl
            if ($LASTEXITCODE -eq 0) { $landed = @($lostEngines, $lostChannels) }
        }
        if ($null -eq $landed -and $pArgs.Count -gt 0) {
            & $UvPath tool install --force @cArgs "raven[channels] @ $wheelUrl"
            if ($LASTEXITCODE -eq 0) { $landed = @($lostPlugins) }
        }
        if ($null -eq $landed -and $pArgs.Count -gt 0) {
            & $UvPath tool install --force @cArgs $wheelUrl
            if ($LASTEXITCODE -eq 0) { $landed = @($lostPlugins, $lostChannels) }
        }
        if ($null -eq $landed) { Fail "Raven install failed." }
        foreach ($loss in $landed) { Write-Warn $loss }
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
        if (-not (Install-Chromium $py)) { throw "playwright install chromium exited $LASTEXITCODE" }
    } catch {
        Write-Warn "Chromium download failed; the browser tool stays off. Retry later with: $py -m playwright install chromium"
    }
}

# Playwright tries its own hosts in turn and reports only that none of them
# delivered, so the mirror gets one retry after the whole download failed --
# unless the user named a download host, or one of those hosts answered: it had
# its say, and the mirror stands in only for hosts that cannot be reached. True
# when chromium is in place; what playwright prints goes to the console rather
# than into that answer.
function Install-Chromium([string]$Py) {
    & $Py -m playwright install chromium | Out-Host
    if ($LASTEXITCODE -eq 0) { return $true }
    if (-not (Test-MirrorsAllowed) -or $env:PLAYWRIGHT_DOWNLOAD_HOST -or $env:PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST) {
        return $false
    }
    foreach ($playwrightHost in $PlaywrightHosts) {
        if (Test-Reachable $playwrightHost) { return $false }
    }
    Write-Warn "Chromium could not be downloaded; trying $PlaywrightMirror instead (RAVEN_NO_MIRROR=1 turns this off)."
    Set-MirrorEnv "PLAYWRIGHT_DOWNLOAD_HOST" $PlaywrightMirror
    & $Py -m playwright install chromium | Out-Host
    return ($LASTEXITCODE -eq 0)
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
    # Default yes, matching install.sh: for the deck lane this is the one
    # dependency that matters, and macOS installs it without asking.
    $answer = Read-Host "Install LibreOffice for deck preview (may raise a UAC prompt)? Without it a deck still builds, but no page is ever rendered, measured or checked. [Y/n]"
    if ($answer -match "^[nN]") {
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

# The install ends on a running page. `--stop` first, because a gateway an
# earlier install left resident would be attached to instead of the build that
# just landed; `--foreground` then holds this session on a fresh one and opens
# the browser on it, so Ctrl-C here means what it says. The raven shim lands in
# `uv tool dir --bin`, which this session's PATH may not carry yet, so invoke it
# by absolute path.
#
# A non-zero page exit only warns: under `irm | iex` this is the caller's own
# interactive PowerShell, and Ctrl-C -- the ordinary way to end a foreground
# page -- comes back non-zero, so exiting on it would close the window the
# reader is standing in.
function Start-Web([string]$UvPath) {
    $binDir = ""
    try { $binDir = [string](& $UvPath tool dir --bin 2>$null) } catch { $binDir = "" }
    $bin = if ($binDir) { Join-Path $binDir "raven.exe" } else { $null }
    if (-not $bin -or -not (Test-Path $bin)) { $bin = Join-Path $HOME ".local\bin\raven.exe" }
    if (-not (Test-Path $bin)) {
        Write-Warn "raven is not where this script looked for it; open a new PowerShell window and run: raven"
        return
    }
    # The release this script just installed may predate `raven web` (0.1.13
    # does). Ask before calling, and end on the command every release has.
    & $bin web --help *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Ok "Raven installed. Open a new PowerShell window, then run: raven"
        return
    }
    Write-Host ""
    Write-Ok "Starting Raven -- your browser will open in a moment. Ctrl-C here stops it."
    Write-Host ""
    # `raven web` starts its engine as `python -m raven`, which puts the working
    # directory first on sys.path: run from inside a source checkout, a release
    # without the `-P` guard comes up on that checkout's raven, and the published
    # release this script installs may predate that guard. Under `irm | iex` this
    # is the caller's own shell, so the directory is put back once the page ends.
    Push-Location $HOME
    try {
        & $bin web --stop *> $null
        if ($LASTEXITCODE -ne 0) { Write-Warn "could not stop a previous gateway; continuing" }
        & $bin web --foreground
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "the page ended with exit code $LASTEXITCODE; start it again with 'raven web'"
        }
    } finally {
        Pop-Location
    }
}

function Main {
    try {
        $uv = Ensure-Uv
        $node = Ensure-Node
        Install-Raven $uv $node

        if (-not $env:RAVEN_MINIMAL) { Install-Browser $uv }
        if (-not $env:RAVEN_MINIMAL) { Install-Office }
    } finally {
        # A mirror served this install's downloads only. The page and the
        # session the reader comes back to start with their own environment;
        # the runtime falls back for itself.
        Restore-MirrorEnv
    }

    # Before the page, not after: the page holds this session until Ctrl-C, and
    # `raven` has to work in the session the reader comes back to.
    Add-ProcessPath (Join-Path $HOME ".local\bin")

    if (-not $env:RAVEN_NO_LAUNCH) { Start-Web $uv }
}

Main
