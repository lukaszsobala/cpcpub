# Install cpcpub's Windows setup on this machine and check it the way
# test-inside.sh checks a Linux package: the installed benchmark is the
# release's build for this machine, to the byte; it is on PATH, it runs and
# names itself; the Start-menu window finds it, runs it and reads the result
# back; installing again over it changes nothing; and uninstalling takes all
# of it away again.
#
#   pwsh packaging/windows/test-installer.ps1 -Setup X.exe -Release DIR -Machine x64|arm64
param(
    [Parameter(Mandatory)] [string] $Setup,
    [Parameter(Mandatory)] [string] $Release,
    [Parameter(Mandatory)] [ValidateSet("x64", "arm64")] [string] $Machine
)
$ErrorActionPreference = "Stop"
$dir = Join-Path $env:ProgramFiles "cpcpub"
$work = Join-Path $env:RUNNER_TEMP "installer-test"
New-Item -ItemType Directory -Force $work | Out-Null

function Say($text) { Write-Host "`n== $text" }

# Setup or the uninstaller, silently, with its log; returns the exit code.
# -Wait waits for the process's descendants too, which matters for the
# uninstaller: it starts a copy of itself from a temporary folder and exits.
function Silent([string] $program, [string] $log) {
    $p = Start-Process $program -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=`"$log`"") -Wait -PassThru
    if ($p.ExitCode -ne 0 -and (Test-Path $log)) { Get-Content $log -Tail 60 }
    return $p.ExitCode
}

function Digest($path) { (Get-FileHash -Algorithm SHA256 $path).Hash.ToLower() }

function PathEntries {
    [Environment]::GetEnvironmentVariable("Path", "Machine") -split ";" |
        Where-Object { $_.TrimEnd("\") -eq $dir }
}

Say "install"
$code = Silent $Setup (Join-Path $work "install.log")
if ($code -ne 0) { throw "setup exited $code" }
Write-Host "installed to $dir"

Say "the installed binaries are the release's"
$builds = @{ "cpcpub.exe" = "cpcpub-windows-arm64.exe" }
if ($Machine -eq "x64") {
    $builds = @{ "cpcpub.exe" = "cpcpub-windows-x86_64.exe"; "cpcpub-v3.exe" = "cpcpub-windows-x86_64-v3.exe" }
}
# The other machine's build must not be there at all: on Arm, Windows would
# run an x64 benchmark under emulation and report the emulator's numbers as
# the processor's.
if ($Machine -eq "arm64" -and (Test-Path (Join-Path $dir "cpcpub-v3.exe"))) { throw "an x64 build was installed on Arm" }
foreach ($name in $builds.Keys) {
    $have = Digest (Join-Path $dir $name)
    $want = Digest (Join-Path $Release $builds[$name])
    if ($have -ne $want) { throw "$name is $have, $($builds[$name]) is $want" }
    Write-Host "$name = $($builds[$name]) ($have)"
}

Say "on PATH, in the Start menu, in Apps"
if (@(PathEntries).Count -ne 1) { throw "$dir is on the machine PATH $(@(PathEntries).Count) times" }
Write-Host "PATH has $dir"
$lnk = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\cpcpub.lnk"
if (-not (Test-Path $lnk)) { throw "no Start-menu shortcut at $lnk" }
$target = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk).TargetPath
if ($target -ne (Join-Path $dir "gui\cpcpub-gui.exe")) { throw "the shortcut points at $target" }
Write-Host "Start menu: $lnk -> $target"
$app = Get-ItemProperty "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*" |
    Where-Object { $_.DisplayName -eq "cpcpub" }
if (-not $app) { throw "cpcpub is not in the list of installed apps" }
Write-Host "Apps: $($app.DisplayName) $($app.DisplayVersion) by $($app.Publisher)"
if ($app.Publisher -ne "`u{0141}ukasz Sobala") { throw "the publisher reads $($app.Publisher)" }

Say "the benchmark runs and names itself"
$exe = Join-Path $dir "cpcpub.exe"
& $exe --version
if ($LASTEXITCODE -ne 0) { throw "cpcpub --version exited $LASTEXITCODE" }
$run = Join-Path $work "run.json"
& $exe --threads 1 --cpus 0 --time 0.05 --reps 1 --warmup 0.02 --json | Set-Content -Encoding utf8 $run
if ($LASTEXITCODE -ne 0) { throw "the run exited $LASTEXITCODE" }
$doc = Get-Content -Raw $run | ConvertFrom-Json
if ($doc.build.binary_sha256 -ne (Digest $exe)) { throw "the run says $($doc.build.binary_sha256)" }
Write-Host "binary_sha256 matches the installed file"

Say "the window runs the benchmark"
$smoke = Join-Path $work "smoke"
New-Item -ItemType Directory -Force $smoke | Out-Null
$report = Join-Path $smoke "report.json"
$env:CPCPUB_GUI_SMOKE = $report
# Cairo: a runner has no GPU for GTK's default renderer to find.
$env:GSK_RENDERER = "cairo"
$gui = Start-Process (Join-Path $dir "gui\cpcpub-gui.exe") -PassThru
if (-not $gui.WaitForExit(240000)) { $gui.Kill(); throw "the window was still open after four minutes" }
Remove-Item Env:CPCPUB_GUI_SMOKE, Env:GSK_RENDERER
if (-not (Test-Path $report)) {
    if (Test-Path "$report.log") { Get-Content "$report.log" }
    throw "the window wrote no report (exit code $($gui.ExitCode))"
}
$r = Get-Content -Raw $report | ConvertFrom-Json
Write-Host "binary=$($r.binary) gtk=$($r.gtk) exit=$($r.exit) docs=$($r.docs) saved=$($r.saved)"
if ($r.binary -ne $exe) { throw "the window found $($r.binary), not $exe" }
if ($r.exit -ne 0 -or $r.docs -ne 1) { Write-Host $r.log; throw "the window's run failed" }
if (-not $r.saved) { throw "the window saved no result" }
Write-Host "GTK $($r.gtk): found $exe, ran it, saved the result"

Say "install again over it"
$code = Silent $Setup (Join-Path $work "reinstall.log")
if ($code -ne 0) { throw "setup exited $code the second time" }
if (@(PathEntries).Count -ne 1) { throw "$dir is on the machine PATH $(@(PathEntries).Count) times now" }
if ((Digest $exe) -ne (Digest (Join-Path $Release $builds["cpcpub.exe"]))) { throw "cpcpub.exe changed" }
if (@(Get-ItemProperty "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*" |
        Where-Object { $_.DisplayName -eq "cpcpub" }).Count -ne 1) { throw "cpcpub is in Apps more than once" }
Write-Host "still one install, one PATH entry"

Say "uninstall"
$uninstaller = $app.UninstallString.Trim('"')
$code = Silent $uninstaller (Join-Path $work "uninstall.log")
if ($code -ne 0) { throw "the uninstaller exited $code" }
if (Test-Path $dir) { Get-ChildItem -Recurse $dir | Select-Object -First 20; throw "$dir is still there" }
if (Test-Path $lnk) { throw "the shortcut is still there" }
if (PathEntries) { throw "$dir is still on PATH" }
if (Get-ItemProperty "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*" |
        Where-Object { $_.DisplayName -eq "cpcpub" }) { throw "cpcpub is still in Apps" }
Write-Host "removed cleanly"
