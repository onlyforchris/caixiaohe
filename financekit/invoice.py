# -*- coding: utf-8 -*-
"""发票管家核心逻辑（识别 / 查重 / HTTP）。路径与台账存储见 financekit.paths / store。"""
import argparse
import base64
import copy
import faulthandler
import glob
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
import webbrowser
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from financekit.paths import (
    APP_DIR, APP_VERSION, BACKUP_DIR, BACKUP_KEEP, BUYER_DEFAULT, CONFIG,
    ENGINE_VER, ERROR_LOG, INVOICE_EXTS, LEDGER, LEDGER_STATUS, REJECT_CHOICES,
    STATIC, USED_LEDGER, VERIFY_CHOICES,
)
from financekit.store import (
    list_backups, load_ledger, load_used_ledger, restore_ledger, save_ledger,
    save_used_ledger, used_item_key, used_ledger_add,
)
from financekit.parse import *  # noqa: F401,F403
from financekit.parse import _companies, _itinerary_fields, _stem_core  # noqa: F401
from financekit.export import *  # noqa: F401,F403
from financekit.anomaly import check_anomaly

WORKBUDDY_PACKAGES = os.path.expanduser(r"~/.workbuddy/binaries/python/envs/site-packages")
if os.path.isdir(WORKBUDDY_PACKAGES):
    sys.path.append(WORKBUDDY_PACKAGES)
try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None
try:
    import pypdfium2 as pdfium
    from PIL import Image
except Exception:
    pdfium = None
    Image = None
try:
    from rapidocr_onnxruntime import RapidOCR
except Exception:
    RapidOCR = None

STATE_LOCK = threading.RLock()










# ---------- 文件与 OCR ----------
def file_md5(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def zhipu_key():
    """读智谱 API Key：进程环境变量优先，再实时读 Windows 注册表。

    实时读注册表是为了绕开 Windows 经典坑：用户设置"用户级环境变量"后，
    已在运行的 explorer/终端不会刷新环境，之后双击启动的程序全部继承
    旧快照，os.environ 里永远看不到新变量。直接查注册表则立即生效，
    无需注销/重启。
    """
    for name in ("ZHIPUAI_API_KEY", "ZAI_API_KEY"):
        v = os.environ.get(name)
        if v and v.strip():
            return v.strip()
    try:
        import winreg
    except ImportError:  # 非 Windows
        return ""
    for root, path in ((winreg.HKEY_CURRENT_USER, "Environment"),
                       (winreg.HKEY_LOCAL_MACHINE,
                        r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, path) as k:
                for name in ("ZHIPUAI_API_KEY", "ZAI_API_KEY"):
                    try:
                        v, _t = winreg.QueryValueEx(k, name)
                    except OSError:
                        continue
                    if v and v.strip():
                        return v.strip().strip('"')
        except OSError:
            continue
    return ""


def mask_key(key):
    """脱敏展示：只露头尾各 4 位。"""
    key = (key or "").strip()
    return key[:4] + "…" + key[-4:] if len(key) >= 10 else ""


def save_zhipu_key(key):
    """把 Key 写入 Windows 当前用户环境变量（等效于旧版 PowerShell 命令，但无需命令行）。

    写注册表 HKCU\\Environment 后广播 WM_SETTINGCHANGE，之后新开的程序都能读到；
    同时更新当前进程 os.environ，本服务立即生效，无需重启。
    """
    key = (key or "").strip()
    if not key:
        raise ValueError("API Key 不能为空")
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            winreg.SetValueEx(k, "ZHIPUAI_API_KEY", 0, winreg.REG_SZ, key)
        try:  # 通知系统刷新环境变量，让之后新启动的程序立刻可见
            import ctypes
            ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF, 0x001A, 0, "Environment", 0x0002, 1000,
                ctypes.byref(ctypes.c_ulong()))
        except Exception:
            pass
    except ImportError:  # 非 Windows（开发环境）：仅写入当前进程
        pass
    os.environ["ZHIPUAI_API_KEY"] = key


def clear_zhipu_key():
    """删除本机保存的 Key（注册表 + 当前进程）。"""
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            try:
                winreg.DeleteValue(k, "ZHIPUAI_API_KEY")
            except OSError:
                pass
    except ImportError:
        pass
    os.environ.pop("ZHIPUAI_API_KEY", None)


def friendly_ocr_error(error):
    """把供应商错误转换为用户可执行的提示，不暴露响应正文。"""
    code = getattr(error, "code", None)
    if code in (401, 403):
        return "API Key 无效、已删除或无模型权限"
    if code == 429:
        return "调用频率或账户额度受限，请稍后重试"
    text = str(error).lower()
    if "timed out" in text or "timeout" in text:
        return "连接超时，请检查网络后重试"
    return "智谱服务暂不可用，请检查网络或稍后重试"


# 视觉模型优先级表：仅放官方标注"免费"的视觉模型（智谱定价页红框三档）。
# 绝对不引入付费/限时免费档，避免账上产生意料外的扣费。
# 顺序：最新 → 兜底；首个可用即锁定到本进程。
VISION_MODEL_CHAIN = ("glm-4.6v-flash", "glm-4v-flash", "glm-4.1v-thinking-flash")
_ACTIVE_VISION_MODEL = None  # 首次探测成功后锁定到本进程
_PICK_LOCK = threading.Lock()


def _probe_model(name):
    """极简连通性探测：能跑通即视为可用。"""
    zhipu_chat([{"role": "user", "content": "仅回复OK"}],
               model=name, timeout=15, max_tokens=2)


def pick_vision_model():
    """按优先级表自动选视觉模型：首个可用即锁定；所有失败抛出最后一次错误。"""
    global _ACTIVE_VISION_MODEL
    if _ACTIVE_VISION_MODEL:
        return _ACTIVE_VISION_MODEL
    if not zhipu_key():
        raise RuntimeError("未配置 ZHIPUAI_API_KEY")
    with _PICK_LOCK:
        if _ACTIVE_VISION_MODEL:
            return _ACTIVE_VISION_MODEL
        last = None
        for name in VISION_MODEL_CHAIN:
            try:
                _probe_model(name)
                _ACTIVE_VISION_MODEL = name
                return name
            except Exception as e:
                last = e
                continue
        if last is not None:
            raise last
        raise RuntimeError("无可用视觉模型")


def zhipu_chat(messages, model=None, timeout=60, max_tokens=None):
    if model is None:
        model = pick_vision_model()
    key = zhipu_key()
    if not key:
        raise RuntimeError("未配置 ZHIPUAI_API_KEY")
    payload = {"model": model, "messages": messages}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    req = urllib.request.Request(
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"] or ""


def ocr_b64(b64, mime, model=None, timeout=60):
    """把 base64 图片发到智谱视觉模型，返回识别出的纯文本。"""
    return zhipu_chat([{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}},
                {"type": "text",
                 "text": "请完整识别这张发票/票据上的全部文字，按原文输出纯文本。务必保留「发票号码："
                         "xxx」「开票日期：xxxx年x月x日」「价税合计（小写）¥xx.xx」「销售方名称」「购买方名称」"
                         "「项目名称」下的明细等关键内容。不要解释、不要总结。"},
            ],
        }], model, timeout)


