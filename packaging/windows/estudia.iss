; Instalador de Estudia para Windows (Inno Setup 6.3 o más nuevo).
; Antes: `pyinstaller packaging/windows/estudia.spec` (deja dist\Estudia\).
; Compilar: `iscc packaging\windows\estudia.iss` -> dist\EstudiaSetup-<versión>.exe
; La versión sale del Estudia.exe compilado (su version_info la escribe
; scripts/version_from_tag.py); se puede forzar con `iscc /DAppVersion=X.Y.Z`.
; Prueba sin ventanas: EstudiaSetup-X.Y.Z.exe /VERYSILENT /SUPPRESSMSGBOXES /CURRENTUSER
; Este archivo va en UTF-8 con BOM para que Inno lea bien los acentos.

#define AppExe "Estudia.exe"
#define HandlerExe "EstudiaHandler.exe"
#define BuildDir AddBackslash(SourcePath) + "..\..\dist\Estudia"
#ifndef AppVersion
  #define AppVersion GetStringFileInfo(BuildDir + "\" + AppExe, "ProductVersion")
#endif
#if AppVersion == ""
  #error No se pudo leer la versión de dist\Estudia\Estudia.exe: corré PyInstaller antes o pasá /DAppVersion=X.Y.Z
#endif

[Setup]
; AppId identifica la instalación para actualizar y desinstalar. NUNCA cambiarlo:
; con otro valor, Windows vería una app distinta y quedarían dos instalaciones.
AppId={{C97271B7-728C-4CA8-BA43-2D7C80A65B2E}
AppName=Estudia
AppVersion={#AppVersion}
AppVerName=Estudia {#AppVersion}
AppPublisher=JimmyAlter
AppPublisherURL=https://github.com/JimmyAlter/estudia
AppSupportURL=https://github.com/JimmyAlter/estudia/issues
AppUpdatesURL=https://github.com/JimmyAlter/estudia/releases
VersionInfoVersion={#AppVersion}
; Por usuario, sin permisos de administrador.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Estudia
DisableProgramGroupPage=yes
DisableWelcomePage=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=EstudiaSetup-{#AppVersion}
SetupIconFile=..\..\src\estudia\assets\icono.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName=Estudia
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Estudia abierta se cierra en [Code] (no tiene ventana a la que pedirle que cierre).
CloseApplications=no

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Messages]
WelcomeLabel2=Se va a instalar [name/ver] en tu computadora.%n%nEstudia va a recibir el regreso del inicio de sesión del campus.%n%nCuando estés listo, hacé clic en Siguiente.

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el escritorio"; GroupDescription: "Accesos directos:"; Flags: unchecked

[InstallDelete]
; Al actualizar, que no queden módulos de la versión anterior mezclados con los nuevos.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Estudia"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\Estudia"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; Registra estudia:// y moodlemobile:// (HKCU) para que vuelva el login del campus.
Filename: "{app}\{#HandlerExe}"; Parameters: "--install"; StatusMsg: "Preparando el inicio de sesión del campus..."; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "Abrir Estudia"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Borra estudia:// y devuelve moodlemobile:// a la app que lo tenía antes.
Filename: "{app}\{#HandlerExe}"; Parameters: "--uninstall"; RunOnceId: "UnregisterSchemes"; Flags: runhidden waituntilterminated

[Code]
const
  DataQuestion = '¿Borrar también tus datos de estudio?';

var
  DataDir: String;
  DeleteData: Boolean;

{ Cierra Estudia (y sus procesos hijos) solo si está abierta. Los errores no importan. }
procedure CloseRunningApp();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{cmd}'),
    '/S /C ""' + ExpandConstant('{sys}\tasklist.exe') + '" /FI "IMAGENAME eq {#AppExe}" /NH'
    + ' | "' + ExpandConstant('{sys}\find.exe') + '" /I "{#AppExe}" >NUL'
    + ' && "' + ExpandConstant('{sys}\taskkill.exe') + '" /IM {#AppExe} /T /F >NUL"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if ResultCode = 0 then
    Sleep(1000); { que Windows suelte los archivos del proceso cerrado }
end;

{ Carpeta de datos de Estudia. Es lo mismo que platformdirs.user_data_dir("estudia")
  en Windows (estudia.config.data_path()); `EstudiaHandler.exe --data-dir` la imprime
  para usarla desde scripts. Si data_path() cambia, hay que cambiar esto también. }
function StudyDataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\estudia\estudia');
end;

{ Borra la clave de IA y los tokens del campus del Administrador de credenciales. }
procedure ForgetSecrets();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{app}\{#HandlerExe}'), '--forget-secrets', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  { Al actualizar con Estudia abierta, sus archivos están en uso. }
  CloseRunningApp();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    { Antes de [UninstallRun] y de borrar los archivos. }
    CloseRunningApp();
    DataDir := StudyDataDir();
    DeleteData := False;
    { En silencio no se pregunta: los datos se quedan. }
    if (not UninstallSilent()) and DirExists(DataDir) then
      DeleteData := MsgBox(DataQuestion, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
    { Mientras EstudiaHandler.exe todavía está instalado. }
    if DeleteData then
      ForgetSecrets();
  end
  else if CurUninstallStep = usPostUninstall then
  begin
    if DeleteData then
    begin
      DelTree(DataDir, True, True, True);
      { %LOCALAPPDATA%\estudia queda vacía: se va solo si no tiene nada más. }
      RemoveDir(ExtractFileDir(DataDir));
    end;
  end;
end;
