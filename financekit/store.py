# -*- coding: utf-8 -*-
"""台账存储：SQLite 主存，首次自动从 JSON 迁移；写入仍滚动备份 JSON 快照便于手工还原。"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import threading
import time

from .paths import (
    APP_DIR, BACKUP_DIR, BACKUP_KEEP, DB_PATH, ENGINE_VER, LEDGER,
    LEDGER_STATUS, USED_LEDGER,
)

_LOCK = threading.RLock()
_MIGRATED = False


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS invoices (
      path TEXT PRIMARY KEY,
      data TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS used_items (
      item_key TEXT PRIMARY KEY,
      data TEXT NOT NULL,
      closed_at REAL
    );
    """)
    row = conn.execute("SELECT value FROM meta WHERE key='engine_ver'").fetchone()
    if not row:
        conn.execute("INSERT INTO meta(key,value) VALUES('engine_ver',?)", (str(ENGINE_VER),))
    conn.commit()


def _rotate_backup(path, tag=""):
    if not os.path.exists(path):
        return None
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        name = os.path.basename(path)
        stamp = time.strftime("%Y%m%d-%H%M%S") + ("-" + tag if tag else "")
        dst = os.path.join(BACKUP_DIR, "%s.%s.bak" % (name, stamp))
        i = 1
        while os.path.exists(dst):
            dst = os.path.join(BACKUP_DIR, "%s.%s-%d.bak" % (name, stamp, i))
            i += 1
        shutil.copy2(path, dst)
        olds = sorted(glob.glob(os.path.join(BACKUP_DIR, name + ".*.bak")), reverse=True)
        for old in olds[BACKUP_KEEP:]:
            try:
                os.remove(old)
            except OSError:
                pass
        return dst
    except OSError:
        return None