def pick_folder(initial=""):
    """调用系统目录选择器；取消时返回空字符串。"""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        start = initial if initial and os.path.isdir(initial) else os.path.expanduser("~/Desktop")
        return filedialog.askdirectory(initialdir=start, mustexist=True) or ""
    finally:
        root.destroy()


def ocr_image_file(path, model=None):
    ext = os.path.splitext(path)[1].lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "bmp": "image/bmp", "webp": "image/webp"}.get(ext.lstrip("."), "image/jpeg")
    with open(path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    return ocr_b64(b64, mime, model)


# ---------- 本地 OCR（RapidOCR，可选依赖）----------
_RAPID = None
_RAPID_FAILED = False
_OCR_LOCK = threading.Lock()  # 引擎调用非线程安全；并发实测无吞吐收益，串行化即可


def rapid_engine():
    """懒加载本地识别引擎；未安装或初始化失败返回 None。"""
    global _RAPID, _RAPID_FAILED
    if RapidOCR is None or _RAPID_FAILED:
        return None
    if _RAPID is None:
        try:
            _RAPID = RapidOCR()
        except Exception:
            _RAPID_FAILED = True
            return None
    return _RAPID


def local_ocr_ready():
    return RapidOCR is not None and not _RAPID_FAILED


# 本机识别组件自动安装：首次启动或升级后若未安装，后台静默装上，用户无感知
OCR_INSTALL = {"status": "idle"}  # idle / installing / ready / failed
_OCR_INSTALLStarted = False


def _boot_mtime():
    """记录启动时 app.py 的修改时间，用于判断「代码已更新但服务未重启」。"""
    try:
        return os.path.getmtime(os.path.abspath(__file__))
    except OSError:
        return 0.0


BOOT_MTIME = _boot_mtime()


def code_stale():
    """app.py 在进程启动之后被改过 → 页面提示用户重启，避免白跑旧代码。"""
    try:
        return abs(os.path.getmtime(os.path.abspath(__file__)) - BOOT_MTIME) > 0.5
    except OSError:
        return False


def _pip_install_ocr():
    import subprocess
    pkg = "rapidocr-onnxruntime>=1.4,<2"
    attempts = [
        [sys.executable, "-m", "pip", "install", pkg,
         "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"],
        [sys.executable, "-m", "pip", "install", pkg],
    ]
    for cmd in attempts:
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=900)
            if r.returncode == 0:
                break
        except Exception:
            continue
    global _RAPID, _RAPID_FAILED
    _RAPID = None       # 重新懒加载，让新装的组件立即生效
    _RAPID_FAILED = False
    OCR_INSTALL["status"] = "ready" if local_ocr_ready() else "failed"


def ensure_local_ocr_async():
    """启动时调用：未安装则后台静默安装，绝不阻断启动与使用。"""
    global _OCR_INSTALLStarted
    if _OCR_INSTALLStarted:
        return
    _OCR_INSTALLStarted = True
    if local_ocr_ready():
        OCR_INSTALL["status"] = "ready"
        return
    def _worker():
        OCR_INSTALL["status"] = "installing"
        _pip_install_ocr()
    threading.Thread(target=_worker, daemon=True).start()


def local_ocr_image(pil_img):
    """对 PIL 图片做本地识别，返回按阅读顺序排列的文本。失败抛异常由调用方兜底。"""
    import numpy as np
    eng = rapid_engine()
    if eng is None:
        raise RuntimeError("本地识别组件未安装")
    img = pil_img.convert("RGB")
    # 长边降采样：显著提速并降低超大图失败率
    long_side = 1600
    w, h = img.size
    if max(w, h) > long_side:
        ratio = long_side / float(max(w, h))
        img = img.resize((int(w * ratio), int(h * ratio)))
    with _OCR_LOCK:
        result, _ = eng(np.array(img))
    if not result:
        return ""
    ordered = sorted(result, key=lambda x: (min(p[1] for p in x[0]), min(p[0] for p in x[0])))
    return "\n".join(str(t).strip() for _box, t, _score in ordered if t)


def local_ocr_file(path):
    """图片发票的本地识别。"""
    if Image is None:
        raise RuntimeError("缺少 Pillow 组件")
    with Image.open(path) as im:
        return local_ocr_image(im)


def local_ocr_pdf(path, scale=2.2, max_pages=4):
    """无文本层 PDF：渲染成图后本地识别。"""
    if pdfium is None or Image is None:
        raise RuntimeError("缺少 PDF 渲染组件")
    texts = []
    doc = pdfium.PdfDocument(path)
    try:
        for i in range(min(len(doc), max_pages)):
            texts.append(local_ocr_image(doc[i].render(scale=scale).to_pil()))
    finally:
        doc.close()
    return "\n".join(texts)


def render_pdf_pages(path, scale=2.2, max_pages=4):
    """无文本层 PDF -> 渲染成 PNG -> base64 列表（pypdfium2）。"""
    if pdfium is None:
        return []
    doc = pdfium.PdfDocument(path)
    out = []
    try:
        for i in range(min(len(doc), max_pages)):
            page = doc[i]
            bmp = page.render(scale=scale)
            img = bmp.to_pil()
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            out.append(base64.b64encode(buf.getvalue()).decode())
    finally:
        doc.close()
    return out


