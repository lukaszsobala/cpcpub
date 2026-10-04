# Build the Windows installer from a release's binaries and the window's
# folder:
#
#   pwsh packaging/windows/build-installer.ps1 -Release DIR -GuiX64 DIR -GuiArm64 DIR -Version V -Out DIR
#
# -> OUT\cpcpub-VERSION-windows-setup.exe, for both x64 and Arm64 machines; see
# cpcpub.iss for what goes in. -GuiX64 and -GuiArm64 are the folders
# build-gui.sh made (dist\cpcpub-gui) on each machine. Needs Inno Setup 6.3
# or newer (winget install JRSoftware.InnoSetup, or choco install innosetup).
param(
    [Parameter(Mandatory)] [string] $Release,
    [Parameter(Mandatory)] [string] $GuiX64,
    [Parameter(Mandatory)] [string] $GuiArm64,
    [Parameter(Mandatory)] [string] $Version,
    [Parameter(Mandatory)] [string] $Out
)
$ErrorActionPreference = "Stop"
$top = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Version = $Version -replace "^v", ""
if ($Version -notmatch "^\d+\.\d+\.\d+$") { throw "version $Version is not three numbers" }

$iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    $iscc = @("${env:ProgramFiles(x86)}", "$env:ProgramFiles", "$env:LOCALAPPDATA\Programs") |
        ForEach-Object { Join-Path $_ "Inno Setup 6\ISCC.exe" } |
        Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) { throw "no ISCC.exe: install Inno Setup 6.3 or newer" }
# The version is checked by cpcpub.iss itself, which the preprocessor can
# do reliably; ISCC.exe's version resource does not always carry it.
Write-Host "ISCC at $iscc, file version $((Get-Item $iscc).VersionInfo.FileVersion)"

$Release = (Resolve-Path $Release).Path
$GuiX64 = (Resolve-Path $GuiX64).Path
$GuiArm64 = (Resolve-Path $GuiArm64).Path
foreach ($name in "cpcpub-windows-x86_64.exe", "cpcpub-windows-x86_64-v3.exe", "cpcpub-windows-arm64.exe") {
    if (-not (Test-Path (Join-Path $Release $name))) { throw "$Release has no $name" }
}
# Each window folder for the machine it claims to be for, read off the
# executable's PE header: a mix-up would install an emulated window.
foreach ($gui in @(@($GuiX64, 0x8664), @($GuiArm64, 0xAA64))) {
    $exe = Join-Path $gui[0] "cpcpub-gui.exe"
    if (-not (Test-Path $exe)) { throw "$($gui[0]) has no cpcpub-gui.exe" }
    $bytes = [IO.File]::ReadAllBytes($exe)
    $machine = [BitConverter]::ToUInt16($bytes, [BitConverter]::ToInt32($bytes, 0x3C) + 4)
    if ($machine -ne $gui[1]) { throw ("{0} is for machine 0x{1:X4}, not 0x{2:X4}" -f $exe, $machine, $gui[1]) }
}
New-Item -ItemType Directory -Force $Out | Out-Null
$Out = (Resolve-Path $Out).Path

& $iscc /Qp "/DVersion=$Version" "/DRel=$Release" "/DGuiX64=$GuiX64" "/DGuiArm64=$GuiArm64" "/DTop=$top" "/O$Out" `
    (Join-Path $PSScriptRoot "cpcpub.iss")
Write-Host ""   # /Qp leaves its progress line unfinished
if ($LASTEXITCODE -ne 0) { throw "ISCC exited $LASTEXITCODE" }
$setup = Get-Item (Join-Path $Out "cpcpub-$Version-windows-setup.exe")
Write-Host ("{0}: {1:N1} MiB" -f $setup.Name, ($setup.Length / 1MB))
