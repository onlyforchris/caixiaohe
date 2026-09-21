# -*- coding: utf-8 -*-
"""发票管家入口 —— 兼容旧 import app / 双击启动。

业务逻辑在 financekit 包；本文件再导出符号供 test_core.py 使用。
"""
from financekit.paths import *  # noqa: F401,F403
from financekit.store import (  # noqa: F401
    list_backups, load_ledger, load_used_ledger, restore_ledger, save_ledger,
    save_used_ledger, used_item_key, used_ledger_add,
)
from financekit.invoice import *  # noqa: F401,F403
from financekit.invoice import _companies, _itinerary_fields, _stem_core  # noqa: F401  # test_core 直接断言用

if __name__ == "__main__":
    import sys
    from financekit.paths import ensure_user_dir
    ensure_user_dir()
    sys.exit(main())
