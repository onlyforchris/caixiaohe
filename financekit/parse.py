# -*- coding: utf-8 -*-
"""纯文本处理：分类、字段提取、指纹、校验、OFD 解析。无网络、无可变状态。"""
import io
import os
import re
import time
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from decimal import Decimal

from financekit.paths import BUYER_DEFAULT

CATALOG = [
    {"id": "office", "label": "办公费", "note": "办公用品、打印纸、快递、饮用水、绿植"},
    {"id": "travel", "label": "差旅费", "note": "机票、火车票、住宿、打车、订票手续费"},
    {"id": "transport", "label": "交通费", "note": "网约车、加油、停车、过路、租车"},
    {"id": "communication", "label": "通讯费", "note": "办公电话、宽带、通信服务"},
    {"id": "hospitality", "label": "业务招待费", "note": "餐饮招待、茶叶、礼品、宴请"},
    {"id": "meeting", "label": "会议费", "note": "场地、资料"},
    {"id": "training", "label": "培训费", "note": "培训、报名费"},
    {"id": "advertising", "label": "广告宣传费", "note": "广告服务费、制作费、推广费"},
    {"id": "service", "label": "服务费", "note": "技术服务费、招聘费"},
    {"id": "rd", "label": "研发费用", "note": "专利/软著、研发设备、认证检测"},
    {"id": "property", "label": "房租物业水电", "note": "租赁、物业、水电"},
    {"id": "benefit", "label": "人事福利", "note": "团建、下午茶、餐饮、零食、食品"},
    {"id": "other", "label": "其他/待分类", "note": "建议人工补充规则"},
]
CAT_IDS = [c["id"] for c in CATALOG]

RULES = [
    ("office", ["办公用品", "打印纸", "快递", "收派服务", "物流网络", "饮用水", "绿植", "文具", "耗材", "硒鼓", "墨盒", "a4纸"]),
    ("travel", ["机票", "火车票", "住宿", "打车", "订票手续费", "行程单", "高铁", "动车", "酒店", "宾馆"]),
    ("transport", ["网约车", "客运服务费", "加油", "车用乙醇", "汽油", "柴油", "停车", "过路", "租车", "滴滴", "出租车", "通行费", "代驾"]),
    ("communication", ["办公电话", "宽带", "通信服务", "中国移动", "中国联通", "中国电信", "话费"]),
    ("hospitality", ["餐饮招待", "宴请", "茶叶", "礼品"]),
    ("meeting", ["会议场地", "会议资料", "会议费"]),
    ("training", ["培训", "报名费", "课程", "训练营", "考试费"]),
    ("advertising", ["广告服务费", "制作费", "推广费", "广告宣传"]),
    ("service", ["技术服务费", "技术服务", "招聘费", "软件服务", "云服务", "咨询服务"]),
    ("rd", ["专利", "软著", "软件著作权", "研发设备", "认证检测", "认证费", "检测费"]),
    ("property", ["房屋租赁", "租赁费", "物业", "水费", "电费", "水电"]),
    ("benefit", ["团建", "下午茶", "员工餐", "餐饮", "餐费", "外卖", "咖啡", "奶茶", "零食", "食品", "熟肉制品", "酱板鸭", "水果", "牛奶", "乳制品", "纯奶"]),
]

DEFAULT_CATEGORIES = [
    {**c, "keywords": next((list(kws) for cid, kws in RULES if cid == c["id"]), [])}
    for c in CATALOG
]

COMPANY_RE = re.compile(
    r"(?=[\u4e00-\u9fa5（(])"
    r"[\u4e00-\u9fa5A-Za-z0-9（）()·]+?"
    r"(?:有限公司|有限责任公司|股份有限公司|个体工商户|合伙企业|工作室"
    r"|服务中心|购物中心|合作社|事务所|招待所|门市部|经营部|加工厂|汽修厂|幼儿园|食堂"
    r"|商行|超市|餐厅|饭店|酒店|宾馆|大药房|药店|诊所|美容院|医院"
    r"|便利店|小吃店|水果店|奶茶店|咖啡店|甜品店|烘焙店|理发店|打印店|图文店"
    r"|馆|铺|中心|会所|坊|美食城"
    r"|店)")

EXTRACT_REV = 3


