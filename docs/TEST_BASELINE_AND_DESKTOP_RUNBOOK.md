# FishTool 测试执行口径 · 全量双口径 + 桌面单跑 Runbook

> 本文是 **主全量桌面口径的唯一写死处**（第四批 f 收口）。
> 版本：R5-4f｜2026-10-03｜依据 `FishTool_04_R5执行规格_第四批f_三点收口.md` §一 / §五。

---

## 1. 全量双口径（A / B）

`pytest.ini` 限定 `testpaths = tests`，以下命令均**在仓库根目录**执行。

### 口径 A —— 排除 3 个桌面模块

```bat
:: 注意：cmd 里必须用 set "VAR=value" 的**带引号**写法，见 §3 根因
set "QT_QPA_PLATFORM=offscreen"
python -m pytest -q ^
  --ignore=tests/test_desktop_pet_ui.py ^
  --ignore=tests/test_desktop_websocket.py ^
  --ignore=tests/test_desktop_webengine_isolation.py
```

**排除的 3 个桌面模块与原因：**

| 文件 | 排除原因 |
|---|---|
| `tests/test_desktop_pet_ui.py` | 需 `QT_QPA_PLATFORM=offscreen` 且 `desktop.*` 模块级 import（早于任何 `QApplication`）；历史上曾因**环境变量被污染**导致解释器级崩溃 `0xC0000409` |
| `tests/test_desktop_websocket.py` | 桌面 WebSocket 链路，依赖桌面运行时装配 |
| `tests/test_desktop_webengine_isolation.py` | 其 `construct-*` 子进程**故意**触发 `0xC0000005`（访问违规），只验证「崩在子进程、父进程存活」 |

> 口径 A 用于「与桌面 Qt 运行时解耦」的稳定基线。

### 口径 B —— 含桌面（全量）

```bat
set "QT_QPA_PLATFORM=offscreen"
python -m pytest -q
```

**现状（R5-4f 实测）**：口径 B 与口径 A 均**全绿**，桌面 3 模块在 offscreen 下正常通过
（`test_desktop_pet_ui.py` 8 passed；`test_desktop_webengine_isolation.py` 的 `construct-*`
访问违规被 **subprocess 隔离**在子进程内，父进程照常通过）。因此桌面**不需要**强制 `skip`。

> **基线最终裁定（2026-10-03 CST，叉子拍板）：统一取口径 B（含桌面）。**
> 理由：桌面崩点已根治为绿，A / B 分歧的前提消失，不再保留双口径。
> **当前基线：`2297 passed / 2 skipped / 0 failed`**（R5-4e 收口实测，15:55，单跑无并发，323.11s）。
> 基线随批次递增（4b = 2257 → 4f = 2283 → 4e = 2297），门槛取「上一批基线 + 本批新增用例数」。
> 2 skipped = `test_bilibili_contract_live.py`（live 默认跳）+ `test_discovery_smoke.py`（skipif），**既有开关门，与本批无关**。

---

## 2. 桌面单跑命令（写死）

单跑 `test_desktop_pet_ui.py`：

```bat
:: cmd —— 关键：set 带引号，避免尾空格污染 QT_QPA_PLATFORM
set "QT_QPA_PLATFORM=offscreen"
python -X faulthandler -m pytest tests/test_desktop_pet_ui.py -v
```

PowerShell：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -X faulthandler -m pytest tests/test_desktop_pet_ui.py -v
```

**前置硬约束**（文件头 docstring 自陈）：
1. 必须 `QT_QPA_PLATFORM=offscreen`；
2. `desktop.*` 必须**模块级 import**（早于任何 `QApplication` 创建）；
3. 依赖 `QtWebEngineWidgets` 在 `QApplication` 之前 import。

---

## 3. 桌面崩点根因与处置（一次说清，不许含糊）

**最终状态：绿（green）。**

- **崩点**：`python -X faulthandler -m pytest tests/test_desktop_pet_ui.py` 在**第一个用例**
  （`test_pet_asset_roots_are_unique_and_include_repo_assets`）执行前，进程即退出
  `rc=3221226505 = 0xC0000409`（`STATUS_STACK_BUFFER_OVERRUN`），无任何 Python traceback。
- **真根因**：**不是**缺 PyQt5，**也不是**「全量顺序污染」。
  是运行命令里 `set QT_QPA_PLATFORM=offscreen && ...` 的经典 cmd 陷阱 —— `&` 前的**尾空格**
  被并入变量值，`QT_QPA_PLATFORM` 实际为 `"offscreen "`（带尾空格）；Qt 解析平台插件名失败
  → 进程 `qFatal/abort` → `0xC0000409`。**解释器级崩溃**，`try/except` 拦不住。
- **受控实验（唯一变量 = 尾空格）**：
  - `QT_QPA_PLATFORM="offscreen "` → `0xC0000409`（崩）
  - `QT_QPA_PLATFORM="offscreen"` → `8 passed`
- **污染源排查**：全仓仅 `test_desktop_pet_ui.py` / `_desktop_webengine_child.py` 构造
  `QApplication`；后者是子进程探针。**不存在**「前面某测试先建 QApplication」的顺序污染。
- **处置（首选：改测试文件内部根治，不删测试 / 不吞异常 / 不改 `desktop/` 生产代码）**：
  1. `tests/test_desktop_pet_ui.py` 文件头加**环境自愈门**：import 期把 `QT_QPA_PLATFORM`
     规整为合法的 `offscreen`（污染值打印中文告警后覆写）；
  2. `tests/conftest.py` 把 `QT_QPA_PLATFORM` 的 `setdefault` 换成**去空白 + 空值回退**
     （覆盖所有桌面测试，杜绝同类崩溃带走全量）。
- **验证**：在**故意污染**的环境（`set "QT_QPA_PLATFORM=offscreen "`，带尾空格）下重跑
  `tests/test_desktop_pet_ui.py` → **8 passed**（修改前同条件必崩）；正确环境下 → 8 passed。
