# 财小盒

Windows 桌面端财务小工具合集。所有数据留在本机，不上传任何第三方。

## 包含工具

| 工具 | 说明 | 状态 |
|------|------|------|
| **发票管家** | 拍票、查重、归档，一站搞定。支持 PDF / OFD / JPG / PNG，按内容比对查重，可选智谱 AI OCR，按关键词自动归类费用分类 | 可用 |
| **现金流预测** | 录入收支后自动推演未来 12 个月余额，标出资金紧张月份，支持导入 CSV | 可用 |
| 费用看板 | 各类开支一目了然 | 规划中 |
| 报销批次 | 整理报销单，一键生成汇总表 | 规划中 |

## 下载安装（普通用户）

1. 从 [GitHub Releases](https://github.com/onlyforchris/caixiaohe/releases) 或 [Gitee Releases](https://gitee.com/onlyforchris/caixiaohe/releases) 下载 `财小盒-vX.X.X-Setup.exe`
2. 双击运行，按提示完成安装
3. 双击桌面「财小盒」图标即可使用

不需要安装 Python，不需要命令行。

## 升级

从 [GitHub Releases](https://github.com/onlyforchris/caixiaohe/releases) 或 [Gitee Releases](https://gitee.com/onlyforchris/caixiaohe/releases) 下载新版 Setup.exe，双击覆盖安装即可。用户数据（配置、台账）不会丢失。

## 卸载

Windows 设置 → 应用 → 找到「财小盒」→ 卸载。或在开始菜单中点击「卸载 财小盒」。

卸载后用户数据保留在 `C:\Users\你的用户名\财小盒\`，如需彻底清除请手动删除该目录。

## 开发者

**构建安装包：**

```powershell
pip install pyinstaller pywebview pystray pillow
python build.py
```

生成的 `dist/财小盒-vX.X.X-Setup.exe` 即为可分发的安装包。

构建安装包需要 [Inno Setup 6](https://jrsoftware.org/isdl.php)（免费）。如果只构建 exe 不打包：`python build.py --exe-only`。

**源码模式运行（开发调试）：**

```powershell
pip install -r requirements.txt
python test_core.py
python desktop.py
```

源码模式下用户数据存放在项目根目录（与源码同级）。

## 注意

- 分类是财务复核建议，不代表允许报销、税前扣除或进项抵扣。
- 开启 AI OCR 后，相关票据页面会发送至智谱服务。
- 请勿在 Issue 中上传真实发票、API Key、税号、公司内部路径或个人信息。

MIT License。版本变化见 [CHANGELOG](CHANGELOG.md)。
