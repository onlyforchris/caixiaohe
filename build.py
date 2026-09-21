# -*- coding: utf-8 -*-
"""构建安装包 —— PyInstaller 打包 + Inno Setup 生成 Setup.exe。

用法：
  python build.py              构建 exe + 安装包
  python build.py --exe-only   仅构建 exe，不生成安装包

前置条件：
  - pip install pyinstaller pywebview pystray pillow
  - Inno Setup 6（安装包编译器，免费下载：https://jrsoftware.org/isdl.php）
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from financekit.paths import APP_VERSION


DIST_DIR = os.path.join(HERE, "dist")
BUILD_DIR = os.path.join(HERE, "build")
EXE_NAME = "财小盒"
EXE_PATH = os.path.join(DIST_DIR, f"{EXE_NAME}.exe")
ISS_PATH = os.path.join(HERE, f"{EXE_NAME}.iss")
SETUP_PATH = os.path.join(DIST_DIR, f"{EXE_NAME}-v{APP_VERSION}-Setup.exe")

ISCC_CANDIDATES = [
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]


def find_iscc():
    for p in ISCC_CANDIDATES:
        if os.path.exists(p):
            return p
    return shutil.which("iscc")


def build_exe():
    print("=" * 50)
    print(f"  构建 {EXE_NAME} v{APP_VERSION}")
    print("=" * 50)
    print()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", EXE_NAME,
        "--icon", os.path.join(HERE, "app.ico"),
        "--add-data", f"{os.path.join(HERE, 'index.html')};.",
        "--add-data", f"{os.path.join(HERE, 'app.ico')};.",
        "--add-data", f"{os.path.join(HERE, 'financekit')};financekit",
        "--hidden-import", "pywebview",
        "--hidden-import", "pystray",
        "--hidden-import", "PIL",
        "--hidden-import", "rapidocr_onnxruntime",
        "--noconfirm",
        "--clean",
        os.path.join(HERE, "desktop.py"),
    ]

    print("运行 PyInstaller ...")
    r = subprocess.run(cmd, cwd=HERE)
    if r.returncode != 0:
        print("\n[错误] PyInstaller 打包失败")
        return False

    if not os.path.exists(EXE_PATH):
        print(f"\n[错误] 未找到输出文件：{EXE_PATH}")
        return False

    size_mb = os.path.getsize(EXE_PATH) / 1024 / 1024
    print(f"\n  OK: {EXE_PATH} ({size_mb:.1f} MB)")
    return True


def generate_iss():
    iss_content = f"""; Inno Setup 脚本 —— 由 build.py 自动生成
#define MyAppName "财小盒"
#define MyAppVersion "{APP_VERSION}"
#define MyAppPublisher "Chris"
#define MyAppExeName "{EXE_NAME}.exe"

[Setup]
AppId={{{{A7D3E5F1-8B2C-4D6A-9E1F-3C5A7B9D2E4F}}
AppName={{#MyAppName}}
AppVersion={{#MyAppVersion}}
AppPublisher={{#MyAppPublisher}}
DefaultDirName={{autopf}}\\{{#MyAppName}}
DefaultGroupName={{#MyAppName}}
AllowNoIcons=yes
OutputDir={DIST_DIR.replace(os.sep, '/')}
OutputBaseFilename={EXE_NAME}-v{APP_VERSION}-Setup
SetupIconFile={os.path.join(HERE, 'app.ico').replace(os.sep, '/')}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={{app}}\\{{#MyAppExeName}}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式(&D)"; GroupDescription: "附加选项:"
Name: "quicklaunchicon"; Description: "创建快速启动栏快捷方式(&Q)"; GroupDescription: "附加选项:"; Flags: unchecked

[Files]
Source: "{EXE_PATH.replace(os.sep, '/')}"; DestDir: "{{app}}"; Flags: ignoreversion

[Icons]
Name: "{{group}}\\{{#MyAppName}}"; Filename: "{{app}}\\{{#MyAppExeName}}"
Name: "{{group}}\\卸载 {{#MyAppName}}"; Filename: "{{uninstallexe}}"
Name: "{{autodesktop}}\\{{#MyAppName}}"; Filename: "{{app}}\\{{#MyAppExeName}}"; Tasks: desktopicon

[Run]
Filename: "{{app}}\\{{#MyAppExeName}}"; Description: "立即运行 {{#MyAppName}}"; Flags: nowait postinstall skipifsilent
"""
    with open(ISS_PATH, "w", encoding="utf-8") as f:
        f.write(iss_content)
    print(f"  已生成：{ISS_PATH}")


def compile_iss():
    iscc = find_iscc()
    if not iscc:
        print("\n[提示] 未找到 Inno Setup 6，跳过安装包编译。")
        print("  下载安装：https://jrsoftware.org/isdl.php")
        print(f"  安装后执行：ISCC \"{ISS_PATH}\"")
        print(f"  或重新运行：python build.py")
        return False

    print(f"\n编译安装包（{iscc}）...")
    r = subprocess.run([iscc, ISS_PATH])
    if r.returncode != 0:
        print("\n[错误] Inno Setup 编译失败")
        return False

    if os.path.exists(SETUP_PATH):
        size_mb = os.path.getsize(SETUP_PATH) / 1024 / 1024
        print(f"\n  OK: {SETUP_PATH} ({size_mb:.1f} MB)")
    return True


def main():
    ap = argparse.ArgumentParser(description="构建财小盒安装包")
    ap.add_argument("--exe-only", action="store_true", help="仅构建 exe，不生成安装包")
    args = ap.parse_args()

    if not build_exe():
        return 1

    if args.exe_only:
        return 0

    generate_iss()
    compile_iss()

    print()
    print("=" * 50)
    print("  构建完成！")
    print("=" * 50)
    print()
    print(f"  exe:       {EXE_PATH}")
    if os.path.exists(SETUP_PATH):
        print(f"  安装包:    {SETUP_PATH}")
    print()
    print("  分发方式：将 Setup.exe 上传到 GitHub Releases")
    print("  用户下载后双击安装，桌面出图标，双击即用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