def parse_file(path, ocr_enabled=True, ocr_model=None, company_names=None):
    """解析单张发票 -> (fields, warn)。warn 非空表示有降级/提示。
    ocr_model=None 时由服务端自动挑选最新可用免费视觉模型。"""
    ext = os.path.splitext(path)[1].lower()
    warn = None
    if is_itinerary_name(path):
        # 行程单：不读文件、不 OCR、不生成发票字段，零成本跳过
        fields = _itinerary_fields()
        fields["class_text"] = ""
        fields["ocr"] = False
        fields["ocr_engine"] = None
        return fields, None
    text = ""
    pdf_text = ""
    did_ocr = False
    engine_used = None  # local=本机识别 / ai=智谱；用于前端如实标注与缓存自愈
    if ext == ".ofd":
        # 数电票官方格式：直接读 XML 票面文本，最准且不联网、不需要第三方库
        text = read_ofd_text(path)
        if not re.sub(r"\s", "", text or ""):
            warn = "OFD 未解析到票面文本（可能是扫描件版式）"
            if ocr_enabled:
                text = ""  # 交由下方统一兜底逻辑处理
    elif ext == ".pdf":
        if PdfReader is not None:
            try:
                text = "".join((p.extract_text() or "") for p in PdfReader(path).pages)
                pdf_text = text
            except Exception as e:
                warn = "PDF解析异常:%s" % e
        # 文本过少或字符编码损坏时尝试渲染 OCR。
        needs_ocr = len(re.sub(r"\s", "", text or "")) < 40 or text.count("\x00") >= 8
        if needs_ocr and ocr_enabled:
            # 优先本地识别（免 Key、数据不出本机），不可用时再走 AI
            if local_ocr_ready():
                try:
                    local_text = local_ocr_pdf(path)
                    if local_text.strip():
                        text, warn, did_ocr = local_text, None, True
                        engine_used = "local"
                except Exception as e:
                    warn = "本地识别失败：%s" % e
            if not did_ocr and zhipu_key():
                try:
                    pages = render_pdf_pages(path)
                    chunks = []
                    for b64 in pages:
                        try:
                            chunks.append(ocr_b64(b64, "image/png", ocr_model))
                        except Exception as e:
                            warn = "PDF页OCR失败：%s" % friendly_ocr_error(e)
                    if chunks:
                        text = "\n".join(chunks)
                        warn = None
                        did_ocr = True
                        engine_used = "ai"
                except Exception as e:
                    warn = "PDF渲染失败:%s" % e
        if not warn and needs_ocr and not did_ocr:
            warn = "PDF无可用文本层且未完成OCR(需配置ZHIPUAI_API_KEY)"
    else:  # 图片
        if ocr_enabled:
            if local_ocr_ready():
                try:
                    local_text = local_ocr_file(path)
                    if local_text.strip():
                        text, warn, did_ocr = local_text, None, True
                        engine_used = "local"
                except Exception as e:
                    warn = "本地识别失败：%s" % e
            if not did_ocr and zhipu_key():
                try:
                    text = ocr_image_file(path, ocr_model)
                    did_ocr = True
                    engine_used = "ai"
                    warn = None if text.strip() else "OCR返回为空"
                except Exception as e:
                    warn = "OCR失败：%s" % friendly_ocr_error(e)
            if not did_ocr and not warn:
                warn = "图片发票未启用OCR(需配置ZHIPUAI_API_KEY)"
    fields = extract_fields(text or "", os.path.basename(path), company_names)
    if did_ocr and pdf_text:
        fallback = extract_fields(pdf_text, os.path.basename(path), company_names)
        # 数字字段优先采用本地文本层；OCR 主要补齐损坏的中文购销方。
        for key in ("no", "code", "date", "amount_cents"):
            if fallback.get(key) is not None:
                fields[key] = fallback[key]
        for key in ("seller", "buyer"):
            if fields.get(key) is None and fallback.get(key) is not None:
                fields[key] = fallback[key]
    # 文本层字符数够但购销方缺失（公司名不在文本层中），补一次 OCR 兜底
    if not did_ocr and ocr_enabled and (not fields.get("seller") or not fields.get("buyer")):
        ocr_text = ""
        ocr_engine = None
        if ext == ".pdf" and local_ocr_ready():
            try:
                ocr_text = local_ocr_pdf(path)
                if ocr_text.strip():
                    ocr_engine = "local"
            except Exception:
                pass
        if not ocr_text.strip() and ext == ".pdf" and zhipu_key():
            try:
                chunks = []
                for b64 in render_pdf_pages(path):
                    try:
                        chunks.append(ocr_b64(b64, "image/png", ocr_model))
                    except Exception:
                        pass
                ocr_text = "\n".join(chunks)
                if ocr_text.strip():
                    ocr_engine = "ai"
            except Exception:
                pass
        if not ocr_text.strip() and ext != ".pdf" and local_ocr_ready():
            try:
                ocr_text = local_ocr_file(path)
                if ocr_text.strip():
                    ocr_engine = "local"
            except Exception:
                pass
        if not ocr_text.strip() and ext != ".pdf" and zhipu_key():
            try:
                ocr_text = ocr_image_file(path, ocr_model)
                if ocr_text.strip():
                    ocr_engine = "ai"
            except Exception:
                pass
        if ocr_text.strip():
            ocr_fields = extract_fields(ocr_text, os.path.basename(path), company_names)
            for key in ("seller", "buyer"):
                if not fields.get(key) and ocr_fields.get(key):
                    fields[key] = ocr_fields[key]
            did_ocr = True
            engine_used = engine_used or ocr_engine
    if fields.get("itinerary"):
        # 行程单（票面版式识别出来的）：只留文本用于展示，不报缺字段
        fields["class_text"] = (text or "")[:6000]
        fields["ocr"] = did_ocr
        fields["ocr_engine"] = engine_used
        return fields, None
    missing = [name for key, name in (("no", "号码"), ("amount_cents", "金额"),
                                      ("date", "日期"), ("seller", "销售方"))
               if fields.get(key) is None]
    if missing:
        detail = "需复核：%s未识别" % "、".join(missing)
        warn = "%s；%s" % (warn, detail) if warn else detail
    fields["class_text"] = (text or "")[:6000]
    fields["ocr"] = did_ocr
    fields["ocr_engine"] = engine_used
    return fields, warn



def load_config():
    if os.path.exists(CONFIG):
        try:
            return json.load(open(CONFIG, encoding="utf-8"))
        except Exception:
            pass
    return {"watch_dirs": [], "used_dirs": [], "archive_dir": "",
            "company_names": BUYER_DEFAULT}


def save_config(cfg):
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, CONFIG)


_ERR_LOCK = threading.Lock()


