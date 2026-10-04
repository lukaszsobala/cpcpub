; The Windows installer: the benchmark, on PATH, and the window, in the Start
; menu, in one setup program for both x64 and Arm64 machines. Compiled by
; build-installer.ps1 with Inno Setup 6.3 or newer, which passes:
;
;   Version   the release, plain numbers and dots
;   Rel       a folder holding the release's Windows binaries
;   Gui       the PyInstaller folder of the window (build-gui.sh)
;   Top       the top of the source tree, for the icon and the license
;
; The file starts with a byte-order mark so that the compiler reads it as
; UTF-8, which the publisher's name needs.
;
; Every binary goes in as the release has it, and Inno Setup copies files
; without touching them: a hub verifies a result by the digest of the binary
; that measured it.

#ifndef Version
  #error Compile with /DVersion=, /DRel=, /DGui= and /DTop=; see build-installer.ps1.
#endif

[Setup]
; Fixed for good: Setup recognises an earlier install by it, and installs a
; new release over it, keeping the folder and the choices made then.
AppId={{E4AA394E-6191-4C59-9691-469E371779B8}
AppName=cpcpub
AppVersion={#Version}
AppVerName=cpcpub {#Version}
AppPublisher=Łukasz Sobala
AppPublisherURL=https://github.com/lukaszsobala/cpcpub
AppSupportURL=https://github.com/lukaszsobala/cpcpub/issues
AppUpdatesURL=https://github.com/lukaszsobala/cpcpub/releases
AppComments=A CPU benchmark: measure the processor's cores and compare the results
VersionInfoVersion={#Version}
VersionInfoDescription=cpcpub setup
DefaultDirName={autopf}\cpcpub
DisableProgramGroupPage=yes
UninstallDisplayName=cpcpub
UninstallDisplayIcon={app}\cpcpub.ico
SetupIconFile={#Top}\packaging\icons\cpcpub.ico
WizardStyle=modern
; Program Files and the machine-wide PATH.
PrivilegesRequired=admin
; GTK 4 needs Windows 10.
MinVersion=10.0
; x64os is x64 Windows and nothing else. An Arm machine would run x64 code too,
; under emulation, and an x64 benchmark there would report the emulator's
; speed as the processor's; so each machine gets its own build, picked by the
; Check functions below.
ArchitecturesAllowed=x64os arm64
ArchitecturesInstallIn64BitMode=x64os arm64
ChangesEnvironment=yes
Compression=lzma2/max
SolidCompression=yes
OutputBaseFilename=cpcpub-{#Version}-windows-setup

[Types]
Name: "full"; Description: "The benchmark and the window"
Name: "cli"; Description: "The benchmark only, for the command line"
Name: "custom"; Description: "Custom"; Flags: iscustom

[Components]
Name: "benchmark"; Description: "The benchmark, cpcpub.exe"; Types: full cli custom; Flags: fixed
Name: "gui"; Description: "The window, in the Start menu"; Types: full

[Tasks]
Name: "path"; Description: "Add cpcpub to PATH, so that a new terminal finds it"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Components: gui; Flags: unchecked

[InstallDelete]
; The window is a frozen Python with GTK, whose files change names from one
; release to the next; a new release's window starts from an empty folder.
Type: filesandordirs; Name: "{app}\gui"

[Files]
; The baseline build is always cpcpub.exe: it runs on every machine of its
; kind, and it is what results compare against. x64 also gets the x86-64-v3
; build beside it, as the Linux package does; the window's Browse picks it.
Source: "{#Rel}\cpcpub-windows-x86_64.exe"; DestDir: "{app}"; DestName: "cpcpub.exe"; Check: IsX64OS; Flags: ignoreversion
Source: "{#Rel}\cpcpub-windows-x86_64-v3.exe"; DestDir: "{app}"; DestName: "cpcpub-v3.exe"; Check: IsX64OS; Flags: ignoreversion
Source: "{#Rel}\cpcpub-windows-arm64.exe"; DestDir: "{app}"; DestName: "cpcpub.exe"; Check: IsArm64; Flags: ignoreversion
Source: "{#Top}\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "{#Top}\bench\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Top}\packaging\icons\cpcpub.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Gui}\*"; DestDir: "{app}\gui"; Components: gui; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\cpcpub"; Filename: "{app}\gui\cpcpub-gui.exe"; WorkingDir: "{app}\gui"; Comment: "Measure the processor's cores and compare the results"; Components: gui
Name: "{autodesktop}\cpcpub"; Filename: "{app}\gui\cpcpub-gui.exe"; WorkingDir: "{app}\gui"; Comment: "Measure the processor's cores and compare the results"; Tasks: desktopicon

[Run]
Filename: "{app}\gui\cpcpub-gui.exe"; Description: "Start cpcpub"; Components: gui; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\gui"

[Code]
const
  EnvironmentKey = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';

{ Path without its entries for Dir, compared as Windows compares them:
  ignoring case and a trailing backslash. Everything else is kept as it was,
  empty entries and all. }
function PathWithout(Path, Dir: String): String;
var
  Rest, Entry: String;
  I: Integer;
  First: Boolean;
begin
  Result := '';
  First := True;
  Dir := Lowercase(RemoveBackslashUnlessRoot(Dir));
  Rest := Path + ';';
  while Rest <> '' do begin
    I := Pos(';', Rest);
    Entry := Copy(Rest, 1, I - 1);
    Delete(Rest, 1, I);
    if Lowercase(RemoveBackslashUnlessRoot(Entry)) <> Dir then begin
      if not First then
        Result := Result + ';';
      Result := Result + Entry;
      First := False;
    end;
  end;
end;

{ The install folder on the machine PATH, once, or off it. Done here rather
  than in [Registry]: that could append the folder, but not take it away
  again on uninstall without deleting the whole of PATH. }
procedure SetOnPath(OnPath: Boolean);
var
  Path, Changed, Dir: String;
begin
  if not RegQueryStringValue(HKLM, EnvironmentKey, 'Path', Path) then
    Path := '';
  Dir := ExpandConstant('{app}');
  Changed := PathWithout(Path, Dir);
  if OnPath then begin
    if (Changed <> '') and (Changed[Length(Changed)] <> ';') then
      Changed := Changed + ';';
    Changed := Changed + Dir;
  end;
  if Changed = Path then
    exit;
  if not RegWriteExpandStringValue(HKLM, EnvironmentKey, 'Path', Changed) then
    RaiseException('Could not write the machine PATH.');
  Log('PATH: ' + Changed);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    SetOnPath(WizardIsTaskSelected('path'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    SetOnPath(False);
end;
