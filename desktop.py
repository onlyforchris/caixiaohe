# -*- coding: utf-8 -*-
"""桌面壳 —— PyWebView 包裹本地 HTTP 服务。

双击启动后：
1. 后台启动 ThreadingHTTPServer
2. 弹出窗口加载本地页面
3. 系统托盘图标提供 显示/隐藏/退出
"""
import sys
import os
import threading
import webview
from PIL import Image

from financekit.paths import APP_VERSION, ICON, ensure_user_dir
from financekit.invoice import main as server_main

_window = None
_server_port = 18899


def _start_server(port):
    sys.argv = ["app.py", "--port", str(port), "--no-browser"]
    server_main()


def _get_icon():
    if os.path.exists(ICON):
        return Image.open(ICON)
    img = Image.new('RGB', (64, 64), color=(37, 99, 235))
    return img


def _create_tray(window):
    import pystray

    icon = _get_icon()

    def on_show():
        window.show()
        window.restore()

    def on_hide():
        window.hide()

    def on_quit():
        icon.stop()
        window.destroy()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("显示窗口", on_show, default=True),
        pystray.MenuItem("隐藏窗口", on_hide),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", on_quit),
    )

    return pystray.Icon("财小盒", icon, f"财小盒 v{APP_VERSION}", menu)


def main():
    global _window

    ensure_user_dir()

    server_thread = threading.Thread(target=_start_server, args=(_server_port,), daemon=True)
    server_thread.start()

    url = f"http://127.0.0.1:{_server_port}"
    _window = webview.create_window(
        f"财小盒 v{APP_VERSION}",
        url,
        width=1200,
        height=800,
        min_size=(900, 600),
        resizable=True,
    )

    def run_tray():
        tray = _create_tray(_window)
        tray.run()

    tray_thread = threading.Thread(target=run_tray, daemon=True)
    tray_thread.start()

    webview.start(debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