def get_categories(cfg=None):
    if cfg is None:
        from financekit.invoice import load_config
        cfg = load_config()
    cats = cfg.get("categories")
    return cats if isinstance(cats, list) and cats else DEFAULT_CATEGORIES


def classify_text(text, categories=None):
    t = (text or "").lower()
    t = re.sub(r"\s+", "", t)
    if not t:
        return "other", "无内容"
    matches = []
    for order, cat in enumerate(categories or DEFAULT_CATEGORIES):
        for k in cat.get("keywords", []):
            if k in t:
                matches.append((len(k), -order, cat["id"], k))
    if matches:
        _length, _order, cid, keyword = max(matches)
        return cid, keyword
    return "other", "未命中规则"


def _strip_ws(s):
    return re.sub(r"\s+", "", s or "")


def norm_amount_cents(raw):
    if raw is None:
        return None
    try:
        return int(Decimal(re.sub(r"[^\d.]", "", str(raw))) * 100)
    except Exception:
        return None


def _valid_date(y, m, d):
    try:
        y, m, d = int(y), int(m), int(d)
        return 1900 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31
    except Exception:
        return False


def norm_date(raw):
    if not raw:
        return None
    m = re.search(r"(\d{4})[年\-/.](\d{1,2})[月\-/.](\d{1,2})", str(raw))
    if m and _valid_date(*m.groups()):
        return "%04d-%02d-%02d" % tuple(int(x) for x in m.groups())
    m = re.search(r"(\d{4})(\d{2})(\d{2})", str(raw))
    if m and _valid_date(*m.groups()):
        return "%04d-%02d-%02d" % tuple(int(x) for x in m.groups())
    return None


def _companies(text):
    out = []
    sources = [(text or "", True),
               (re.sub(r"(?<=[\u4e00-\u9fa5（）()·])[ \t\u00a0]+(?=[\u4e00-\u9fa5（）()·])",
                       "", text or ""), False)]
    for src, allow_taxgap in sources:
        candidates = COMPANY_RE.findall(src)
        candidates += re.findall(r"名称[:：]\s*([^\n]{2,60})", src)
        if allow_taxgap:
            taxes = list(re.finditer(r"(?<![0-9A-Z])[0-9A-Z]{15,20}(?![0-9A-Z])", src))
            for a, b in zip(taxes, taxes[1:]):
                candidates += COMPANY_RE.findall(src[a.end():b.start()])
        for c in candidates:
            c = re.sub(r"\s+", "", c).strip(":：")
            if (4 <= len(re.findall(r"[\u4e00-\u9fa5]", c)) <= 40
                    and not re.search(r"发票|统一社会信用|纳税人识别号|项目名称", c)
                    and not norm_date(c)
                    and c not in out):
                out.append(c)
    return [c for c in out if not any(c != o and c in o for o in out)]


def is_itinerary_name(fname):
    stem = os.path.splitext(os.path.basename(str(fname)))[0]
    return "行程单" in stem or "行程报销单" in stem


def _itinerary_fields():
    return {"no": None, "code": None, "date": None, "amount_cents": None,
            "amount_excl_cents": None, "tax_cents": None,
            "seller": None, "buyer": None, "kind": "行程单",
            "items": [], "svc": [], "itinerary": True}


def _stem_core(fname, itinerary=None):
    stem = os.path.splitext(os.path.basename(str(fname)))[0]
    if itinerary is None:
        itinerary = is_itinerary_name(fname)
    if itinerary:
        stem = re.sub(r"(电子)?(?:行程报销单|行程单)", "", stem)
    else:
        stem = re.sub(r"(电子)?发票$", "", stem)
    return re.sub(r"[\s\-_]+$", "", stem)


def fingerprint_for(fields, md5hex):
    return [] if fields.get("itinerary") else fingerprint(fields, md5hex)


