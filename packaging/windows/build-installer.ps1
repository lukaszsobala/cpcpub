# Build the Windows installer from a release's binaries and the window's
# folder:
#
#   pwsh packaging/windows/build-installer.ps1 -Release DIR -Gui DIR -Version V -Out DIR
#
# -> OUT\cpcpub-VERSION-windows-setup.exe, for both x64 and Arm64 machines; see
# cpcpub.iss for what goes in. -Gui is the folder build-gui.sh made
# (dist\cpcpub-gui). Needs Inno Setup 6.3 or newer (winget install
# JRSoftware.InnoSetup, or choco install innosetup).
param(
    [Parameter(Mandatory)] [string] $Release,
    [Parameter(Mandatory)] [string] $Gui,
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
$inno = [version](Get-Item $iscc).VersionInfo.ProductVersion.Split(" ")[0]
# 6.3 for x64os, IsX64OS and IsArm64: the arm64-aware architecture checks.
if ($inno -lt [version]"6.3") { throw "Inno Setup $inno is too old; 6.3 or newer tells x64 and Arm64 apart" }
Write-Host "Inno Setup $inno at $iscc"

$Release = (Resolve-Path $Release).Path
$Gui = (Resolve-Path $Gui).Path
foreach ($name in "cpcpub-windows-x86_64.exe", "cpcpub-windows-x86_64-v3.exe", "cpcpub-windows-arm64.exe") {
    if (-not (Test-Path (Join-Path $Release $name))) { throw "$Release has no $name" }
}
if (-not (Test-Path (Join-Path $Gui "cpcpub-gui.exe"))) { throw "$Gui has no cpcpub-gui.exe" }
New-Item -ItemType Directory -Force $Out | Out-Null
$Out = (Resolve-Path $Out).Path

& $iscc /Qp "/DVersion=$Version" "/DRel=$Release" "/DGui=$Gui" "/DTop=$top" "/O$Out" `
    (Join-Path $PSScriptRoot "cpcpub.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC exited $LASTEXITCODE" }
$setup = Get-Item (Join-Path $Out "cpcpub-$Version-windows-setup.exe")
Write-Host ("{0}: {1:N1} MiB" -f $setup.Name, ($setup.Length / 1MB))
