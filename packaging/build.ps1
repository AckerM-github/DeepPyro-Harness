#Requires -Version 5.1
<#
.SYNOPSIS
    Build a frozen DeepPyro Harness executable with PyInstaller.

.DESCRIPTION
    Build inputs (hiddenimports + datas) are generated from the shipped bundle
    data by packaging/hiddenimports.py, so the frozen plugin module list can
    never drift from src/deeppyro/bundles/core/bundle.toml.

    onedir is the default and the recommended shape. onefile works too, but it
    extracts the whole payload (including the tz database) to a temp directory on
    every start.

    Never build with --windowed/--noconsole: the TUI and the --internal-*
    child-process protocols need a real console handle.

.PARAMETER Mode
    onedir (default) or onefile.

.PARAMETER Clean
    Pass --clean to PyInstaller (drop its analysis cache).

.PARAMETER IncludePlugins
    Also freeze plugin packages found under $DEEPPYRO_PLUGINS_DIR (default
    ~/.dpro/plugins). Off by default: the executable is built once and plugins
    are installed into a user directory afterwards.

.PARAMETER DryRun
    Print the generated build inputs and stop. Works offline and without
    PyInstaller installed.

.PARAMETER SkipSelfTest
    Skip the post-build smoke test.

.PARAMETER StrictSelfTest
    Treat a failed headless turn as a build failure (a turn needs a configured
    provider, so this is off by default).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [ValidateSet("onedir", "onefile")]
    [string]$Mode = "onedir",
    [switch]$Clean,
    [switch]$IncludePlugins,
    [switch]$DryRun,
    [switch]$SkipSelfTest,
    [switch]$StrictSelfTest,
    [string]$HeadlessPrompt = "reply with the single word OK",
    [string]$Python = "",
    [string]$DistDir = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $repo ".venv\Scripts\python.exe" }
if (-not (Test-Path $Python)) { throw "python not found: $Python (pass -Python <path>)" }
if (-not $DistDir) { $DistDir = Join-Path $repo "dist\packaging" }

# The spec reads these; freezing plugin code stays opt-in.
$env:DEEPPYRO_FREEZE_MODE = $Mode
$env:DEEPPYRO_FREEZE_NAME = "dpyro"
$env:DEEPPYRO_FREEZE_PLUGINS = if ($IncludePlugins) { "1" } else { "0" }
if (-not $IncludePlugins) { Remove-Item Env:\DEEPPYRO_PLUGINS_DIR -ErrorAction SilentlyContinue }

$generator = "import sys; sys.path.insert(0, r'$repo\packaging'); import hiddenimports as g; print(g.summary(include_plugins=$($IncludePlugins.IsPresent)))"

Write-Host "== generated build inputs ($Mode) =="
& $Python -c $generator
if ($LASTEXITCODE -ne 0) { throw "hiddenimports generator failed" }

$probe = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('PyInstaller') else 1)"
& $Python -c $probe
$hasPyInstaller = ($LASTEXITCODE -eq 0)
if (-not $hasPyInstaller) {
    Write-Warning "PyInstaller is not installed in $Python"
    Write-Warning "The inputs above are complete; install it (needs network) with:"
    Write-Warning "    & '$Python' -m pip install pyinstaller"
    if ($DryRun) { Write-Host "dry run: inputs validated, nothing to build"; exit 0 }
    throw "PyInstaller missing; install it or re-run with -DryRun for an offline check"
}

if ($DryRun) { Write-Host "dry run: stopping before PyInstaller"; exit 0 }
Push-Location $repo
try {
    $pyArgs = @("-m", "PyInstaller", "--noconfirm")
    if ($Clean) { $pyArgs += "--clean" }
    $pyArgs += @("--distpath", $DistDir, "--workpath", (Join-Path $repo "build\packaging"), "packaging\deeppyro.spec")

    Write-Host "== build =="
    & $Python @pyArgs
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

    $exe = if ($Mode -eq "onefile") { Join-Path $DistDir "dpyro.exe" } else { Join-Path $DistDir "dpyro\dpyro.exe" }
    if (-not (Test-Path $exe)) { throw "expected executable missing: $exe" }
    Write-Host "built: $exe"

    if ($Mode -eq "onedir") {
        $internal = Join-Path $DistDir "dpyro\_internal"
        foreach ($item in @("bundles\core\bundle.toml", "certifi\cacert.pem", "tzdata\zoneinfo")) {
            $path = Join-Path $internal $item
            if (-not (Test-Path $path)) { throw "bundled data missing: $path" }
        }
        Write-Host "bundled data: bundle.toml + certifi + tzdata present"
    }

    if ($SkipSelfTest) { Write-Host "self-test skipped (-SkipSelfTest)"; exit 0 }

    Write-Host "== self-test =="
    $failures = [System.Collections.Generic.List[string]]::new()

    $version = & $exe --version
    if ($LASTEXITCODE -ne 0) { $failures.Add("--version exited $LASTEXITCODE") }
    elseif ("$version" -notmatch "deeppyro-harness") { $failures.Add("--version printed: $version") }
    else { Write-Host "  --version      : $version" }

    $null = & $exe plugin list 2>&1
    if ($LASTEXITCODE -ne 0) { $failures.Add("plugin list exited $LASTEXITCODE") }
    else { Write-Host "  plugin list    : ok" }

    # Proves the sandbox self-call survived freezing: the runner's own argparse
    # rejects an unknown flag with 127 instead of the TUI swallowing it.
    $null = & $exe --internal-sandbox-runner --bogus 2>&1
    if ($LASTEXITCODE -ne 127) { $failures.Add("internal sandbox dispatch exited $LASTEXITCODE (expected 127)") }
    else { Write-Host "  internal flags : ok" }

    # A real turn needs a configured provider; report it, fail only when strict.
    $turn = & $exe --headless --max-steps 1 $HeadlessPrompt 2>&1
    $turnExit = $LASTEXITCODE
    if ($turnExit -ne 0) {
        $tail = ($turn | Select-Object -Last 3) -join " | "
        if ($StrictSelfTest) { $failures.Add("headless turn exited ${turnExit}: $tail") }
        else { Write-Warning "headless turn exited ${turnExit} (provider not configured?): $tail" }
    }
    else { Write-Host "  headless turn  : ok" }

    if ($failures.Count -gt 0) { throw ("self-test failed: " + ($failures -join "; ")) }
    Write-Host "self-test passed"
}
finally {
    Pop-Location
}