def pair_itineraries(recs):
    """行程单与同名（文件名核心一致）发票互相挂链：发票挂 itins，行程单挂 pair。"""
    for r in recs:
        r.pop("itins", None)
        r.pop("pair", None)
    inv_cores = {}
    for r in recs:
        if not r.get("itinerary"):
            c = _stem_core(r["fname"])
            if c:
                inv_cores.setdefault(c, []).append(r)
    for it in recs:
        if not it.get("itinerary"):
            continue
        tc = _stem_core(it["fname"], itinerary=True)
        if not tc:
            continue
        exact = list(inv_cores.get(tc) or [])
        if len(exact) == 1:
            match = exact[0]
        elif len(exact) > 1:
            same = [r for r in exact if r["folder"] == it["folder"]]
            match = same[0] if len(same) == 1 else None
        else:
            match = None
        if match is None:
            near = [c for c in inv_cores if c in tc or tc in c]
            if len(near) == 1:
                match = inv_cores[near][0]
        if match is not None:
            it["pair"] = {"path": match["path"], "fname": match["fname"]}
            match.setdefault("itins", []).append(
                {"path": it["path"], "fname": it["fname"]})


def extract_fields(text, fname="", company_names=None):
    """返回字段 dict。金额以「价税合计（小写）」为准。"""
    t = unicodedata.normalize("NFKC", (text or "").replace("\x00", "")).translate(
        str.maketrans({"⻔": "门", "⻝": "食"}))
    t = re.sub(r"(?<=[0-9¥￥.])[ \t\r\n]+(?=[0-9¥￥.])", "", t)
    t = re.sub(r"价\s*税\s*合\s*计", "价税合计", t)
    t = re.sub(r"合\s*计", "合计", t)
    t = re.sub(r"小\s*写", "小写", t)
    t2 = re.sub(r"(\d),(?=\d{3})", r"\1", t)
    f = {"no": None, "code": None, "date": None, "amount_cents": None,
         "seller": None, "buyer": None, "kind": "其他", "items": []}

    m = re.search(r"发票号码[:：\s]*([0-9]{8,20})", t2)
    if not m:
        m = re.search(r"([0-9]{20})", t2)
    f["no"] = m.group(1) if m else None
    m = re.search(r"发票代码[:：\s]*([0-9]{10,12})", t2)
    f["code"] = m.group(1) if m else None

    m = re.search(r"开票日期[^0-9]{0,8}([0-9]{4}[年\-/.]\d{1,2}[月\-/.]\d{1,2})", t2)
    if not m:
        m = re.search(r"开票日期[^0-9]{0,8}(\d{8})", t2)
    if m:
        f["date"] = norm_date(m.group(1))
    if not f["date"]:
        anchor = t2.find(f["no"]) if f["no"] else t2.find("价税合计")
        if anchor < 0:
            anchor = 0
        dates = list(re.finditer(r"(\d{4})[年\-/.](\d{1,2})[月\-/.](\d{1,2})", t2))
        if dates:
            dates = sorted(dates, key=lambda x: abs(x.start() - anchor))
            f["date"] = norm_date(dates[0].group(0))
    if not f["date"] and f["no"]:
        tail = t2[t2.find(f["no"]) + len(f["no"]):][:120]
        m = re.search(r"(20\d{2})\D{0,6}(\d{2})\D{0,6}(\d{2})", tail)
        if m and _valid_date(*m.groups()):
            f["date"] = "%04d-%02d-%02d" % tuple(int(x) for x in m.groups())

    amt = None
    for pat in (r"价税合计[^¥￥]{0,24}[¥￥]\s*([0-9]+\.\d{2})",
                r"价税合计[^0-9]{0,20}([0-9]+\.\d{2})",
                r"[（(]\s*小写\s*[)）]\s*[:：]?\s*[¥￥]?\s*([0-9]+\.\d{2})",
                r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分整]{3,}\s*[¥￥]\s*([0-9]+\.\d{2})",
                r"(?:合计|合计金额)[^0-9¥￥]{0,12}([0-9]+\.\d{2})"):
        m = re.search(pat, t2)
        if m:
            amt = m.group(1)
            break
    if amt is None:
        nums = re.findall(r"[¥￥]\s*([0-9]+\.\d{2})", t2) + \
               re.findall(r"(?<![0-9.])([0-9]+\.\d{2})[ \t]*[¥￥]", t2)
        if nums:
            amt = max(nums, key=lambda x: Decimal(x))
    f["amount_cents"] = norm_amount_cents(amt)

    f["amount_excl_cents"] = None
    f["tax_cents"] = None
    m = re.search(r"合\s*计[^0-9¥￥\n]{0,10}[¥￥]?\s*([0-9]+\.\d{2})[^0-9¥￥\n]{0,10}[¥￥]?\s*([0-9]+\.\d{2})", t2)
    if m:
        f["amount_excl_cents"] = norm_amount_cents(m.group(1))
        f["tax_cents"] = norm_amount_cents(m.group(2))

    buyers = company_names or BUYER_DEFAULT
    comps = _companies(t2)
    if comps:
        b = next((c for c in comps if any(bk in c for bk in buyers)), comps[0])
        s = next((c for c in comps if c != b), None)
        f["buyer"], f["seller"] = b, s

    if ("数电" in t2 or "电子发票（普通" in t2 or "电子发票(普通" in t2
            or "电子发票（专用" in t2 or "电子发票(专用" in t2):
        f["kind"] = "数电票"
    elif "增值税专用发票" in t2:
        f["kind"] = "增值税专用发票"
    elif "增值税普通发票" in t2:
        f["kind"] = "增值税普通发票"
    elif "行程单" in t2:
        f["kind"] = "行程单"
    elif "通行费" in t2:
        f["kind"] = "通行费发票"
    elif "出租车" in t2:
        f["kind"] = "出租车票"

    if is_itinerary_name(fname) or (f["kind"] == "行程单" and f["no"] is None):
        f.update(_itinerary_fields())
        return f

    items = []
    m = re.search(r"项目名称[^\n]{0,12}\n", t2)
    if m:
        seg = t2[m.end():m.end() + 400]
        for line in seg.splitlines():
            line = line.strip()
            if not line or re.search(r"规格型号|单位|数量|单价|金额|税率|税额|价税合计|备注|合计", line):
                break
            if len(line) > 3:
                items.append(line[:80])
    f["items"] = items[:6]
    f["svc"] = re.findall(r"\*[^*\n]{1,30}\*([^*\n]{2,40})", t2)[:4]

    return f


