# -*- coding: utf-8 -*-
"""命令行入口 —— 不依赖浏览器即可使用核心功能。

用法示例：
  python cli.py scan                          # 扫描已配置目录
  python cli.py scan D:\\发票\\202609          # 扫描指定目录
  python cli.py export --format xlsx           # 导出为 Excel
  python cli.py status                         # 查看台账状态
  python cli.py config                         # 查看当前配置
"""
import argparse
import json
import sys
import os

# 确保能 import financekit
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from financekit.paths import ensure_user_dir
from financekit.invoice import (
    APP_VERSION, load_config, build_scan, pick_vision_model, zhipu_key,
    export_xlsx, export_csv, get_categories,
)
from financekit.store import load_ledger, load_used_ledger

ensure_user_dir()


def cmd_scan(args):
    """扫描发票目录。"""
    cfg = load_config()
    dirs = args.dirs or cfg.get("watch_dirs") or []
    if not dirs:
        print("未指定扫描目录，也未配置 watch_dirs。")
        print("用法：python cli.py scan <目录1> [目录2] ...")
        return 1

    used_dirs = cfg.get("used_dirs") or []
    ocr_model = cfg.get("ocr_model") or pick_vision_model()

    print(f"扫描 {len(dirs)} 个目录...")
    led, used_set, recs = build_scan(dirs, used_dirs, True, ocr_model)

    # 打印结果
    for r in sorted(recs, key=lambda r: (r["folder"], r["fname"])):
        dup = ("重复[%s]" % ", ".join(
            "%s/%s:%s" % (d["folder"], d["fname"], d["basis"])
            for d in r["dups"]
        )) if r["dups"] else ""
        warn = (" | " + r["warn"]) if r.get("warn") else ""
        amt = "%.2f" % ((r["amount_cents"] or 0) / 100) if r["amount_cents"] is not None else "-"
        print("%-6s %-24s %8s %-10s %-20s %-14s %-20s %s%s" % (
            r["folder"], r["fname"][:24], amt,
            r.get("date") or "-", (r.get("no") or "-")[:20],
            (r.get("cat_label") or "")[:14], (r.get("seller") or "-")[:20],
            dup, warn
        ))

    print("\n合计 %d 张 | 重复风险 %d | 已使用 %d | OCR可用=%s | 金额已解析 %d" % (
        len(recs),
        sum(1 for x in recs if x["dups"]),
        sum(1 for x in recs if x["is_used"]),
        bool(zhipu_key()),
        sum(1 for x in recs if x.get("amount_cents") is not None)
    ))
    return 0


def cmd_export(args):
    """导出台账。"""
    led = load_ledger()
    recs = led.get("records", {}) if isinstance(led, dict) else led
    if not recs:
        print("台账为空，请先扫描发票。")
        return 1

    fmt = args.format or "xlsx"
    cats = get_categories()

    if fmt == "csv":
        path = export_csv(recs, cats)
    else:
        path = export_xlsx(recs, cats)

    print(f"已导出：{path}")
    return 0


def cmd_status(args):
    """查看台账状态。"""
    led = load_ledger()
    recs = led.get("records", {}) if isinstance(led, dict) else led
    used = load_used_ledger()

    total = len(recs)
    used_count = len(used)
    pending = sum(1 for r in recs.values() if not r.get("status"))

    print(f"台账总计：{total} 张")
    print(f"已使用：  {used_count} 张")
    print(f"待处理：  {pending} 张")

    if recs:
        dates = [r.get("date") for r in recs.values() if r.get("date")]
        if dates:
            print(f"日期范围：{min(dates)} ~ {max(dates)}")

    return 0


def cmd_config(args):
    """查看/修改配置。"""
    cfg = load_config()

    if args.show:
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
        return 0

    # 默认显示关键配置
    print(f"版本：      {APP_VERSION}")
    print(f"扫描目录：  {len(cfg.get('watch_dirs', []))} 个")
    print(f"已用目录：  {len(cfg.get('used_dirs', []))} 个")
    print(f"归档目录：  {cfg.get('archive_dir') or '(未设置)'}")
    print(f"OCR 模型：  {cfg.get('ocr_model') or '(自动)'}")
    print(f"智谱 Key：  {'已配置' if zhipu_key() else '未配置'}")
    print(f"公司名称：  {', '.join(cfg.get('company_names', [])) or '(未设置)'}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="财小盒 CLI")
    ap.add_argument("--version", action="version", version=f"财小盒 v{APP_VERSION}")

    sub = ap.add_subparsers(dest="command")

    # scan
    p_scan = sub.add_parser("scan", help="扫描发票目录")
    p_scan.add_argument("dirs", nargs="*", help="要扫描的目录（默认使用配置中的 watch_dirs）")

    # export
    p_export = sub.add_parser("export", help="导出台账")
    p_export.add_argument("--format", choices=["xlsx", "csv"], default="xlsx", help="导出格式（默认 xlsx）")

    # status
    sub.add_parser("status", help="查看台账状态")

    # config
    p_config = sub.add_parser("config", help="查看配置")
    p_config.add_argument("--show", action="store_true", help="显示完整配置 JSON")

    args = ap.parse_args()

    if not args.command:
        ap.print_help()
        return 0

    cmd_map = {
        "scan": cmd_scan,
        "export": cmd_export,
        "status": cmd_status,
        "config": cmd_config,
    }

    return cmd_map[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
