# -*- coding: utf-8 -*-
"""共享路径与版本常量。

打包模式（PyInstaller）与源码模式的差异：
  - APP_DIR：只读资源目录（index.html、app.ico）。打包后指向 _MEIPASS 临时解压目录。
  - USER_DIR：可写用户数据目录（config.json、financekit.db 等）。
    打包后指向 ~/财小盒/，源码模式下与 APP_DIR 相同（向后兼容）。
"""
import os
import shutil
import sys

FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    APP_DIR = sys._MEIPASS
else:
    APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if FROZEN:
    USER_DIR = os.path.join(os.path.expanduser("~"), "财小盒")
else:
    USER_DIR = APP_DIR

STATIC = os.path.join(APP_DIR, "index.html")
ICON = os.path.join(APP_DIR, "app.ico")

CONFIG = os.path.join(USER_DIR, "config.json")
LEDGER = os.path.join(USER_DIR, "invoice_ledger.json")
USED_LEDGER = os.path.join(USER_DIR, "used_ledger.json")
DB_PATH = os.path.join(USER_DIR, "financekit.db")
BACKUP_DIR = os.path.join(USER_DIR, "backups")
ERROR_LOG = os.path.join(USER_DIR, "finance_error.log")

ENGINE_VER = 10
APP_VERSION = "1.6.0"
INVOICE_EXTS = {".pdf", ".ofd", ".jpg", ".jpeg", ".png", ".bmp", ".webp"}
BUYER_DEFAULT = []
BACKUP_KEEP = 5
VERIFY_CHOICES = ("未查验", "已查验通过", "查验异常")
REJECT_CHOICES = ("", "重复报销", "抬头/税号不符", "疑似假票", "金额不符", "票面信息不全", "其他")

LEDGER_STATUS = {"error": None, "backups": [], "record_count": 0}


def ensure_user_dir():
    """确保 USER_DIR 存在；首次运行时从 APP_DIR 迁移旧用户数据。"""
    if os.path.isdir(USER_DIR):
        return
    os.makedirs(USER_DIR, exist_ok=True)
    if FROZEN:
        exe_dir = os.path.dirname(sys.executable)
        for name in ("config.json", "invoice_ledger.json", "used_ledger.json", "financekit.db"):
            src = os.path.join(exe_dir, name)
            if os.path.exists(src):
                shutil.move(src, os.path.join(USER_DIR, name))
