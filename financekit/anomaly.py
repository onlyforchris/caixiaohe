# -*- coding: utf-8 -*-
"""业务合理性校验——补充 validate_fields 的结构校验。

validate_fields 检查"数据是否自洽"（号码位数、金额加减、日期范围），
本模块检查"数据是否合理"（金额与分类是否匹配、销售方名称是否像公司名）。

宁可少报也不误报：缺字段就跳过，不猜测。
"""
import re

# 各分类的常见金额上限（分）。超过此值提示核对，不是硬限制。
# 阈值目的：抓 OCR 把 35.00 识别成 3500.00 这类小数点偏移错误。
AMOUNT_UPPER = {
    "transport": 200000,
    "travel": 1000000,
    "office": 500000,
    "communication": 1000000,
    "hospitality": 3000000,
    "meeting": 5000000,
    "training": 2000000,
    "advertising": 5000000,
    "service": 5000000,
    "rd": 10000000,
    "property": 20000000,
    "benefit": 500000,
    "other": 20000000,
}

# 明显不像公司名的模式
_SELLER_GARBAGE = re.compile(
    r"^[\d\s.\-]+$"
    r"|^https?://"
    r"|^(支付宝|微信|财付通)$"
)


def check_amount_range(amount_cents, cat_id):
    """金额与分类是否匹配。返回问题描述或 None。"""
    if amount_cents is None or not cat_id or cat_id == "other":
        return None
    upper = AMOUNT_UPPER.get(cat_id)
    if upper and amount_cents > upper:
        return "金额偏高（该分类常见不超过 %.0f 元，请核对是否多识别了零）" % (upper / 100)
    return None


def check_seller_name(seller):
    """销售方名称是否像合法公司名。返回问题描述或 None。"""
    if not seller:
        return None
    s = seller.strip()
    if len(s) < 2:
        return "销售方名称过短，可能识别有误"
    if _SELLER_GARBAGE.match(s):
        return "销售方名称不像公司名称（%s），请核对票面" % s
    return None


def check_anomaly(rec):
    """综合业务合理性检查。返回问题列表（空列表表示无异常）。"""
    issues = []
    amt_issue = check_amount_range(rec.get("amount_cents"), rec.get("cat_id"))
    if amt_issue:
        issues.append(amt_issue)
    seller_issue = check_seller_name(rec.get("seller"))
    if seller_issue:
        issues.append(seller_issue)
    return issues
