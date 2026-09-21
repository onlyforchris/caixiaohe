# -*- coding: utf-8 -*-
"""财小盒 - 安装 / 升级 / 卸载

用法：
  python install.py            安装（创建虚拟环境 + 安装依赖 + 桌面快捷方式）
  python install.py --upgrade  升级到最新版本（从 GitHub 下载）
  python install.py --uninstall  卸载（删除快捷方式 + 删除虚拟环境）
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


# ── 工具函数 ──────────────────────────────────────────────

def find_python():
    """找到可用的 Python 解释器（优先 py -3，其次 python）。"""
    for cmd in [["py", "-3"], ["python"]]:
        try:
            r = subprocess.run(
                cmd + ["-c", "import sys;sys.exit(0 if sys.version_info>=(3,8) else 1)"],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0:
                return cmd
        except Exception:
            continue
    return None


def get_desktop():
    return os.path.join(os.path.expanduser("~"), "Desktop")


def get_startmenu():
    return os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft", "Windows", "Start Menu", "Programs",
    )


def create_shortcut(name, target, work_dir, description, icon=None):
    """用 PowerShell 创建 .lnk 快捷方式。"""
    icon_line = f"$Shortcut.IconLocation = '{icon}'" if icon else ""
    ps = f"""
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut('{name}')
$s.TargetPath = '{target}'
$s.WorkingDirectory = '{work_dir}'
$s.Description = '{description}'
{icon_line}
$s.Save()
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)


def remove_shortcut(name):
    """删除桌面和开始菜单中的快捷方式。"""
    for folder in [get_desktop(), get_startmenu()]:
        lnk = os.path.join(folder, name)
        if os.path.exists(lnk):
            os.remove(lnk)
            print(f"  已删除：{lnk}")


# ── 安装 ──────────────────────────────────────────────────

def do_install(args):
    print("=" * 50)
    print("  财小盒 - 安装")
    print("=" * 50)
    print()

    py = find_python()
    if not py:
        print("[错误] 未找到 Python 3.8+，请先安装。")
        print("  下载：https://www.python.org/downloads/windows/")
        print("  安装时勾选 'Add python.exe to PATH'")
        return 1

    print(f"Python: {' '.join(py)}")

    venv_dir = os.path.join(HERE, ".venv")
    pyexe = os.path.join(venv_dir, "Scripts", "python.exe")

    if not os.path.exists(pyexe):
        print("创建虚拟环境 .venv ...")
        r = subprocess.run(py + ["-m", "venv", venv_dir], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[错误] 创建虚拟环境失败：{r.stderr.strip()}")
            return 1
    else:
        print("虚拟环境已存在，跳过创建。")

    pip = os.path.join(venv_dir, "Scripts", "pip.exe")
    req = os.path.join(HERE, "requirements.txt")
    req_ocr = os.path.join(HERE, "requirements-ocr.txt")

    print("安装依赖（清华源，失败自动回退官方源）...")
    r = subprocess.run(
        [pip, "install", "-r", req, "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print("  清华源失败，回退官方源...")
        r = subprocess.run([pip, "install", "-r", req], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[错误] 依赖安装失败：{r.stderr.strip()[:200]}")
        return 1
    print("  核心依赖 OK")

    print("检查本地 OCR 组件（可选，约 100MB）...")
    r = subprocess.run(
        [pip, "show", "rapidocr-onnxruntime"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        r2 = subprocess.run(
            [pip, "install", "-r", req_ocr, "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"],
            capture_output=True, text=True,
        )
        if r2.returncode == 0:
            print("  本地 OCR OK")
        else:
            print("  本地 OCR 未安装（不影响使用，图片票可用 AI 识别）")
    else:
        print("  本地 OCR 已安装")

    icon = os.path.join(HERE, "app.ico")
    exe_for_shortcut = pyexe.replace("python.exe", "pythonw.exe")
    if not os.path.exists(exe_for_shortcut):
        exe_for_shortcut = pyexe

    print("创建快捷方式...")
    for folder_fn, label in [(get_desktop, "桌面"), (get_startmenu, "开始菜单")]:
        folder = folder_fn()
        lnk = os.path.join(folder, "财小盒.lnk")
        try:
            create_shortcut(
                lnk,
                exe_for_shortcut,
                HERE,
                "财小盒 - 本地发票管理",
                icon if os.path.exists(icon) else None,
            )
            shortcut_target = os.path.join(folder, "财小盒.lnk")
            print(f"  [{label}] {shortcut_target}")
        except Exception as e:
            print(f"  [{label}] 创建失败：{e}")

    print()
    print("=" * 50)
    print("  安装完成！")
    print("=" * 50)
    print()
    print("启动方式：")
    print("  - 双击桌面「财小盒」快捷方式")
    print("  - 或双击 启动桌面版.bat")
    print("  - 命令行：cli.bat scan / cli.bat export")
    print()
    print("升级：python install.py --upgrade")
    print("卸载：python install.py --uninstall")
    return 0


# ── 升级 ──────────────────────────────────────────────────

def do_upgrade(args):
    """调用 update.py 从 GitHub 下载最新版本覆盖更新。"""
    update_py = os.path.join(HERE, "update.py")
    if not os.path.exists(update_py):
        print("[错误] update.py 不存在，无法升级。")
        return 1

    venv_py = os.path.join(HERE, ".venv", "Scripts", "python.exe")
    py = [venv_py] if os.path.exists(venv_py) else (find_python() or ["python"])

    cmd = py + [update_py, "--yes"]
    print("正在升级...")
    r = subprocess.run(cmd)
    if r.returncode != 0:
        return r.returncode

    print("重新安装依赖...")
    pip = os.path.join(HERE, ".venv", "Scripts", "pip.exe")
    if os.path.exists(pip):
        req = os.path.join(HERE, "requirements.txt")
        subprocess.run(
            [pip, "install", "-r", req, "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"],
            capture_output=True,
        )

    print("升级完成！请重新启动程序。")
    return 0


# ── 卸载 ──────────────────────────────────────────────────

def do_uninstall(args):
    print("=" * 50)
    print("  财小盒 - 卸载")
    print("=" * 50)
    print()

    confirm = input("确认卸载？配置和数据会保留。输入 Y 继续：").strip().upper()
    if confirm != "Y":
        print("已取消。")
        return 0

    print("删除快捷方式...")
    remove_shortcut("财小盒.lnk")

    venv_dir = os.path.join(HERE, ".venv")
    if os.path.exists(venv_dir):
        print("删除虚拟环境...")
        shutil.rmtree(venv_dir, ignore_errors=True)

    pycache = os.path.join(HERE, "__pycache__")
    if os.path.exists(pycache):
        shutil.rmtree(pycache, ignore_errors=True)

    print()
    print("=" * 50)
    print("  卸载完成！")
    print("=" * 50)
    print()
    print("以下用户数据已保留（可手动删除）：")
    print(f"  config.json      - 配置文件")
    print(f"  financekit.db    - 发票台账数据库")
    print(f"  {HERE}")
    return 0


# ── 入口 ──────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="财小盒 - 安装管理")
    ap.add_argument("--upgrade", action="store_true", help="升级到最新版本")
    ap.add_argument("--uninstall", action="store_true", help="卸载程序")
    args = ap.parse_args()

    if args.upgrade:
        return do_upgrade(args)
    if args.uninstall:
        return do_uninstall(args)
    return do_install(args)


if __name__ == "__main__":
    sys.exit(main())