def list_backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    items = []
    for p in sorted(glob.glob(os.path.join(BACKUP_DIR, "*.bak")), reverse=True):
        try:
            items.append({"name": os.path.basename(p), "size": os.path.getsize(p),
                          "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                                 time.localtime(os.path.getmtime(p)))})
        except OSError:
            continue
    return items[:20]


def _dump_json_snapshot(led):
    """兼容旧还原流程：SQLite 写入后同步一份 JSON 快照（可被 restore 读回）。"""
    try:
        _rotate_backup(LEDGER)
        tmp = LEDGER + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(led, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, LEDGER)
    except OSError:
        pass


def _dump_used_snapshot(data):
    try:
        _rotate_backup(USED_LEDGER)
        tmp = USED_LEDGER + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, USED_LEDGER)
    except OSError:
        pass


def _load_json_ledger():
    if not os.path.exists(LEDGER):
        return None
    try:
        with open(LEDGER, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("records"), dict):
            return data
        raise ValueError("台账结构异常")
    except Exception as e:
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            shutil.copy2(LEDGER, os.path.join(
                BACKUP_DIR, "invoice_ledger.corrupt-%s.bak" % time.strftime("%Y%m%d-%H%M%S")))
        except OSError:
            pass
        LEDGER_STATUS["error"] = "台账文件损坏或格式异常（%s），已自动留档，可在「台账恢复」中还原。" % e
        LEDGER_STATUS["backups"] = list_backups()
        return {"version": ENGINE_VER, "records": {}, "_corrupt": True}


def _load_json_used():
    if not os.path.exists(USED_LEDGER):
        return {"version": 1, "items": []}
    try:
        with open(USED_LEDGER, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except Exception:
        pass
    return {"version": 1, "items": []}


def used_item_key(r):
    """永久库唯一键：优先 号码+金额，其次 代码+号码。"""
    no = (r or {}).get("no")
    cents = (r or {}).get("amount_cents")
    if no and cents is not None:
        return "no+amt|%s|%s" % (no, cents)
    code = (r or {}).get("code")
    if no and code:
        return "code+no|%s|%s" % (code, no)
    return ""


def _migrate_if_needed():
    global _MIGRATED
    if _MIGRATED:
        return
    with _LOCK:
        if _MIGRATED:
            return
        conn = _connect()
        try:
            _ensure_schema(conn)
            n = conn.execute("SELECT COUNT(*) AS c FROM invoices").fetchone()["c"]
            if n == 0:
                led = _load_json_ledger()
                if led and not led.get("_corrupt") and led.get("records"):
                    ver = led.get("version", ENGINE_VER)
                    conn.execute(
                        "INSERT OR REPLACE INTO meta(key,value) VALUES('engine_ver',?)",
                        (str(ver),))
                    conn.executemany(
                        "INSERT OR REPLACE INTO invoices(path,data) VALUES(?,?)",
                        [(p, json.dumps(r, ensure_ascii=False))
                         for p, r in led["records"].items()])
                    conn.commit()
            u = conn.execute("SELECT COUNT(*) AS c FROM used_items").fetchone()["c"]
            if u == 0:
                used = _load_json_used()
                rows = []
                for it in used.get("items") or []:
                    key = used_item_key(it)
                    if not key:
                        continue
                    rows.append((key, json.dumps(it, ensure_ascii=False), it.get("closed_at")))
                if rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO used_items(item_key,data,closed_at) VALUES(?,?,?)",
                        rows)
                    conn.commit()
            # 标记迁移完成
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('migrated_at',?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"),))
            conn.commit()
        finally:
            conn.close()
        _MIGRATED = True


def load_ledger():
    """读取台账（SQLite）。损坏/空库时回退 JSON；API 与旧版一致。"""
    _migrate_if_needed()
    with _LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            ver_row = conn.execute("SELECT value FROM meta WHERE key='engine_ver'").fetchone()
            ver = int(ver_row["value"]) if ver_row else ENGINE_VER
            rows = conn.execute("SELECT path, data FROM invoices").fetchall()
            records = {}
            for row in rows:
                try:
                    records[row["path"]] = json.loads(row["data"])
                except Exception:
                    continue
            if not records and os.path.exists(LEDGER):
                led = _load_json_ledger()
                if led:
                    LEDGER_STATUS["record_count"] = len(led.get("records", {}))
                    if not led.get("_corrupt"):
                        LEDGER_STATUS["error"] = None
                    return {"version": led.get("version", ENGINE_VER),
                            "records": led.get("records", {})}
            LEDGER_STATUS["error"] = None
            LEDGER_STATUS["record_count"] = len(records)
            return {"version": ver, "records": records}
        finally:
            conn.close()


def save_ledger(led):
    _migrate_if_needed()
    with _LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            ver = led.get("version", ENGINE_VER)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('engine_ver',?)", (str(ver),))
            conn.execute("DELETE FROM invoices")
            conn.executemany(
                "INSERT INTO invoices(path,data) VALUES(?,?)",
                [(p, json.dumps(r, ensure_ascii=False))
                 for p, r in (led.get("records") or {}).items()])
            conn.commit()
            LEDGER_STATUS["record_count"] = len(led.get("records", {}))
            LEDGER_STATUS["error"] = None
        finally:
            conn.close()
        _dump_json_snapshot(led)


def restore_ledger(name):
    """从 backups 目录的 JSON 备份还原到 SQLite（并写回 JSON 快照）。"""
    path = os.path.join(BACKUP_DIR, os.path.basename(name))
    if not os.path.isfile(path) or os.path.dirname(os.path.abspath(path)) != os.path.abspath(BACKUP_DIR):
        raise ValueError("备份文件不存在")
    try:
        data = json.load(open(path, encoding="utf-8"))
        if not (isinstance(data, dict) and isinstance(data.get("records"), dict)):
            raise ValueError("该备份不是有效的台账文件")
    except Exception as e:
        raise ValueError("备份无法解析：%s" % e)
    _rotate_backup(LEDGER, "prerestore")
    # 先把当前 SQLite 也落一份 JSON prerestore（若已有库）
    try:
        cur = load_ledger()
        _dump_json_snapshot(cur)
        _rotate_backup(LEDGER, "prerestore-sqlite")
    except Exception:
        pass
    save_ledger(data)
    LEDGER_STATUS["error"] = None
    return len(data["records"])


def load_used_ledger():
    _migrate_if_needed()
    with _LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            rows = conn.execute(
                "SELECT data FROM used_items ORDER BY closed_at ASC, item_key ASC").fetchall()
            items = []
            for row in rows:
                try:
                    items.append(json.loads(row["data"]))
                except Exception:
                    continue
            if not items and os.path.exists(USED_LEDGER):
                return _load_json_used()
            return {"version": 1, "items": items}
        finally:
            conn.close()


def save_used_ledger(data):
    _migrate_if_needed()
    with _LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.execute("DELETE FROM used_items")
            rows = []
            for it in data.get("items") or []:
                key = used_item_key(it)
                if not key:
                    continue
                rows.append((key, json.dumps(it, ensure_ascii=False), it.get("closed_at")))
            conn.executemany(
                "INSERT OR REPLACE INTO used_items(item_key,data,closed_at) VALUES(?,?,?)",
                rows)
            conn.commit()
        finally:
            conn.close()
        _dump_used_snapshot(data)


def used_ledger_add(records, batch=""):
    data = load_used_ledger()
    seen = set()
    for it in data["items"]:
        seen.add(used_item_key(it))
    added = 0
    now = time.time()
    for r in records:
        key = used_item_key(r)
        if not key or key in seen:
            continue
        seen.add(key)
        data["items"].append({
            "no": r.get("no"), "code": r.get("code"),
            "amount_cents": r.get("amount_cents"), "date": r.get("date"),
            "seller": r.get("seller"), "buyer": r.get("buyer"),
            "kind": r.get("kind"), "cat_label": r.get("cat_label"),
            "fname": r.get("fname"), "path": r.get("path"),
            "batch": batch or time.strftime("%Y-%m"), "closed_at": now,
        })
        added += 1
    if added:
        save_used_ledger(data)
    return added