def log_error(where, exc):
    """异常 traceback 追加到 finance_error.log——双击/分离启动时 stderr 不可见。"""
    try:
        with _ERR_LOCK:
            with open(ERROR_LOG, "a", encoding="utf-8") as fh:
                fh.write("\n[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), where))
                traceback.print_exception(type(exc), exc, exc.__traceback__, file=fh)
    except OSError:
        pass


_FAULT_LOG_FH = None


def enable_crash_log():
    """faulthandler：C 库原生崩溃（段错误等）时把全部线程栈写入 finance_error.log。"""
    global _FAULT_LOG_FH
    try:
        _FAULT_LOG_FH = open(ERROR_LOG, "a", encoding="utf-8")
        faulthandler.enable(file=_FAULT_LOG_FH)
    except OSError:
        _FAULT_LOG_FH = None


def is_within(path, folder):
    try:
        return os.path.commonpath((os.path.abspath(path), os.path.abspath(folder))) == os.path.abspath(folder)
    except (ValueError, OSError):
        return False


def records_in_dirs(led, dirs):
    return [r for r in led.get("records", {}).values()
            if any(is_within(r["path"], d) for d in dirs)]


def collect_records():
    """当前配置目录下的全部台账记录，并按配置标注 is_used。"""
    cfg = load_config()
    led = load_ledger()
    watch_dirs = list(cfg.get("watch_dirs", []))
    used_set = {os.path.normpath(d) for d in cfg.get("used_dirs", []) if d and os.path.isdir(d)}
    dirs = watch_dirs + list(cfg.get("used_dirs", []))
    recs = records_in_dirs(led, dirs)
    for r in recs:
        folder_used = any(is_within(r["path"], d) for d in used_set)
        r["in_watch"] = any(is_within(r["path"], d) for d in watch_dirs)
        r["is_used"] = bool(r.get("used_override")) if r.get("used_override_set") else folder_used
    return recs



def iter_invoice_files(dirs):
    seen = set()
    for d in dirs or []:
        if not d or not os.path.isdir(d):
            continue
        for root, _subs, files in os.walk(d):
            if any(seg.startswith(".") for seg in root.split(os.sep)):
                continue
            for fn in files:
                if os.path.splitext(fn)[1].lower() in INVOICE_EXTS:
                    p = os.path.normpath(os.path.join(root, fn))
                    if p not in seen:
                        seen.add(p)
                        yield p


def build_scan(watch_dirs, used_dirs, ocr_enabled, ocr_model, progress=None):
    """扫描并增量更新台账（只读文件内容，绝不删除/移动）。"""
    rapid_engine()  # 先在主线程完成引擎初始化，避免并发 workers 首次同时构造
    led = load_ledger()
    if led.get("version") != ENGINE_VER:
        led = {"version": ENGINE_VER, "records": {}}
    all_dirs = list(dict.fromkeys(list(watch_dirs or []) + list(used_dirs or [])))
    used_set = {os.path.normpath(d) for d in (used_dirs or []) if d and os.path.isdir(d)}
    if os.path.exists(CONFIG):
        led_cfg = json.load(open(CONFIG, encoding="utf-8"))
    else:
        led_cfg = {}
    buyers = led_cfg.get("company_names") or BUYER_DEFAULT
    categories = get_categories(led_cfg)
    parse_sig = hashlib.sha256(json.dumps({
        "engine": ENGINE_VER, "ocr": bool(ocr_enabled), "ocr_model": ocr_model,
        "ocr_key": bool(zhipu_key()), "buyers": buyers, "extract": EXTRACT_REV,
    }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    cat_sig = hashlib.sha256(json.dumps(categories, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]

    # 1) 增量解析（AI OCR 走网络，线程池并发提速；结果按序写回台账）
    todo = []
    for p in iter_invoice_files(all_dirs):
        st = os.stat(p)
        rec = led["records"].get(p)
        unchanged = rec and rec.get("mtime") == st.st_mtime and rec.get("size") == st.st_size
        if unchanged and rec.get("parse_sig") == parse_sig:
            rec["file_missing"] = False
            # 一次性故障自愈：上次走了 AI 兜底仍缺关键字段（如销售方），而本机识别现已可用
            # → 本轮强制重解析一次（本机识别免费、离线）。重解析后 engine 变为 local，
            #   即使仍缺字段也不会再进此分支，避免每轮空转。
            # 扩展：文本层字符够但公司名缺失导致销售方未识别，也补一次 OCR 兜底；
            # 为防止本地 OCR 也失败时无限重解析，仅在 cls_text 中找不到公司名候选时触发
            # （说明之前的解析从未成功从图像中提取过公司名）。
            _warn_txt = rec.get("warn") or ""
            seller_missing = not rec.get("seller")
            cls_has_company = bool(COMPANY_RE.search(rec.get("cls_text", "") or ""))
            needs_ocr_fallback = ("需复核" in _warn_txt and local_ocr_ready()
                    and (rec.get("ocr_engine") == "ai"
                         or (rec.get("ocr") and rec.get("ocr_engine") is None)
                         or (seller_missing and not cls_has_company)))
            if needs_ocr_fallback:
                todo.append((p, st))
                continue
            if (rec.get("ocr") and rec.get("warn") == "PDF无可用文本层且未完成OCR(需配置ZHIPUAI_API_KEY)"
                    and all(rec.get(k) is not None for k in ("no", "amount_cents", "date", "seller"))):
                rec["warn"] = None
            if rec.get("itinerary"):
                rec["cat_id"], rec["cat_rule"], rec["cat_label"] = None, "行程单", "行程单"
            elif rec.get("cat_rule") == "人工":
                rec["cat_label"] = next((c["label"] for c in categories
                                          if c["id"] == rec.get("cat_id")), "其他/待分类")
            elif rec.get("cat_sig") != cat_sig:
                cid, rule = classify_text(rec.get("cls_text", ""), categories)
                rec.update(cat_id=cid, cat_rule=rule,
                           cat_label=next((c["label"] for c in categories if c["id"] == cid), cid),
                           cat_sig=cat_sig)
            continue
        todo.append((p, st))
    # 1) 内容继承：文件被移动/重命名（如按月归档）后路径变了会整体重新识别。
    #    旧记录对应的文件已不在（含尚未打 file_missing 标记的）若与新文件内容
    #    一致（md5 相同），直接继承旧识别结果，整月归档从半小时变成秒级。
    #    签名不一致的旧结果仅当关键字段齐全且无警告时才继承，避免把过期的
    #    解析产物带进来。
    adopt_src = {}
    for r in led["records"].values():
        if r.get("md5") and not r.get("itinerary") and not os.path.exists(r.get("path") or ""):
            cur = adopt_src.get(r["md5"])
            if cur is None:
                adopt_src[r["md5"]] = r
            elif (r.get("parse_sig") == parse_sig) and (cur.get("parse_sig") != parse_sig):
                adopt_src[r["md5"]] = r
            elif (r.get("parse_sig") == parse_sig) == (cur.get("parse_sig") == parse_sig) \
                    and (r.get("updated") or 0) > (cur.get("updated") or 0):
                adopt_src[r["md5"]] = r
    if adopt_src and todo:
        still = []
        for p, st in todo:
            try:
                src = adopt_src.get(file_md5(p))
            except OSError:
                still.append((p, st))
                continue
            if src is None:
                still.append((p, st))
                continue
            if src.get("parse_sig") != parse_sig and (
                    any(src.get(k) is None for k in ("no", "amount_cents", "date", "seller"))
                    or src.get("warn")):
                still.append((p, st))
                continue
            rec = copy.deepcopy(src)
            rec.update(path=p, folder=os.path.basename(os.path.dirname(p)),
                       fname=os.path.basename(p), size=st.st_size, mtime=st.st_mtime,
                       file_missing=False, parse_sig=parse_sig, updated=time.time())
            rec["buyer_status"], _buyer = check_buyer(rec, buyers)
            rec["fp"] = fingerprint_for(rec, rec["md5"])
            if rec.get("itinerary"):
                rec["cat_id"], rec["cat_rule"], rec["cat_label"] = None, "行程单", "行程单"
            elif rec.get("cat_rule") == "人工":
                rec["cat_label"] = next((c["label"] for c in categories
                                          if c["id"] == rec.get("cat_id")), "其他/待分类")
            elif rec.get("cat_sig") != cat_sig:
                cid, rule = classify_text(rec.get("cls_text", ""), categories)
                rec.update(cat_id=cid, cat_rule=rule,
                           cat_label=next((c["label"] for c in categories if c["id"] == cid), cid),
                           cat_sig=cat_sig)
            led["records"][p] = rec
        todo = still
    if todo:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _parse_one(path):
            try:
                return parse_file(path, ocr_enabled, ocr_model, buyers)
            except Exception as e:  # 文件被占用/删除等，不让单张失败拖垮整轮扫描
                return {"ocr": False}, "解析失败:%s" % e

        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(_parse_one, p): i for i, (p, _st) in enumerate(todo)}
            parsed = [None] * len(todo)
            for n, fut in enumerate(as_completed(futs), 1):
                parsed[futs[fut]] = fut.result()
                if progress:
                    progress(n, len(todo))
        for (p, st), (fields, warn) in zip(todo, parsed):
            old = led["records"].get(p)
            md5hex = file_md5(p)
            issues = validate_fields(fields)
            buyer_status, _buyer = check_buyer(fields, buyers)
            fp = fingerprint_for(fields, md5hex)
            cls_text = os.path.basename(p) + "|" + (fields.get("seller") or "") + "|" + \
                " ".join(fields.get("items") or []) + "|" + \
                " ".join(fields.get("svc") or []) + "|" + (fields.get("kind") or "") + "|" + \
                fields.get("class_text", "")
            rec = {
                "path": p, "folder": os.path.basename(os.path.dirname(p)),
                "fname": os.path.basename(p), "size": st.st_size, "mtime": st.st_mtime,
                "md5": md5hex,
                "no": fields.get("no"), "code": fields.get("code"),
                "date": fields.get("date"), "amount_cents": fields.get("amount_cents"),
                "seller": fields.get("seller"), "buyer": fields.get("buyer"),
                "kind": fields.get("kind"), "items": fields.get("items"),
                "svc": fields.get("svc"), "itinerary": bool(fields.get("itinerary")),
                "ocr": fields.get("ocr", False), "ocr_engine": fields.get("ocr_engine"),
                "warn": warn,
                "fp": fp, "cls_text": cls_text[:6000], "parse_sig": parse_sig, "cat_sig": cat_sig,
                "cat_id": old.get("cat_id") if old and old.get("cat_rule") == "人工" else None,
                "cat_label": old.get("cat_label") if old and old.get("cat_rule") == "人工" else None,
                "cat_rule": old.get("cat_rule") if old and old.get("cat_rule") == "人工" else None,
                "used_override": old.get("used_override", False) if old else False,
                "used_override_set": old.get("used_override_set", False) if old else False,
                "check_issues": issues, "buyer_status": buyer_status,
                "verify_status": old.get("verify_status", "未查验") if old else "未查验",
                "reject_reason": old.get("reject_reason", "") if old else "",
                "closed": old.get("closed", False) if old else False,
                "file_missing": False,
                "first_seen": old.get("first_seen", time.time()) if old else time.time(),
                "updated": time.time(),
            }
            if fields.get("itinerary"):
                rec["cat_id"], rec["cat_rule"], rec["cat_label"] = None, "行程单", "行程单"
            else:
                cid, rule = classify_text(rec["cls_text"], categories)
                if rec["cat_id"] is None:
                    rec["cat_id"], rec["cat_rule"] = cid, rule
                    rec["cat_label"] = next((c["label"] for c in categories if c["id"] == cid), cid)
            rec["ai_issues"] = check_anomaly(rec)
            led["records"][p] = rec

    # 2) 文件消失时的处理：仅已报销/已结案的记录永久保留（财务留痕），其余清理。
    #    注意不能用 verify_status 判断——所有记录默认就是「未查验」，用它兜底
    #    会导致清空的文件夹永远留下幽灵记录，与移动后的新文件互相误判重复。
    cfg_prefixes = tuple(os.path.normpath(d) for d in all_dirs if d)
    if cfg_prefixes:
        for p in list(led["records"]):
            if any(is_within(p, d) for d in cfg_prefixes) and not os.path.exists(p):
                r = led["records"][p]
                if r.get("used_override") or r.get("closed"):
                    r["file_missing"] = True
                else:
                    del led["records"][p]

    # 3) 内容查重（同源不判重：同一物理文件只有一条记录）
    recs = records_in_dirs(led, all_dirs)
    for r in recs:
        folder_used = any(is_within(r["path"], d) for d in used_set)
        r["in_watch"] = any(is_within(r["path"], d) for d in (watch_dirs or []))
        r["in_used_dir"] = folder_used
        r["is_used"] = bool(r.get("used_override")) if r.get("used_override_set") else folder_used
    idx = {}
    for r in recs:
        for w, k in r["fp"]:
            idx.setdefault(k, []).append(r)
    # 已报销永久库注入：原文件即使被移走/删除，仍能持续查重
    for it in load_used_ledger()["items"]:
        ghost = {"path": "used://" + (used_item_key(it) or "unknown"),
                 "origin_path": it.get("path") or "",
                 "folder": "已报销库", "fname": it.get("fname") or "（历史台账记录）",
                 "is_used": True, "ghost": True, "no": it.get("no"),
                 "amount_cents": it.get("amount_cents"), "batch": it.get("batch")}
        for w, k in fingerprint(it, None):
            idx.setdefault(k, []).append(ghost)
    for r in recs:
        r["dups"] = []
        seenp = set()
        for w, k in r["fp"]:
            basis = ("文件内容完全一致" if k.startswith("md5|") else
                     "发票代码+号码一致" if k.startswith("code+no|") else
                     "发票号+金额一致" if k.startswith("no+amt|") else
                     "发票号码一致" if k.startswith("no|") else
                     "销方+金额+日期一致" if k.startswith("sel+amt+date|") else w)
            for o in idx.get(k, []):
                if o["path"] != r["path"] and o["path"] not in seenp:
                    # 永久库里就是它自己时不判重，避免结案后自匹配
                    if o.get("origin_path") and o["origin_path"] == r["path"]:
                        continue
                    seenp.add(o["path"])
                    r["dups"].append({"level": "高危" if w != "low" else "疑似", "basis": basis,
                                      "fname": o["fname"], "folder": o["folder"],
                                      "path": o["path"], "no": o.get("no"),
                                      "amount": o.get("amount_cents"), "is_used": o["is_used"],
                                      "ghost": bool(o.get("ghost")), "batch": o.get("batch", "")})
    # 4) 行程单配对：行程单不是发票，按文件名核心与同名发票互相挂链展示
    pair_itineraries(recs)
    save_ledger(led)
    return led, used_set, recs



# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        ln = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(ln).decode("utf-8")) if ln else {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            if os.path.exists(STATIC):
                html = open(STATIC, encoding="utf-8").read().replace("{{APP_VERSION}}", APP_VERSION)
                return self._send(200, html,
                                  "text/html; charset=utf-8")
            return self._send(404, "缺少 index.html")
        if u.path == "/cashflow-workbench.html":
            cf = os.path.join(APP_DIR, "cashflow-workbench.html")
            if os.path.exists(cf):
                return self._send(200, open(cf, encoding="utf-8").read(),
                                  "text/html; charset=utf-8")
            return self._send(404, "缺少 cashflow-workbench.html")
        if u.path == "/api/config":
            cfg = load_config()
            cfg["ocr_key_configured"] = bool(zhipu_key())
            cfg["key_masked"] = mask_key(zhipu_key())
            cfg["app_version"] = APP_VERSION
            cfg["code_stale"] = code_stale()
            cfg["local_ocr_ready"] = local_ocr_ready()
            cfg["local_ocr_status"] = OCR_INSTALL.get("status", "idle")
            return self._send(200, json.dumps(cfg))
        if u.path == "/api/subdirs":
            # 列出所选目录的直接子目录，供「添加目录」多选勾选
            q = parse_qs(u.query)
            base = (q.get("path") or [""])[0]
            found = []
            if base and os.path.isdir(base):
                try:
                    for name in sorted(os.listdir(base)):
                        full = os.path.normpath(os.path.join(base, name))
                        if os.path.isdir(full):
                            found.append(full)
                except OSError:
                    pass
            return self._send(200, json.dumps({"base": base, "dirs": found[:200]}, ensure_ascii=False))
        if u.path == "/api/categories":
            return self._send(200, json.dumps(get_categories(), ensure_ascii=False))
        if u.path == "/api/dashboard":
            led = load_ledger()
            recs = led.get("records", {})
            record_count = len(recs)
            used = load_used_ledger()
            used_count = len(used.get("items", []))
            pending_review = sum(1 for r in recs.values()
                                 if not r.get("verify_status") or r.get("verify_status") == "未查验")
            last_scan = None
            if os.path.exists(LEDGER):
                try:
                    last_scan = time.strftime("%Y-%m-%dT%H:%M:%S",
                                              time.localtime(os.path.getmtime(LEDGER)))
                except OSError:
                    pass
            recent = sorted(recs.values(), key=lambda r: (r.get("mtime") or 0), reverse=True)[:5]
            recent_items = []
            for r in recent:
                recent_items.append({
                    "product": "发票管家",
                    "fname": r.get("fname") or "",
                    "date": r.get("date") or "",
                    "amount_cents": r.get("amount_cents"),
                    "seller": r.get("seller") or "",
                    "cat_label": r.get("cat_label") or "",
                    "mtime": r.get("mtime")
                })
            return self._send(200, json.dumps({
                "record_count": record_count,
                "used_count": used_count,
                "pending_review": pending_review,
                "last_scan": last_scan,
                "recent": recent_items
            }, ensure_ascii=False))
        if u.path == "/api/folders":
            desk = os.path.join(os.path.expanduser("~"), "Desktop")
            found = []
            if os.path.isdir(desk):
                for d in sorted(os.listdir(desk)):
                    full = os.path.join(desk, d)
                    if os.path.isdir(full) and (re.match(r"^20\d{4}", d) or "发票" in d
                                                or "报销" in d or "归档" in d):
                        found.append(full)
            return self._send(200, json.dumps({"desktop": desk, "folders": found}))
        if u.path == "/api/file":
            # 发票原文件预览：仅允许已配置目录内的发票文件，防止任意路径读取
            q = parse_qs(u.query)
            path = (q.get("path") or [""])[0]
            cfg = load_config()
            dirs = [d for d in list(cfg.get("watch_dirs", [])) + list(cfg.get("used_dirs", [])) if d]
            ext = os.path.splitext(path)[1].lower()
            if not path or ext not in INVOICE_EXTS or not os.path.isfile(path) \
                    or not any(is_within(path, d) for d in dirs):
                return self._send(404, json.dumps(
                    {"error": "发票文件不存在或不在已配置目录中"}, ensure_ascii=False))
            ctype = "application/pdf" if ext == ".pdf" else {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".bmp": "image/bmp", ".webp": "image/webp"}.get(ext, "application/octet-stream")
            with open(path, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if u.path == "/api/export.csv":
            q = parse_qs(u.query)
            scope = (q.get("scope") or ["all"])[0]
            return self._send(200, export_csv(collect_records(), scope), "text/csv; charset=utf-8")
        if u.path == "/api/export.xlsx":
            from urllib.parse import quote
            q = parse_qs(u.query)
            scope = (q.get("scope") or ["all"])[0]
            recs = [r for r in collect_records() if record_in_scope(r, scope)]
            try:
                data = export_xlsx(recs)
            except ImportError:
                return self._send(500, json.dumps(
                    {"error": "缺少 openpyxl 组件，请先执行: pip install openpyxl"}, ensure_ascii=False))
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header(
                "Content-Disposition",
                'attachment; filename="invoice_summary.xlsx"; filename*=UTF-8\'\''
                + quote("发票分类汇总.xlsx"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if u.path == "/api/ledger-status":
            return self._send(200, json.dumps({
                "error": LEDGER_STATUS.get("error"),
                "record_count": LEDGER_STATUS.get("record_count", 0),
                "used_count": len(load_used_ledger().get("items", [])),
                "backups": list_backups(),
            }, ensure_ascii=False))
        if u.path == "/api/used-ledger":
            items = load_used_ledger().get("items", [])
            return self._send(200, json.dumps(
                {"total": len(items), "items": items[-500:]}, ensure_ascii=False))
        if u.path == "/api/records-list":
            with STATE_LOCK:
                led = load_ledger()
                cfg = load_config()
                used_dirs = [os.path.normpath(d) for d in (cfg.get("used_dirs") or []) if d]
                items = []
                for p, r in led["records"].items():
                    in_used = any(is_within(p, d) for d in used_dirs)
                    items.append({
                        "path": p, "fname": r.get("fname"), "folder": r.get("folder"),
                        "date": r.get("date"), "seller": r.get("seller"),
                        "amount_cents": r.get("amount_cents"), "no": r.get("no"),
                        "kind": r.get("kind"), "cat_label": r.get("cat_label"),
                        "closed": bool(r.get("closed")),
                        "used": bool(r.get("used_override")) if r.get("used_override_set") else in_used,
                        "file_missing": bool(r.get("file_missing")),
                        "verify_status": r.get("verify_status"), "warn": r.get("warn"),
                    })
            items.sort(key=lambda x: (x["file_missing"], x.get("date") or "", x.get("fname") or ""))
            return self._send(200, json.dumps({"ok": True, "records": items}, ensure_ascii=False))
        if u.path == "/api/export.worksheet":
            recs = [r for r in collect_records()]
            try:
                data = export_worksheet(recs)
            except ImportError:
                return self._send(500, json.dumps(
                    {"error": "缺少 openpyxl 组件，请先执行: pip install openpyxl"}, ensure_ascii=False))
            from urllib.parse import quote
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header(
                "Content-Disposition",
                'attachment; filename="invoice_worksheet.xlsx"; filename*=UTF-8\'\''
                + quote("发票审核底稿.xlsx"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if u.path == "/api/scan-status":
            return self._send(200, json.dumps(scan_status(), ensure_ascii=False))
        self._send(404, "{}")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/categories":
            try:
                cats = validate_categories(self._read_json().get("categories"))
                with STATE_LOCK:
                    cfg = load_config()
                    cfg["categories"] = cats
                    save_config(cfg)
                return self._send(200, json.dumps({"ok": True, "categories": cats}, ensure_ascii=False))
            except (ValueError, TypeError) as e:
                return self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False))
        if u.path == "/api/scan":
            try:
                return self._send(200, json.dumps(
                    scan_start(self._read_json()), ensure_ascii=False))
            except Exception as e:
                log_error("/api/scan", e)
                return self._send(500, json.dumps(
                    {"error": "无法启动扫描，原因已记录到 finance_error.log，请重启程序后重试"},
                    ensure_ascii=False))
        if u.path == "/api/pick-folder":
            try:
                path = pick_folder(self._read_json().get("initial") or "")
                return self._send(200, json.dumps({"path": path}, ensure_ascii=False))
            except Exception:
                return self._send(500, json.dumps(
                    {"error": "无法打开 Windows 文件夹选择器，请直接粘贴目录路径"}, ensure_ascii=False))
        if u.path == "/api/ocr-status":
            if not zhipu_key():
                return self._send(200, json.dumps({"ok": False, "configured": False,
                    "message": "尚未配置智谱 API Key"}, ensure_ascii=False))
            try:
                # 服务端自动选最新免费模型；忽略 client-supplied 的 model
                pick_vision_model()
                return self._send(200, json.dumps({"ok": True, "configured": True,
                    "message": "连接正常"}, ensure_ascii=False))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "configured": True,
                    "message": friendly_ocr_error(e)}, ensure_ascii=False))
        if u.path == "/api/save-key":
            body = self._read_json()
            if body.get("clear"):
                clear_zhipu_key()
                return self._send(200, json.dumps(
                    {"ok": True, "configured": False, "masked": "",
                     "message": "已清除本机保存的 API Key"}, ensure_ascii=False))
            key = (body.get("api_key") or "").strip().strip('"').strip("'")
            if len(key) < 10:
                return self._send(400, json.dumps(
                    {"error": "这串内容不像完整的 API Key（应是一长串字母数字），请回网页重新「复制」再粘贴"},
                    ensure_ascii=False))
            save_zhipu_key(key)
            try:
                # 保存 Key 后立刻用自动选出的最新免费模型连通一次，
                # 验证 Key 有效且至少有一个视觉模型可用。
                pick_vision_model()
                return self._send(200, json.dumps(
                    {"ok": True, "configured": True, "masked": mask_key(key),
                     "message": "连接正常，AI 识别已就绪"}, ensure_ascii=False))
            except Exception as e:
                return self._send(200, json.dumps(
                    {"ok": False, "configured": True, "masked": mask_key(key),
                     "message": friendly_ocr_error(e)}, ensure_ascii=False))
        if u.path == "/api/override":
            body = self._read_json()
            path = body.get("path")
            with STATE_LOCK:
                led = load_ledger()
                if path not in led["records"]:
                    return self._send(404, json.dumps({"error": "发票记录不存在"}, ensure_ascii=False))
                r = led["records"][path]
                if "cat_id" in body:
                    catalog = get_categories()
                    if body["cat_id"] not in {c["id"] for c in catalog}:
                        return self._send(400, json.dumps({"error": "分类不存在"}, ensure_ascii=False))
                    r["cat_id"] = body["cat_id"]
                    r["cat_label"] = next((c["label"] for c in catalog
                                           if c["id"] == body["cat_id"]), body["cat_id"])
                    r["cat_rule"] = "人工"
                if "used" in body:
                    r["used_override"] = bool(body["used"])
                    r["used_override_set"] = True
                if "verify_status" in body:
                    if body["verify_status"] not in VERIFY_CHOICES:
                        return self._send(400, json.dumps({"error": "查验状态无效"}, ensure_ascii=False))
                    r["verify_status"] = body["verify_status"]
                if "reject_reason" in body:
                    r["reject_reason"] = body["reject_reason"] if body["reject_reason"] in REJECT_CHOICES else ""
                save_ledger(led)
            return self._send(200, json.dumps({"ok": True}))
        if u.path == "/api/review":
            """批量设置查验状态 / 退回原因（财务核验）。"""
            body = self._read_json()
            paths = body.get("paths") or []
            with STATE_LOCK:
                led = load_ledger()
                n = 0
                for p in paths:
                    r = led["records"].get(p)
                    if not r:
                        continue
                    if body.get("verify_status") in VERIFY_CHOICES:
                        r["verify_status"] = body["verify_status"]
                    if "reject_reason" in body:
                        r["reject_reason"] = body["reject_reason"] if body["reject_reason"] in REJECT_CHOICES else ""
                    n += 1
                save_ledger(led)
            return self._send(200, json.dumps({"ok": True, "updated": n}, ensure_ascii=False))
        if u.path == "/api/records-delete":
            body = self._read_json()
            paths = body.get("paths") or []
            with STATE_LOCK:
                led = load_ledger()
                n = 0
                for p in paths:
                    if p in led["records"]:
                        del led["records"][p]
                        n += 1
                save_ledger(led)
            return self._send(200, json.dumps(
                {"ok": True, "deleted": n,
                 "message": "已删除 %d 条记录。删除前已自动备份，「台账恢复」里可以找回。" % n},
                ensure_ascii=False))
        if u.path == "/api/restore-ledger":
            try:
                n = restore_ledger(self._read_json().get("name") or "")
                return self._send(200, json.dumps(
                    {"ok": True, "records": n, "message": "已还原 %d 条台账记录，请重新查重。" % n},
                    ensure_ascii=False))
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False))
        if u.path == "/api/import-history":
            """N3 冷启动：导入财务已有的历史台账（CSV/Excel 另存为 CSV）。"""
            body = self._read_json()
            items = parse_history_csv(body.get("csv") or "")
            if not items:
                return self._send(400, json.dumps(
                    {"error": "未识别到有效记录，请确认文件含发票号码或「金额+开票日期」两列"},
                    ensure_ascii=False))
            with STATE_LOCK:
                added = used_ledger_add(items, body.get("batch") or "历史导入")
            return self._send(200, json.dumps(
                {"ok": True, "total": len(items), "added": added,
                 "message": "解析 %d 条，新增 %d 条进入已报销库（重复自动跳过）。" % (len(items), added)},
                ensure_ascii=False))
        if u.path == "/api/close-batch":
            """N1 结案：本批入库已报销 + 可选规范命名归档，一次走完 SOP 最后一环。"""
            import shutil as _shutil
            body = self._read_json()
            paths = body.get("paths") or []
            batch = (body.get("batch") or "").strip() or time.strftime("%Y-%m")
            do_archive = bool(body.get("archive"))
            with STATE_LOCK:
                led = load_ledger()
                picked = [led["records"][p] for p in paths if p in led["records"]]
                if not picked:
                    return self._send(400, json.dumps({"error": "请先勾选要结案的发票"}, ensure_ascii=False))
                for r in picked:
                    r["closed"] = True
                    r["closed_at"] = time.time()
                    r["used_override"] = True
                    r["used_override_set"] = True
                    r["is_used"] = True
                save_ledger(led)
                added = used_ledger_add(picked, batch)
                copied, skipped = [], []
                if do_archive:
                    target = (load_config().get("archive_dir") or "").strip()
                    if not target or not os.path.isdir(target):
                        skipped.append("归档目录未设置或不存在，已跳过归档")
                    else:
                        for r in picked:
                            src = r.get("path")
                            if not src or not os.path.isfile(src):
                                skipped.append(r.get("fname") or src)
                                continue
                            dst = os.path.join(target, archive_name(r))
                            base, ext = os.path.splitext(dst)
                            i = 1
                            while os.path.exists(dst):
                                dst = "%s(%d)%s" % (base, i, ext)
                                i += 1
                            _shutil.copy2(src, dst)
                            copied.append(os.path.basename(dst))
            # 无号码且无金额的票无法生成唯一键，必须明确告知，否则下月查不出来
            no_key = [r.get("fname") or r.get("path") for r in picked if not used_item_key(r)]
            return self._send(200, json.dumps(
                {"ok": True, "closed": len(picked), "added": added,
                 "no_key": no_key, "copied": copied, "skipped": skipped}, ensure_ascii=False))
        if u.path == "/api/extract":
            import shutil
            body = self._read_json()
            paths = body.get("paths") or []
            target = body.get("target_dir") or ""
            if not target or not os.path.isdir(target):
                return self._send(400, json.dumps({"error": "归档目录不存在或未填写"}))
            cfg = load_config()
            allowed = {r["path"] for r in records_in_dirs(
                load_ledger(), list(cfg.get("watch_dirs", [])) + list(cfg.get("used_dirs", [])))}
            copied, skipped = [], []
            for p in paths:
                if p in allowed and os.path.isfile(p):
                    dst = os.path.join(target, os.path.basename(p))
                    if os.path.exists(dst):
                        skipped.append(dst)
                    else:
                        shutil.copy2(p, dst)
                        copied.append(dst)
            return self._send(200, json.dumps({"copied": copied, "skipped": skipped}, ensure_ascii=False))
        self._send(404, "{}")


# ---------- 后台扫描任务：启动即返回 + 前端轮询，长扫描不再依赖单次 HTTP 请求活到最后 ----------
_SCAN = {"running": False, "started": 0.0, "done": 0, "total": 0,
         "result": None, "error": None}
_SCAN_LOCK = threading.Lock()


def _scan_progress(done, total):
    with _SCAN_LOCK:
        _SCAN["done"], _SCAN["total"] = done, total


def scan_start(body):
    with _SCAN_LOCK:
        if _SCAN["running"]:
            return {"running": True, "done": _SCAN["done"], "total": _SCAN["total"]}
        _SCAN.update(running=True, started=time.time(), done=0, total=0,
                     result=None, error=None)
    threading.Thread(target=_scan_worker, args=(body,), daemon=True).start()
    return {"running": True, "done": 0, "total": 0}


def _scan_worker(body):
    try:
        data = run_scan(body, progress=_scan_progress)
        with _SCAN_LOCK:
            _SCAN["result"], _SCAN["error"] = data, None
    except Exception as e:
        log_error("/api/scan", e)
        with _SCAN_LOCK:
            _SCAN["result"] = None
            _SCAN["error"] = "扫描时服务内部出错，原因已记录到 finance_error.log，请重启程序后重试"
    finally:
        with _SCAN_LOCK:
            _SCAN["running"] = False


def scan_status():
    with _SCAN_LOCK:
        st = dict(_SCAN)
    if st["running"]:
        st["elapsed"] = time.time() - st["started"]
    return st


def run_scan(body, progress=None):
    """执行一次完整查重扫描并返回响应 dict；异常由后台任务兜底并写入 _SCAN.error。"""
    watch = body.get("watch_dirs") or []
    used = body.get("used_dirs") or []
    ocr = bool(body.get("ocr_enabled", True))
    # 模型由服务端自动挑选（最新免费优先 + 自动降级），不再读取客户端传入的 ocr_model
    model = pick_vision_model() if ocr and zhipu_key() else (body.get("ocr_model") or "")
    cfg = load_config()
    cfg.update({"watch_dirs": watch, "used_dirs": used,
                "ocr_model": model, "ocr_enabled": ocr,
                "archive_dir": body.get("archive_dir") or ""})
    if body.get("company_names"):
        cfg["company_names"] = body["company_names"]
    with STATE_LOCK:
        save_config(cfg)
        led, used_set, recs = build_scan(watch, used, ocr, model, progress=progress)
    catalog = get_categories(cfg)
    cats = {}
    for c in catalog:
        cats[c["id"]] = {"label": c["label"], "note": c["note"], "count": 0, "sum": 0.0}
    n_dup, n_used, n_warn, n_amt, n_itin = 0, 0, 0, 0, 0
    for r in recs:
        if r.get("itinerary"):
            n_itin += 1
            continue
        if r["is_used"]:
            n_used += 1
        if r["dups"]:
            n_dup += 1
        if r.get("warn"):
            n_warn += 1
        if r.get("amount_cents") is not None:
            n_amt += 1
        cid = r.get("cat_id") or "other"
        if cid in cats:
            cats[cid]["count"] += 1
            cats[cid]["sum"] += (r["amount_cents"] or 0) / 100
    recs.sort(key=lambda r: (-1 if r["dups"] else 0, -1 if r["is_used"] else 0,
                             -(r["amount_cents"] or 0)))
    pairs = {tuple(sorted((r["path"], d["path"]))) for r in recs for d in r["dups"]}
    reused = sum(r.get("in_watch") and any(d.get("is_used") for d in r["dups"]) for r in recs)
    watch_count = sum(bool(r.get("in_watch")) for r in recs)
    used_count = sum(bool(r.get("in_used_dir")) for r in recs)
    ai_error = next((r.get("warn") for r in recs if r.get("warn") and
                     ("OCR失败" in r["warn"] or "PDF页OCR失败" in r["warn"])), "")
    return {
        "records": recs, "categories": cats,
        "stats": {"total": len(recs), "dup": n_dup, "used": n_used,
                  "warn": sum(bool(r.get("in_watch") and r.get("warn")) for r in recs),
                  "warn_all": n_warn, "amount_ok": n_amt,
                  "ocr_key": bool(zhipu_key()), "dup_pairs": len(pairs),
                  "reused": reused, "watch_count": watch_count,
                  "used_count": used_count, "itinerary": n_itin,
                  "ai_error": ai_error},
        "config": {**cfg, "ocr_key_configured": bool(zhipu_key())},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--test", nargs="*", help="命令行只读自测目录")
    a = ap.parse_args()
    enable_crash_log()
    if a.test is not None:
        cfg = load_config()
        dirs = a.test or cfg.get("watch_dirs") or []
        led, used_set, recs = build_scan(dirs, cfg.get("used_dirs") or [], True,
                                         cfg.get("ocr_model") or pick_vision_model())
        for r in sorted(recs, key=lambda r: (r["folder"], r["fname"])):
            dup = ("重复[%s]" % ", ".join("%s/%s:%s" % (d["folder"], d["fname"], d["basis"])
                                          for d in r["dups"])) if r["dups"] else ""
            print("%-6s %-24s %8s %-10s %-20s %-14s %-20s %s%s" % (
                r["folder"], r["fname"][:24],
                "%.2f" % ((r["amount_cents"] or 0) / 100) if r["amount_cents"] is not None else "-",
                r.get("date") or "-", (r.get("no") or "-")[:20],
                (r.get("cat_label") or "")[:14], (r.get("seller") or "-")[:20],
                dup, (" | " + r["warn"]) if r.get("warn") else ""))
        print("\n合计 %d 张 | 重复风险 %d | 已使用 %d | OCR可用=%s | 金额已解析 %d" % (
            len(recs), sum(1 for x in recs if x["dups"]),
            sum(1 for x in recs if x["is_used"]), bool(zhipu_key()),
            sum(1 for x in recs if x.get("amount_cents") is not None)))
        return 0
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = "http://127.0.0.1:%d" % a.port
    ensure_local_ocr_async()  # 未装本机识别组件时后台静默安装，用户无感知
    print("财小盒已启动: %s  (Ctrl+C 退出)  · 发票管家 v%s" % (url, APP_VERSION))
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
