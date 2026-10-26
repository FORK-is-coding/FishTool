# 异常处理规范

> 目标：任何异常都必须留下可追溯的信息；低优先级错误只写日志，高优先级错误在写日志之外
> 再向用户弹一次 toast。**禁止裸 `except:`，禁止 `except Exception: pass`。**

## 1. 分级口径

| 级别 | 判定标准 | 处理方式 | 典型场景 |
|---|---|---|---|
| **LOW** | 可重试、可降级，或失败只影响单条数据 | `logger.warning` / `logger.debug`，需要时带堆栈；**不弹 toast** | 单条解析失败、旧库缺列降级、单页采集失败、清理临时文件失败 |
| **HIGH** | 用户当前操作无法完成，或需要用户介入 | `logger.error(..., exc_info=True)` **且** `emit_toast(...)` | 登录态失效、配置读写失败、数据库提交失败、权限不足、导出失败、后台监控连续失败 |

不确定时按 **LOW** 处理并写 `warning`，不要为了「安静」而吞掉异常。

## 2. 代码写法

### 2.1 统一入口：`core.error_policy`

```python
from core.error_policy import ErrorPriority, report_exception

try:
    await save_config()
except Exception as exc:
    report_exception(
        exc,
        logger=logger,
        action="保存配置",
        priority=ErrorPriority.HIGH,
        context={"path": config_path},
        source="web",
    )
    raise
```

- `priority` 不传时按异常类型推断：`AuthenticationError` / `ConfigError` /
  `DatabaseError` / `PermissionDeniedError` / `TokenLimitExceededError` /
  `LLMNotConfiguredError` / `TaskError` / `IPBannedError` / `AccountRiskError` /
  `CaptchaRequiredError` 视为 HIGH，其余默认 LOW；`RecordNotFoundError` 虽然是
  `DatabaseError` 子类，但属于常见 404 分支，显式按 LOW 处理。
- `include_traceback=False` 只用于「预期内且高频」的降噪；非预期异常不要关。
- `context` 只放排查必需字段（`bvid` / `uid` / `path`），**不要放 Cookie、Token、密钥**。
- 高优先级但不想弹窗（如测试、纯后台）传 `notify=False`。

便捷入口：

```python
log_low_priority(exc, logger=logger, action="解析评论时间")
report_high_priority(exc, logger=logger, action="保存配置", source="web")
```

### 2.2 只有日志、不需要 toast 时

```python
except (ValueError, TypeError) as exc:
    logger.debug("时间戳格式非法，按无时间处理: %r (%s)", raw, exc)
    value = None
```

- **不要**只写 `str(exc)` 就结束：至少保留异常消息；非预期宽泛异常要带 `exc_info=True`。
- 高频解析路径用 `debug`，不要用 `error` 刷屏。

### 2.3 禁止的写法

```python
except:                      # 禁止：裸 except
    pass

except Exception:            # 禁止：宽泛捕获后什么都不做
    return {}

except Exception as exc:     # 不推荐：只打一句无堆栈的 error
    logger.error("失败")      # 丢失 exc 与堆栈
```

宽泛捕获后如果确实要降级，必须满足其一：

1. 写日志（含异常消息，必要时 `exc_info=True`）；
2. `raise` 重新抛出；
3. 用窄异常类型表达预期分支（如 `except FileNotFoundError`）。

## 3. Toast 链路

`core.toast` 是进程内总线，业务层只调用 `emit_toast`，不关心 UI：

```python
from core.toast import emit_toast

emit_toast(
    "评论监控异常",
    "评论常驻采集已连续失败 3 次，正在自动退避重试。",
    level="error",
    source="monitor",
    error_code="comment_monitor_consecutive_failures",
)
```

投递路径：

- **Web**：`web/main.py` 的 lifespan 注册 sink，把 toast 广播为
  `{"type": "toast", "data": {...}}` 到 `/ws`；前端 `app.ui.js` 的 `showToast()`
  在右上角展示，自动消失，可在 `showToast(message, {title, level, duration})` 中覆盖。
- **桌面**：桌宠 WebSocket 收到 `type=toast` 后复用通知气泡展示；主窗口的
  `show_toast()` 走系统托盘气泡，用于桌面壳自身的启动/数据库错误。
- **无 UI 环境**（脚本、测试）：没有 sink 时只记日志，`emit_toast` 返回 0，不抛异常。

约定：

- sink 可能在任意线程被调用，必须线程安全；sink 自身异常会被吞掉并记录，不影响业务；
- 同一「标题 + 正文 + 级别」默认 2 秒内去重，避免轮询失败刷屏；
- API 返回 5xx 时，`web/main.py` 统一补一条 toast；前端 `apiRequest` 同样按 toast 展示
  5xx（同一文案按文本去重），toast 脚本缺失时回退到原有模态提示；4xx 属于用户输入问题，
  仍走页面内既有提示，不重复打扰。

## 4. 分层职责

| 层 | 职责 |
|---|---|
| 业务 / 采集 / 存储 | 捕获后记录上下文；决定 LOW/HIGH；该抛则抛 |
| `core.error_policy` | 统一日志格式、优先级推断、HIGH 时投递 toast |
| `core.toast` | 进程内 sink 注册/注销、去重、故障隔离 |
| `web/main.py` | HTTPException 5xx 与未处理异常兜底；`/ws` 广播 |
| 前端 / 桌面 | 只负责展示 toast，不反向影响业务逻辑 |

## 5. 检查与回归

```bash
# 静态检查（CI 可直接用退出码）
python tools/check_exception_handling.py

# 回归测试
python -m pytest tests/test_error_policy.py -q
```

`tools/check_exception_handling.py` 会检查：

1. 裸 `except:`；
2. 宽泛 `except Exception` 既不写日志也不重新抛出；
3. 任何 `except` 只写 `pass`（`asyncio.CancelledError` / `asyncio.TimeoutError`
   等控制流例外除外）。

`tests/`、`tools/` 目录不在检查范围内：测试替身与 CLI 脚本的失败信息通过断言 /
报告输出体现，不要求写业务日志。
