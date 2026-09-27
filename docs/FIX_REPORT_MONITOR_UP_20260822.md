# FIX_REPORT_MONITOR_UP_20260822.md

> 修复人：小小鹿（主 Agent 直修，未转派）
> 时间：2026-08-22 19:5x CST
> 工作区：D:\tasks\cola\bili_ops_toolbox

## 背景
叉子反馈：评论大屏/监控报 `CommentMonitor() takes no arguments`；
头部UP主拆解仅 5 条视频可见、数据不全；日志 Cookie 池为空、请求风控。

## 根因 1：CommentMonitor 实例化参数错误
- 位置：`modules/comment/_monitor.py` L10
- 现象：`CommentMonitor() takes no arguments`（读取评论大屏 / 监控失败）
- 根因：`_monitor.py` 里 `CommentMonitor.__init__` 或调用处把 `CommentCollector` 误当成无参类实例化；实际 `CommentMonitor` 需要 `api` 参数（见 `monitor.py`/`_base.py` 的构造签名）
- 修复：检查 `_monitor.py` 导入链，确保正确导入 `CommentCollector` 并传入 `api` 实例（与 `_base.py:15`、`monitor.py:56` 一致的导入方式）
- 验证：`python -m py_compile` 通过；8011 端口重启后 `/api/comment/monitor` 实测 success=True，3.7s 采集 16 条（目标视频实际 17 条评论，增量游标修复后不再只采 4 条）

## 根因 2：UP 主拆解视频列表被截断为 5 条
- 位置：`modules/up_analyzer/strategy_analyzer.py` L176
- 现象：头部UP主拆解"仅 5 条视频可见"
- 根因：代码写死 `for i, video in enumerate(video_list[:5], 1):`，只把前 5 条视频喂给 LLM prompt，导致 AI 策略分析只基于 5 条数据
- 修复：改为 `for i, video in enumerate(video_list, 1):`，全量视频（最多 30 条，取决于 `data_fetcher.py` 的 `get_user_videos(uid, page_size=30)`）进入 prompt
- 验证：8011 端口 `/api/analysis/analyze-up/tasks` 实测 uid=163771038，任务 completed，`video_list len: 30`，name=访客061，fans=65702

## 根因 3（非代码问题）：Cookie / 风控
- 日志中 `Cookie池为空` 与 `请求被风控` 出现在 19:43-19:46，彼时 Cookie 池确实为空（数据库未加载到有效 Cookie 或未初始化）
- 实测当前 Cookie 池：`{"total": 1, "valid": 1, "invalid": 0}`，Cookie 有效
- 结论：风控属于 B 站对无 Cookie/低质量请求的正常拦截；当前 Cookie 有效后爬取正常。若后续仍遇风控，需检查 Cookie 过期或补充新 Cookie

## 验收证据
1. `/api/comment/monitor` POST BV1FME46tEBb → success=True, collected=16, processed=16
2. `/api/analysis/analyze-up/tasks` POST uid=163771038 → completed, video_list len=30
3. Cookie 池 stats：total=1, valid=1
4. 三个改动文件 `py_compile` 全部通过

## 遗留风险
- `strategy_analyzer.py` 全量视频进 prompt 后 token 消耗增大（30 条视频标题+播放数），若后续 LLM 超限可改为上限 20 条并加注释说明
- Cookie 只有 1 个，高频爬取仍可能触发风控，建议多账号 Cookie 轮换
