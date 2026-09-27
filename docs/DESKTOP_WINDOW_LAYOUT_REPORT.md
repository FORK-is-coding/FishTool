# 桌面端窗口版式修复报告

## 结论

桌面端实际使用的是 `PyQt5.QWebEngineView`，不是 `pywebview`。原窗口在 `desktop/main_window.py` 中通过 `setGeometry(100, 100, 1200, 800)` 创建，未设置最小尺寸，也未显式设置 QWebEngine 缩放或 Qt 高 DPI 初始化。

已将桌面端启动窗口调整为：

- 初始尺寸：`1440 x 900`
- 初始比例：`8:5`（`1.6`）
- 最小尺寸：`1200 x 750`
- WebEngine CSS 缩放：`1.0`
- Qt Windows 高 DPI：在创建 `QApplication` 前启用 `AA_EnableHighDpiScaling` 与 `AA_UseHighDpiPixmaps`

## 版式依据

前端页面位于 `web/frontend/templates/index.html`，样式位于 `web/frontend/static/css/style.css`。

页面是固定左侧导航、右侧内容区的桌面布局：

- 侧栏固定宽度：`240px`
- 主内容区左右内边距：各 `40px`
- 默认页面使用多列网格，例如指标区五列、图表区双列、欢迎页卡片按最小 `280px` 自适应
- `1100px`、`900px`、`768px` 等断点逐步减少列数，`768px` 以下侧栏收窄为图标栏

原始 `1200px` 宽窗口扣除侧栏和两侧内边距后，主内容有效宽度约为 `880px`，容易让桌面网格处于空间不足但尚未完全进入窄屏断点的状态。调整为 `1440px` 后，有效内容宽度约为 `1120px`，能够保持完整桌面网格；`900px` 高度也与常见浏览器工作区比例一致，并为纵向内容提供足够空间。

最小尺寸 `1200 x 750` 与初始尺寸保持相同比例，防止用户缩小窗口后有效内容区进一步挤压。该限制不会改变登录态、导航、Web 服务启动或各功能页逻辑。

## 修改文件

1. `desktop/main_window.py`
   - 增加初始尺寸和最小尺寸常量。
   - 使用 `setMinimumSize` 阻止窗口缩小到容易挤压布局的尺寸。
   - 将窗口初始尺寸改为 `1440 x 900`。
   - 显式设置 `QWebEngineView.setZoomFactor(1.0)`，避免桌面端产生额外 CSS 缩放。
2. `start_desktop.py`
   - 在创建 `QApplication` 前启用 Qt 高 DPI 缩放和高 DPI 图标支持。
3. `docs/DESKTOP_WINDOW_LAYOUT_REPORT.md`
   - 本报告。

## 验证方式

### 已完成

- 两个修改后的 Python 文件已通过语法检查。
- `python -m pytest test_lottery_tool.py -q`：`10 passed`，`1 warning`。
- 静态确认窗口初始尺寸、最小尺寸、WebEngine 缩放和 DPI 属性均位于正确初始化路径。
- 修改只涉及桌面壳窗口初始化，没有修改 Web API、登录态、导航或功能页逻辑。

### 发布前验证

当前 `dist/BiliOpsToolbox.exe` 仍是修改前的构建产物。由于本轮环境无法记录覆盖 exe 的审批，未直接替换该文件。发布时应在项目根目录执行项目既有 PyInstaller 配置对应的构建命令，例如：

```bash
python -m PyInstaller --clean --noconfirm build.spec
```

需要重新打包生成 `dist/BiliOpsToolbox.exe`，再分别打开：

1. 浏览器访问 `http://localhost:8000`。
2. 启动新生成的 `dist/BiliOpsToolbox.exe`。
3. 对比欢迎页、配置页、热点/评论页、账号自诊页和抽奖页的首屏宽度、侧栏宽度、卡片列数与字体大小。
4. 尝试将 exe 窗口缩小到低于 `1200 x 750`，确认窗口不再继续缩小。
5. 在 Windows 显示缩放为 `100%`、`125%` 时分别检查字体和控件是否发生异常放大、截断或重排。
6. 验证已有登录态、页面导航、Web 服务健康检查和桌宠窗口仍能正常工作。

现有 `dist/BiliOpsToolbox.exe` 是修改前的构建产物；仅修改源码不会自动更新该文件，必须按上述流程重新构建后再进行 exe 截图对比。