def parse_history_csv(text):
    """解析财务已有的 Excel/CSV 台账（列顺序不限），最小集 = 号码 + 金额。"""
    import csv as _csv
    out = []
    for row in _csv.reader(io.StringIO(text or "")):
        cells = [c.strip() for c in row if c and c.strip()]
        if not cells:
            continue
        no = date = seller = None
        cents = None

        for c in cells:
            if date is None:
                d = norm_date(c)
                if d:
                    date = d
                    continue
            if no is None and re.fullmatch(r"\d{8}|\d{20}", c):
                no = c
                continue
            if cents is None and re.fullmatch(r"[¥￥]?\s*-?\d+\.\d{2}", c):
                cents = norm_amount_cents(c)
                continue
            if seller is None and len(c) <= 40 and re.search(r"[\u4e00-\u9fa5]{4}", c) \
                    and not re.search(r"已报|未报|报销|发票|状态|分类|合计", c):
                seller = c
        if no or (cents is not None and date):
            out.append({"no": no, "code": None, "amount_cents": cents, "date": date,
                        "seller": seller, "fname": "（历史台账导入）", "path": "",
                        "buyer": None, "kind": None, "cat_label": None})
    return out


def archive_name(r):
    """归档规范命名：日期_销售方_金额_号码，便于日后按文件名检索。"""
    ext = os.path.splitext(r.get("fname") or "")[1]
    if not r.get("no"):
        stem = os.path.splitext(r.get("fname") or "未识别发票")[0]
        return re.sub(r'[\\/:*?"<>|\r\n\t]', "", stem)[:60] + ext
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]', "", r.get("seller") or "未知销售方")[:20]
    return "_".join([r.get("date") or "无日期", safe,
                     "%.2f" % ((r.get("amount_cents") or 0) / 100),
                     r.get("no")]) + ext


def validate_fields(f, today=None):
    """字段自校验。抓不到就不校验，宁可少报也不误报。"""
    issues = []
    no = f.get("no")
    if no and len(no) not in (8, 20):
        issues.append("发票号码位数异常（%d 位，常见为 8 位或数电票 20 位）" % len(no))
    date = f.get("date")
    if date:
        today = today or time.strftime("%Y-%m-%d")
        if date > today:
            issues.append("开票日期晚于今天（%s）" % date)
        elif date < "2010-01-01":
            issues.append("开票日期过早（%s）" % date)
    cents = f.get("amount_cents")
    if cents is not None:
        if cents <= 0:
            issues.append("金额异常（≤0）")
        elif cents > 1000000000:
            issues.append("金额异常偏大，请核对票面")
    excl, tax = f.get("amount_excl_cents"), f.get("tax_cents")
    if cents is not None and excl is not None and tax is not None:
        if abs(excl + tax - cents) > 100:
            issues.append("金额自洽未通过：不含税 %.2f + 税额 %.2f ≠ 价税合计 %.2f" % (
                excl / 100, tax / 100, cents / 100))
    return issues


