# 抽奖工具交付说明

## 交付范围

新增左侧 `🎁 抽奖工具`，位于日志上方，日志仍为最后一项。页面包含评论区真人批量筛选、单 UID 快速筛选、可配置 AI 侧重点模板、视频/动态随机抽奖、目标预览确认、长任务进度和结构化结果卡片。

后端按项目结构新增：

- `modules/lottery/target.py`：BV/动态完整链接解析，标题、发布者与评论区标识获取。
- `modules/lottery/service.py`：本地数据优先复用、现有视频评论采集器回退、动态低频分页采集、用户画像和安全随机抽取。
- `modules/lottery/analyzer.py`：启发式兜底、受约束 LLM JSON 判定、可配置侧重点注入。
- `web/routers/lottery.py`：预览、UID 快筛、筛选任务、抽奖任务与进度查询。
- `web/main.py`：以 `/api` 前缀挂载抽奖 router。

## 核心行为

1. 抽奖前调用 `/api/lottery/preview`，展示标题与发布者；只有确认弹窗选择“确认开始”后才创建任务。
2. 视频评论先查 SQLite 的 `videos/comments`；动态评论先查 `data/lottery_cache`。未命中时，视频复用 `CommentCollector.STRATEGY_FULL`，动态使用 B 站公开评论接口串行分页。
3. 真人筛选采集等级、关系统计、投稿数、近期公开动态、抽奖转发占比和可观察活动跨度，并按 UID 去重。
4. 自定义提示词只能调整判定侧重点。系统提示强制模型仅使用 `DATA_JSON`，要求严格 JSON 输出；结果校验失败或 LLM 未配置时使用可解释规则兜底。
5. 随机抽奖使用 `secrets.SystemRandom().sample`；默认同一 UID 仅一次机会，可在页面关闭去重。
6. 所有耗时任务展示本地检索、评论采集、账号画像、AI 判断等阶段文案、加载圈、进度条和预计时间。

## 数据边界

B站公开接口通常不提供可靠的精确账号注册时间。本功能不会猜测注册日期，使用“近期公开样本中最早可观察动态距今天数”作为辅助信号，并在返回字段 `account_age_note` 中明确说明。主页动态不可访问或触发风控时，会写入 `data_errors`，判定不会把缺失数据伪装成事实。

## API

- `POST /api/lottery/preview`：`{"target":"BV号或动态完整链接"}`
- `POST /api/lottery/quick-filter`：`{"uid":123,"focus_template":"可选"}`
- `POST /api/lottery/filter/tasks`：创建真人筛选任务
- `POST /api/lottery/draw/tasks`：创建随机抽奖任务
- `GET /api/lottery/tasks/{task_id}`：读取任务阶段、进度、预计时间与结果

## 验证结果

- Python 语法检查：通过。
- JavaScript 语法检查：通过。
- 新增单元测试：`4 passed`。
- 全项目当前 pytest 收集：`4 passed`。
- Import/依赖扫描：80 个 Python 文件，无循环依赖。
- 前端静态契约：抽奖导航位于日志前；页面结果容器、确认函数、两类后台任务接口均存在。
- UI 契约：继续使用 `--bg-primary: #faf7f2`、`--primary-color: #d4a373`；新增结果卡光晕 `pointer-events: none`，坐标更新复用 `requestAnimationFrame` 节流。
- Playwright 运行态验收：桌面 `1440x1000` 与移动 `390x844` 均无横向溢出；光晕坐标分别更新为 `542px`、`216px`；OpenAPI 中 5 个抽奖路由全部存在。
- 运行态证据：`evidence/lottery/lottery_desktop.png`、`lottery_mobile.png`、`verification.json`。

## 验收方法

1. 启动：在项目根目录运行 `python start_web.py`，打开 `http://127.0.0.1:8000/`。
2. 导航：确认 `🎁抽奖工具` 位于日志上方，日志保持侧栏最底层。
3. 目标防错：输入 BV 或动态完整链接，点击“核对内容并开始”，核对标题、发布者和确认弹窗；取消时不得创建抽奖任务。
4. 本地复用：先用评论监控采集一个 BV，再在抽奖工具使用同一 BV；结果来源应显示“本地数据复用”。
5. 爬虫回退：使用本地没有记录的内容；进度应先显示本地检索，再显示低频评论采集。
6. 真人批筛：观察进度依次经过评论准备、账号画像、AI 判断；结果应分为真人候选和疑似抽奖号，并展示理由、置信度和数据来源。
7. UID 快筛：修改侧重点模板后输入 UID；确认结果中的 UID 与输入一致，AI 不应输出传入画像之外的事实。
8. 抽奖去重：默认开启 UID 去重，中奖者 UID 不重复；关闭后按评论记录作为候选项。
9. 视觉：桌面与窄屏检查无文字溢出；移动鼠标时卡片光晕跟手且不拦截点击。

## 运行要求

无需新增第三方依赖。真人筛选的 AI 增强需要在现有“配置”页设置 LLM API Key、Base URL 和模型；未配置时仍可使用规则兜底。读取受限用户主页和动态评论建议保持有效 B 站扫码登录态。
