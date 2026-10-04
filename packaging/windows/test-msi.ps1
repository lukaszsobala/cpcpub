# Install a cpcpub MSI on this machine and check it the way test-inside.sh
# checks a Linux package: the installed benchmark is the release's to the byte,
# it is on PATH, it runs and names itself, the Start-menu window finds it, runs
# it and reads the result back -- and uninstalling takes all of it away again.
#
#   pwsh packaging/windows/test-msi.ps1 -Msi X.msi -Release DIR -Machine x64|arm64 [-Other Y.msi]
#
# -Other is the installer for the other machine, which has to refuse to
# install here: on Arm the x64 benchmark would run under emulation and report
# the emulator's numbers as the processor's.
param(
    [Parameter(Mandatory)] [string] $Msi,
    [Parameter(Mandatory)] [string] $Release,
    [Parameter(Mandatory)] [ValidateSet("x64", "arm64")] [string] $Machine,
    [string] $Other = ""
)
$ErrorActionPreference = "Stop"
$dir = Join-Path $env:ProgramFiles "cpcpub"
$work = Join-Path $env:RUNNER_TEMP "msi-test"
New-Item -ItemType Directory -Force $work | Out-Null

function Say($text) { Write-Host "`n== $text" }

function Msiexec([string[]] $arguments, [string] $log) {
    $p = Start-Process msiexec.exe -ArgumentList ($arguments + @("/qn", "/norestart", "/l*v", "`"$log`"")) -Wait -PassThru
    return $p.ExitCode
}

function Digest($path) { (Get-FileHash -Algorithm SHA256 $path).Hash.ToLower() }

if ($Other) {
    Say "the other machine's installer refuses"
    $log = Join-Path $work "other.log"
    $code = Msiexec @("/i", "`"$Other`"") $log
    # 1603 is the fatal error a failed launch condition ends in.
    if ($code -eq 0) { throw "$Other installed on a $Machine machine" }
    $said = Select-String -Path $log -Pattern "This is the cpcpub installer for" -SimpleMatch -Quiet
    if (-not $said) { Get-Content $log -Tail 40; throw "$Other failed ($code), but not over the machine" }
    Write-Host "refused with exit code $code, saying why"
}

Say "install"
$log = Join-Path $work "install.log"
$code = Msiexec @("/i", "`"$Msi`"") $log
if ($code -ne 0) { Get-Content $log -Tail 60; throw "msiexec /i exited $code" }
Write-Host "installed to $dir"

Say "the installed binaries are the release's"
$builds = @{ "cpcpub.exe" = "cpcpub-windows-arm64.exe" }
if ($Machine -eq "x64") {
    $builds = @{ "cpcpub.exe" = "cpcpub-windows-x86_64.exe"; "cpcpub-v3.exe" = "cpcpub-windows-x86_64-v3.exe" }
}
foreach ($name in $builds.Keys) {
    $have = Digest (Join-Path $dir $name)
    $want = Digest (Join-Path $Release $builds[$name])
    if ($have -ne $want) { throw "$name is $have, $($builds[$name]) is $want" }
    Write-Host "$name = $($builds[$name]) ($have)"
}

Say "on PATH, in the Start menu, in Apps"
$path = [Environment]::GetEnvironmentVariable("Path", "Machine") -split ";"
if (-not ($path | Where-Object { $_.TrimEnd("\") -eq $dir })) { throw "$dir is not on the machine PATH" }
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

Say "uninstall"
$code = Msiexec @("/x", "`"$Msi`"") (Join-Path $work "uninstall.log")
if ($code -ne 0) { throw "msiexec /x exited $code" }
if (Test-Path $dir) { Get-ChildItem -Recurse $dir | Select-Object -First 20; throw "$dir is still there" }
if (Test-Path $lnk) { throw "the shortcut is still there" }
$path = [Environment]::GetEnvironmentVariable("Path", "Machine") -split ";"
if ($path | Where-Object { $_.TrimEnd("\") -eq $dir }) { throw "$dir is still on PATH" }
Write-Host "removed cleanly"