def check_buyer(f, company_names):
    """抬头校验：未配置本公司名称时不校验，避免无意义告警。"""
    names = [n for n in (company_names or []) if n]
    if not names:
        return "未校验", f.get("buyer")
    buyer = f.get("buyer") or ""
    if not buyer:
        return "待核对", ""
    return ("符合" if any(n in buyer for n in names) else "不符"), buyer


def _xml_local(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def read_ofd_text(path):
    """解析 OFD 票面文本：OFD 本质是 zip，票面文字在 Content.xml 的 TextCode 中。"""
    out = []
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            root_name = "OFD.xml"
            if root_name not in names:
                root_name = next((n for n in names if n.lower().endswith("ofd.xml")), None)
            if not root_name:
                return ""
            doc_root = None
            try:
                root = ET.fromstring(zf.read(root_name))
                for el in root.iter():
                    if _xml_local(el.tag) == "DocRoot" and (el.text or "").strip():
                        doc_root = el.text.strip().lstrip("/")
                        break
            except Exception:
                return ""
            if not doc_root:
                return ""
            base = os.path.dirname(doc_root)
            doc_dir = (base + "/") if base else ""
            page_locs = []
            try:
                doc = ET.fromstring(zf.read(doc_root))
                for el in doc.iter():
                    if _xml_local(el.tag) == "Page":
                        loc = el.attrib.get("BaseLoc") or ""
                        if loc:
                            page_locs.append(loc.lstrip("/"))
            except Exception:
                return ""
            for loc in page_locs:
                full = loc if loc in names else (doc_dir + loc)
                if full not in names:
                    continue
                try:
                    content = ET.fromstring(zf.read(full))
                except Exception:
                    continue
                items = []
                for obj in content.iter():
                    if _xml_local(obj.tag) != "TextObject":
                        continue
                    y = 0.0
                    try:
                        b = obj.attrib.get("Boundary") or ""
                        if b:
                            y = float(b.split()[1])
                    except (IndexError, ValueError):
                        pass
                    txt = "".join(t.text or "" for t in obj.iter()
                                  if _xml_local(t.tag) == "TextCode")
                    if txt.strip():
                        items.append((y, txt))
                items.sort(key=lambda x: x[0])
                out.extend(t for _y, t in items)
    except Exception:
        return ""
    return "\n".join(out)


def fingerprint(fields, md5hex):
    keys = [("exact", "md5|%s" % md5hex)] if md5hex else []
    if fields.get("no") and fields.get("amount_cents"):
        keys.append(("high", "no+amt|%s|%s" % (fields["no"], fields["amount_cents"])))
    if fields.get("no"):
        if fields.get("code"):
            keys.append(("high", "code+no|%s|%s" % (fields["code"], fields["no"])))
        else:
            keys.append(("mid", "no|%s" % fields["no"]))
    if fields.get("seller") and fields.get("amount_cents") and fields.get("date"):
        keys.append(("low", "sel+amt+date|%s|%s|%s" % (
            _strip_ws(fields["seller"]).lower(), fields["amount_cents"], fields["date"])))
    return keys


def validate_categories(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 50:
        raise ValueError("分类数量应为 1-50 个")

    out, seen = [], set()
    for i, cat in enumerate(raw):
        cid = str(cat.get("id") or "").strip()
        label = str(cat.get("label") or "").strip()
        if not re.fullmatch(r"[a-z0-9_-]{1,40}", cid) or cid in seen or not label:
            raise ValueError("第 %d 个分类的标识或名称无效" % (i + 1))
        kws = [str(x).strip() for x in cat.get("keywords", []) if str(x).strip()]
        out.append({"id": cid, "label": label[:50],
                    "note": str(cat.get("note") or "").strip()[:120],
                    "keywords": list(dict.fromkeys(kws))[:100]})
        seen.add(cid)
    if "other" not in seen:
        raise ValueError("必须保留 other（其他/待分类）")
    return out
