# Changelog

本项目所有值得记录的变更都写在这里。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> **v0.2.2 之前未逐条记录。** 更早版本只保留 tag：
> v0.1.0 / v0.2.0 / v0.2.1

## [未发布]

### 桌宠多屏异构 DPI 拖动修复（#137）

- 两块屏缩放不同（如主屏 100% + 副屏 150%）时，跨屏拖动桌宠不再跳到另一块屏或与鼠标脱节；拖动目标改按物理像素和目标所在屏换算，避开屏幕之间的逻辑坐标空隙。
- 跨屏后窗口尺寸不再累积放大或缩小（原来反复跨界会 150 → 225 → 338 …），每次移动后恢复为 150×150。
- 松手保存和启动恢复位置前先检查可见性：窗口几乎不可见时拉回最近的屏幕，避免坏坐标写入配置后重启仍然找不到桌宠。半挂在屏幕边缘、仍露出一部分的位置保持不变。
- 所有屏幕缩放相同时，拖动行为与原来一致。
- 已知限制：跨界那一帧仍可能出现一次短暂的大小变化。

### 用量统计、评论入库与指纹计算提速（#136）

- 每日 token 限额预检、模型用量统计和配置页「今日已用」改为在 SQL 内求和，不再逐条读出当日记录；日期区间口径不变。旧库存在 NULL / 非整数值或求和超出 64 位时，仍按原来的逐行方式计算。
- 评论入库按每 500 条一次批量查询已有 rpid，替代逐条查询；字段补齐、批内去重、增量断点和失败回滚行为不变。
- SimHash 合并相同字符的贡献，并在进程内缓存最多 4096 个单字符的位贡献（约 1MB，不缓存整条评论）；指纹结果与原实现一致，评论去重与 AI 伪人巡检共用。
- 新增新旧实现对照回归：完整入库结果、持久化字段、指纹、日期边界、异常值与溢出回退。

### 抽奖视频评论采集方式：常规 / 楼中楼（#133）

- 真人筛选和评论区抽奖新增「视频评论采集方式」选择：
  - 常规（默认，与原行为一致）：公开评论接口，任意视频可用，复用 10 分钟内的完整快照。
  - 楼中楼（仅限本人视频）：UP 主评论管理接口 `x/v2/reply/up/fulllist`，包含全部楼中楼；需登录视频发布者账号，每次实时完整采集；评论总数变化、分页缺失或混入其他内容的评论时直接报错。
- 两个任务接口新增可选字段 `comment_mode`（`standard` / `up_fulllist`，默认 `standard`），非法取值返回 422；任务去重指纹包含该字段。动态目标忽略该字段。
- 楼中楼模式与常规模式共用评论采集器的限频（约 4 秒一次），进度里显示预计剩余时间。
- 选择栏在输入动态链接时置灰；确认弹窗、加载提示和结果来源标签按所选方式显示。

### 抽奖工具：AI 伪人巡检

- 新增子功能「AI 伪人巡检」：输入 BV 号扫描评论区（可选楼中楼），每条评论显示伪人率与命中规则。内置规则覆盖「大肥鱼 / 肥鱼 / deepseek / 吃白饭 / 养鱼」等昵称与内容、自称本鲸等自我暴露，以及「这句话稳」「这个账我记住了」「这个我认」等 AI 惯用短句腔调。
- 可选 AI 甄别：首次使用前说明发送内容并预估 Token 用量，规则与 AI 结论综合计分，差异大的标「分歧」；结果缓存 7 天。
- 举报：可在 B站页面手动提交，也可由 FishTool 代为提交（默认开启，首次需同意风险说明，每次提交前确认；同账号只报一条代表评论，单次 5 条、24 小时 10 条，举报过于频繁时自动暂停 30 分钟）。举报台账与 CSV 导出保存在本机。
- 桌面版点击外部链接改为用系统浏览器打开，修复「打开B站主页」与巡检举报链接点不开。
- 事件工作台 CreatorBrief 折叠区标题去掉内部编号。
- AstrBot 插件新增后台开关 `engine.allow_proxy_report`（默认关闭），打开并重启引擎后插件版才允许代发举报。

### 配置、Web 路由与运行时一批修复（#132）

- 配置：
  - 配置文件改为临时文件加原子替换写入；YAML 损坏时备份后回退默认值，启动不再崩溃（#124）。
  - `.key` 损坏或与 `.secrets` 不配套时，旧的 `.secrets` 会先备份成 `.secrets.bad-*`，换回原密钥后可以恢复登录态。`.key`/`.secrets` 只是暂时读不出（被占用、权限不足）时照常报错，不当作损坏处理，也不会覆盖原文件。
  - 历史遗留的明文 `llm.api_key` 迁入加密存储，并从它所在的 yaml 文件删除；读取配置的接口只返回脱敏串（#88）。
  - 单键更新逐段去空白，校验、写入和是否重建大模型客户端都用规范化后的键；带空格的 `llm.*` 键也会触发重建（#98）。
  - 只换 API Key 时不再把已保存的 Base URL 和模型重置成默认值（#91）。
  - 配置页加载时不再发送空的 API Key，能直接用已保存的密钥刷新模型列表（#87）。
- 模型列表探测：
  - 只允许 http/https，拒绝 URL 内嵌账号密码、云元数据地址（含 IPv6 内嵌、非标准数字写法和域名解析结果）、link-local 与未指定地址，并且不跟随重定向（#81）。
  - 本地与局域网模型服务照常可用。
- Web：
  - 写请求和 `/ws` 的同源判定加入端口比较，本机其他端口的页面不再算同源（#57）；可用 `FISHTOOL_ALLOWED_PORTS` 放行。
  - 用量查询的 `days` 限定 1–365，数据库会话在 finally 中关闭；报告导出的 `format` 只接受 markdown/pdf（#94）。
  - 分析和词云后台任务保留强引用；创建新任务时清理已结束超过 1 小时的记录（#96）。
  - 日志先按级别过滤再截断，未知级别返回 422，`WARN` 不再被归成 `INFO`（#97、#128）。
- B 站请求：
  - 429 和 -509 重试耗尽后抛出限频异常；评论采集（包括取视频 oid、UP 主投稿列表）和词云取 tag 阶段遇到限频或本地熔断会中止报错，不再把部分结果当作完整结果（#84）。
  - 请求排队期间触发熔断时，排队中的请求醒来后会被拦下。WBI 密钥刷新失败时，同一波并发请求共用这次失败结果，不再各自重复请求。
  - 请求限流器的实际间隔改为等于配置值：之前约为配置值的一半，所以评论完整采集的耗时会比之前长（#103）。
  - 单 Cookie 滑动窗口的等待不再阻塞其他 Cookie。
  - WBI 密钥刷新加锁（#109）。
  - 扫码登录不再被采集任务留下的分域冷却误判为 Cookie 失效。
- 数据库：SQLite 开启外键约束，启动时检查历史孤儿行（只告警，不删除）；首次保存 Cookie 时补建本地账号（#82）。
- 大模型：上游错误原文不再进入接口返回的 `error` 字段（#108）。自诊遇到输入超长（`context_length_exceeded`）时不再白白重试一次（#105）。自诊 JSON 解析可以容忍前后多余的文字（#106）。
- 其他：
  - 监控服务正常退出不再打出错误堆栈（#100）。
  - 文件日志不再带 ANSI 色码（#125）。
  - AstrBot 插件：engine 段配置做类型和范围检查，开关项的 `"true"`/`"false"` 字符串按字面意思解析（#122）；订阅/退订落盘遇到任何异常（不止 OSError）都回滚并回复用户（#115）；PDF 排版失败给出明确提示（#117）。
  - release workflow 的触发路径覆盖插件内嵌的引擎文件（#127）。
  - STRUCTURE.md 与 README 日志路径更新（#123）。
  - `app.config.js` 本轮有改动，按约定由下一个版本号 PR 统一追加 `?v=` 后缀；`/static/` 已带 `no-cache` 响应头，旧缓存下的行为与改动前一致。

### 前端脚本缓存版本号补追加（#92）

- `app.config.js`、`app.hotspot.js`、`app.events.js`、`app.lottery.js`、`app.comment.semantic.js` 追加缓存后缀 `-fix20261107`。这几个脚本改过之后 `?v=` 没有跟着更新，用过旧版页面的浏览器或桌面端会继续用缓存里的旧脚本，例如配置页点“保存输出上限”报 `saveLLMMaxTokens is not defined`。

### 语义折叠：预计剩余时间修正（#90）与预检日志（#89）

- 运行中显示的“预计还需约 N 分钟”放大了约 1000 倍（剩 2 分钟显示约 2000 分钟），已改正。一次模型调用进行中，倒数不再在每次轮询后跳回。
- 预检读取历史调用耗时出错时，不再因为日志对象未定义而返回 500；开始前的耗时粗估改用固定区间。

### AI 回复：要求返回 JSON、识别截断、失败提示按原因区分

- AI 自诊此前写死了 1200 的输出上限，配置 `llm.max_tokens` 不生效；现在按配置执行（#75）。评论情感总结（原 500）、热点选题（原 2000）、UP 主策略分析（原 2000）同样改为按配置执行。抽奖真人筛选按人数计算上限、语义折叠按预算预留上限，这两处保持不变。
- AI 自诊请求带 `response_format: json_object`，要求服务端只返回 JSON。端点不支持这个参数（返回 400/422）时，自动去掉参数重试一次。
- 回复达到单次输出上限被截断（`finish_reason=length`）时，不再报“未返回 JSON”，而是提示“回复超过单次输出上限被截断”，并指向配置页的“单次输出上限”。模型返回了思考内容时，提示里会说明思考过程也计入上限。思考内容（`reasoning_content`）只记录长度，不当作回复解析。日志会写明截断时的上限、正文长度和思考内容长度。
- 截断后各功能的处理：AI 自诊显示截断提示；热点选题（标签与事件模式）照常降级，并提示调大上限；UP 主策略分析返回错误码 `output_truncated`；评论情感总结把该批记为失败，合并时跳过，不再把半截 JSON 当作原文保留。
- AI 自诊失败时按原因给出提示，不再一律提示“请先检查大模型配置”：未配置、API Key 无效（401/403）、模型不存在时让用户检查配置；限频（429）、其他接口错误、网络超时时提示稍后重试；模型没有返回 JSON 时提示重试或换模型。

### 配置页可调“单次输出上限”

- 配置页 AI 配置卡片新增“单次输出上限（token）”，对应 `llm.max_tokens`（默认 2000），单独保存，不需要重新填写 API Key。没有自带上限的 AI 功能（如 AI 自诊）按它限制每次回复的长度；回复被截断、提示“未按 JSON 返回”时可以调大。开启思考模式的模型，思考过程也计入这个上限。
- `PUT /api/config/` 写 `llm.max_tokens` 时只接受 1 到 100 万的整数，其他值返回 422。超过模型支持的最大值时由服务商报错，这里不按模型校验。
- 默认值不变。

### 语义预算模式补 flex gap 兜底

- `style.flexgap.css` 为 `.semantic-budget-mode`（14px）和 `.semantic-budget-option`（4px）补上 `.no-flex-gap` 兜底规则，`style.flexgap.css` 追加缓存后缀 `-flexgap20261105`。只在不支持 flex gap 的旧内核（Chromium 83 及以下）中生效。

### 版本号升到 v0.3.1

- 侧栏、欢迎页与开场动画中的可见版本号由 v0.3.0 改为 v0.3.1；`app.intro.js` 追加缓存后缀 `-v031`。
- 使用说明页免责声明第 1 条删去“v0.3.0 起由 MIT 改为 AGPL-3.0……”一句，与 README 保持一致。

### 前端静态资源版本号：语义折叠一轮

- `app.comment.semantic.js`、`app.comment.js`、`style.comment.semantic.css` 统一追加 `-budget20261104` 缓存后缀，确保打包版与已打开过页面的浏览器加载到带预算模式、停止报告与预估时间的新面板，而不是旧缓存。
- 本轮之前的语义折叠功能（预算档位、停止报告、预估时间）均为纯增量，无默认行为变化；此 PR 只改版本号与对应断言，不改逻辑。

### 语义折叠：显示预估时间

- 开始前显示粗估：预计调用次数与耗时区间。单次耗时取最近有实测数据的任务的中位数；没有历史数据时用固定区间，文案写明「粗估，取决于模型响应速度」。
- 运行中显示「已用时间 · 预计还需时间（进度）」，按实测的单次耗时计算，前端按秒倒数、随轮询校准。剩余调用数只含确定还要发生的分组调用，合并复核不可预知，所以显示为「至少」。
- 排队时显示排队说明，不显示剩余时间；前几次调用之前也不显示，避免数字乱跳。

### 语义折叠：停止时弹出报告，说明原因、进度与建议动作

- 任务因可续跑的原因停下（任务 / 每日 / 全局预算、限频、服务商临时故障或余额不足、执行失败、进程重启中断）时，弹窗写明：停止原因、总批次与调用次数（含重试）、跨批覆盖、本任务已用 token（标注是否含估算）、已产生的折叠组数，以及建议的下一步。
- 按钮按原因生成：「继续执行」、「稍后再说」，预算类停止另有「切换为无限制并继续」（走与开始时相同的二次确认，续跑时升级为无限制档）。
- 同一次停止只弹一次：按（任务、停止原因、停止序号）记在本机会话存储里，刷新后不重复弹；新的一次停止会重新弹。跨批轮数用完、批次降级、正常完成、用户取消、API Key 无效这类续跑也无进展的情况不弹窗。
- 页面在后台时不弹，回到前台再补；刷新页面后任务已停止也会提示。存储被禁用时仍能正常弹窗（仅在当前页面内去重，刷新后可能再弹一次，AstrBot 插件同样如此）。

### 语义折叠：可选“无限制”预算档位

- 开始分析前可选择预算档位：**默认档**（任务 6 万 token、每日 10 万、20 次调用，行为与之前一致）或**无限制档**（8000 万 token、3000 次调用，不检查也不占用语义每日预算；用量照常记进全局 AI 用量）。档位随任务保存，页面会记住上次的选择（仅本机）。
- 选择无限制档时，开始前要二次确认：写明待分析评论条数、粗估的调用次数 / token / 耗时、占用全局 AI 额度、可随时取消、已产生费用不退。
- 因预算停下的默认档任务，续跑时可单向升级为无限制档继续（同样要二次确认）；不允许降级（降级后已用量可能超限，任务会立刻再停）。升级前残留在飞的请求按原档位口径先结算。
- 无限制档跳过全局每日 AI 上限检查，照常记账；任务区“今日累计”只统计默认档，界面已注明。
- 默认档开始前，若本次粗估已可能超出全局当日剩余额度，会提示去设置页调高上限或改用无限制档。
- 任务区显示当前任务的预算模式。
- 同一发起幂等键换档位创建会返回 409（前端切档时自动换新键）。

### 语义折叠：停下时写明原因，服务商临时故障不再永久漏处理

- 任务结束时在 `coverage.stop_reason` 记一个停止原因，任务区按原因给出说明。只有能产生新进度的原因才显示“继续执行”；跨批轮数已用完、批次已降级、API Key 无效、模型不存在时不再显示。
- **默认档的行为变化（缺陷修复）**：
  - 遇到全局每日 AI 上限、服务商限频（429）、5xx、余额或配额不足时，原来会把当前批次标成“各自独立”并永久跳过，续跑也补不回来；现在改为停下，当前批次或窗口保持未完成，续跑时补上。
  - 5xx 在同一批内多重试一次，最多多花一次调用的费用。
  - 预算已用完时点“继续执行”，原来任务会排队后立刻再停；现在直接返回 429 并说明是哪项预算。
- 被全局每日上限拦下的请求其实没有发出，预约会撤销，不计费，也不占用调用次数。
- API Key 无效、模型不存在时任务直接失败，提示去配置页检查。
- 余额用尽（402，或 429 且报文为 insufficient_quota）单独识别为“余额或配额不足”，不再按限频提示“稍后再试”。
- 取消一个已中断的任务时，残留的预约按估算结算并释放，不再一直占用当日语义预算。
- 修复任务完成后点“重新分析”没有反应的问题（发起时复用了上一次的请求键）。

### 全局每日 AI token 上限调高为 200 万，可在配置页调整

- `llm.daily_token_limit`（所有 AI 功能共用）的默认值由 10 万改为 200 万。10 万不够：一个中等规模的语义折叠任务就要五六万 token，语义默认日预算本身也是 10 万，跑满后选题生成、抽奖筛选、UP 主分析、AI 自诊当天全部停用。
- 已安装用户的 `config.yaml` 若仍是旧默认值 100000，启动时自动改为 200 万，并写入 `llm.daily_token_limit_migrated: 1`，只迁移一次（桌面版、Web 版、AstrBot 插件都会执行）。自己改过的值、在 `user_config.yaml` 里覆盖过的值都保持不变；之后在配置页保存的任何值（包括 100000）也不会再被迁移。
- 配置页 AI 配置卡片新增“每日 token 上限”，单独保存，不需要重新填写 API Key；用量统计区新增“今日已用 / 上限”。
- **0 的含义改变**：以前 0 等于拒绝所有 AI 请求，现在 0 表示不限制（用量照常记录）。以前用 0 关掉 AI 的用户，升级后请改用一个很小的正数，或不配置 API Key。
- 上限只接受 0 到 10 亿的整数，`POST /api/config/llm` 和 `PUT /api/config/` 对非法值返回 422。修复 `POST /api/config/llm` 无法保存 0 的问题（原来用真值判断，0 被忽略）。手改 yaml 写出非法上限（负数、小数、字符串）时，按默认值处理并记录告警。
- 源码用户：仓库跟踪的 `config/config.yaml` 中这一行改为 2000000，本地改过这一行的话拉取时会冲突，保留自己的值即可。

### 许可证改为 AGPL-3.0，版本号升到 v0.3.0

- 许可证由 MIT 改为 GNU Affero General Public License v3.0（AGPL-3.0）。v0.3.0 之前发布的版本（v0.2.9 及更早）仍按其附带的 MIT 许可证授权。
- 同步更新 README 的 License 段、使用说明页的“使用声明”和免责声明第 1、4 条、欢迎页启动公告：写明协议名称（公告中加粗）与分发/网络部署时公开源码的义务，删去与 AGPL-3.0 冲突的“禁止商业使用 / 禁止企业或机构使用 / 需书面授权”条款，保留合法合规使用与免责内容。
- 侧栏、欢迎页与开场动画中的可见版本号由 v0.2.9 改为 v0.3.0；改动过的 `app.intro.js`、`app.notice.js` 追加缓存后缀 `-v030`。

### 实验性语义分组与可撤销自动折叠

- 复用现有聊天模型配置，新增单视频有限范围的异步语义分组任务；首次启用提示评论外发和可能产生的 token 费用。
- 语义结果独立于旧字符去重：批内分组与有界跨批全成员复核，未确认、排除或预算不足的内容保留独立，明确显示部分完成；不宣称真实语义准确率已验证。
- 自动折叠只影响展示，保留全部原文和成员声量；支持展开、撤销整组、拆出成员和显式重新允许折叠。人工保护按视频与评论 ID 持久化，刷新、重启或新任务不能自动覆盖。
- 保存任务检查点、预算预约和修订号；取消阻止迟到结果发布，重启后不自动继续付费请求，未知请求结果恢复前需确认。
- 评论采集增加 root/parent 上下文保留与旧库幂等迁移；未知历史上下文不冒充顶层评论，可重新采集补齐。
- 修正聊天客户端本地统计参数不应外发、显式 temperature=0 不应回退默认值的兼容问题；不新增必需向量模型或 SDK。

### 评论模糊去重批次缓存

- 同一批次内按原文复用 SimHash 指纹，避免在两两比较时重复计算；整条评论的指纹缓存仅在该批次存活（#136 另加了有上限的单字符位贡献缓存，见上文）。
- 保留现有两参数相似度策略替换方式；短评论规则、汉明距离与编辑相似度阈值、代表评论及分组输出不变。
- 预留带注释的批次相似度工厂接口，供未来接入已批量准备的向量特征；本次不启用模型、联网请求或新增依赖。
  当前仍是字符近似去重，不保证近义词或否定句的语义判定；未来语义方案需要独立校准和验证。
- 新增批次缓存的结果等价性、指纹调用次数、零指纹、生命周期与策略替换兼容性回归测试。

### 桌面内核迁到 PySide6

- 桌面壳从 PyQt5 5.15 + PyQtWebEngine 5.15（Chromium 83）迁到 **PySide6 6.12（Chromium 140）**。
  `requirements.txt` 改为 `PySide6==6.12.0`；不能与 PyQt5 装在同一环境（PyInstaller 会中止打包）。
- **WebEngine 数据目录变化**：Qt6 默认 profile 是无痕的，改用有名的持久化 profile，数据目录从
  `%LOCALAPPDATA%\BiliOpsToolbox\B站运营工具箱\QtWebEngine\Default` 变为
  `%APPDATA%\BiliOpsToolbox\B站运营工具箱\QtWebEngine\Default`。首次启动复制旧目录的 `Local Storage`
  （只复制、不移动，旧目录原样保留；成功后写 `.fishtool_migrated_from_qt5` 标记）。
- 桌宠首次启动位置改按主屏可用区域计算（Qt6 删除了 `QApplication.desktop()`）。
- 报告 PDF 封面中文明确回退到微软雅黑（Qt6 下 Bahnschrift 缺字会回退到宋体）。
- 打包：WebEngine 调试资源（`*.debug.pak` / `*.debug.bin`）不打进包；关闭 UPX。
- 前端的 Chromium 83 兼容写法与静态扫描测试**暂时保留**，迁移稳定后另行清理。

### 欢迎页启动公告

- 每次启动进入欢迎页时弹出「欢迎使用 FishTool！」公告：说明项目免费开源、官方不收取任何费用，
  被要求付费即为诈骗，并给出反馈邮箱。关键提醒加粗，防诈骗段落用红色竖条标出。
- 按钮「阅读使用说明」跳转使用说明页，「我已知晓」关闭；Esc、切换页面同样关闭，点遮罩空白处不关闭。
- 桌面版每次启动都会弹；浏览器里同一标签页刷新不重复弹（sessionStorage）。开场动画播放期间不弹，
  开场结束后再弹；截图模式 `?shot=1` 不弹。
- 样式沿用现有弹窗的工业风（顶边三色段、排线、空心大字、卡片底纹），底纹更淡。
  新增 `app.notice.js`、`style.notice.css`，不改动已有脚本与样式表；
  `tests/test_frontend_welcome_notice.py` 在真实桌面内核（PySide6 WebEngine）下覆盖各场景。

### Windows 一键启动 Web 版

- 新增根目录 `start_web.bat`：双击即启动 Web 版（`app.py web`，只监听 127.0.0.1），
  `/health` 就绪后自动打开默认浏览器；可传端口 `start_web.bat 9000`。
- 自动选择 Python 3.11/3.12（`.venv` → `venv` → PATH `python` → `py -3.12` → `py -3.11`），
  优先已装依赖的解释器；都缺依赖时询问是否 `pip install -r requirements.txt`。
- 端口上已有 FishTool（如桌面版）时只打开浏览器；端口被其他程序占用时启动前直接报错；
  失败时窗口暂停以便查看原因。退出时恢复控制台原代码页。
- 新增 `.gitattributes`，固定 `*.bat` / `*.cmd` 为 CRLF；`tests/test_start_web_bat.py` 锁住
  CRLF、无 BOM、注释纯 ASCII、经 `app.py web` 启动等约束。

### README 重写

- 首页 README 按当前界面重写：一键启动放在最前，功能说明按左侧菜单顺序，标明哪些功能用到 AI。
- 配图换成新版界面截图，统一放在 `assets/readme/`（开场动画、配置、热点发现、评论监控、
  头部 UP 拆解、账号自诊、抽奖工具）。

### 默认只监听本机（#5）

- `python app.py web` 的 `--host` 默认值、桌面版内嵌 Web 服务（源码模式子进程与打包版进程内
  uvicorn）的监听地址由 `0.0.0.0` 改为 **`127.0.0.1`**，局域网其他设备默认无法访问。
- `/api/config` 全部路由挂 `require_local_read`：`Host` / `Origin` 不在本机白名单内一律 403
  （`local_host_required` / `cross_site_request_blocked`），防 DNS rebinding 读取配置。
- **迁移说明**：需要局域网访问时，显式传 `--host 0.0.0.0`，并设置环境变量
  `FISHTOOL_ALLOWED_HOSTS`（逗号分隔，覆盖默认白名单，需同时保留 `localhost,127.0.0.1`，
  如 `localhost,127.0.0.1,192.168.1.10`）；否则配置页会返回 403。

### 账号自诊报告导出

- “导出报告”改为 PDF：未自诊时 toast 提示“请先进行自诊”；自诊成功后复用当前结果，
  不再重复抓取账号数据。
- 桌面端点击导出弹出原生 Windows 保存对话框，选择路径后由 Qt 直接渲染 PDF；
  普通浏览器无原生桥接时降级为打印预览，可选“另存为 PDF”。
- 新增 `/api/analysis/report-html`，用 Markdown/HTML 打印样式生成报告；
  修复原有 PDF 依赖 `wkhtmltopdf` 缺失时降级成 Markdown 的问题。
- 桌面端新增报告保存桥与 `desktop/report_export.py`，PDF 使用 A4 页面与中文字体。
- 报告新增“数据可视化”：导出时把柱状图、互动雷达图、可选分区基准图和 Tag 词云
  用 Canvas/ECharts 合成同一主题的仪表盘图片，随 HTML 一起嵌入 PDF，避免图表丢失。

### 启动默认页修复

- 修复桌面端启动后默认落到“抽奖工具”页：启动自检会点击“中奖名单校验”按钮，
  而 `verifyLotteryWinners()` 原先先 `navigateTo('lottery')` 再校验中奖名单。
  现在先检查名单，空名单只提示不跳转；桌面自检结束后也会显式恢复欢迎页。
- `app.lottery.js` 版本号更新为 `welcomefix-20261026`。

### 桌宠单击/拖拽判定修复

- 桌宠鼠标按下时不再立刻切到 `STATE_DRAGGING`；左键按住超过 100ms 即进入拖拽态
  （即使没有位移也有明确的拖拽形态反馈）；快速拖动仍以 6px/系统拖拽距离作为兜底。
- 释放时按“长按或拖动过”分流：拖拽才保存位置；快速单击触发 `toggle_menu()`。
- 桌宠快捷菜单重做视觉：暖白圆角卡片、主操作/普通/危险三级按钮、hover/pressed/focus 状态，
  面板尺寸调整为 236x176。
- 更新 `tests/test_desktop_pet_ui.py`，覆盖长按计时、阈值内抖动、拖拽释放不弹菜单、单击展开菜单。

### 热点发现页 UI 统一

- 热点生命周期：算法口径/采集分区/数量/播放量下限下拉框统一使用 `.form-control` 设计令牌，
  进度条改为与全局 loading 一致的主题渐变、10px 圆角轨道样式。
- 事件工作台：通用 `.btn` 增加统一的中性描边/悬停/禁用/键盘焦点样式；归属方式单选框改为
  主题色自定义圆形控件；事件工作台由 `.card` 改为 `.section`，去掉误继承的居中文本和悬停上浮，
  输入框改为整行铺满。
- 热点页表单组改为 flex 行布局：标签独占一行，输入框/下拉框/按钮统一 20px 间距；
  CreatorBrief 网格内输入框铺满列宽，解决控件贴在一起的问题。
- 同步更新 `style.pages.css`、`style.hotspot.css` 版本号，避免旧样式缓存。

### 进度反馈修复（假进度条 → 真实进度 / 转圈 loading）

- 通用 `showLoading`：`progress` 为 `null` / `undefined` / `0`（尚无真实进度）时不再画 0% 假进度条，
  改为统一转圈 loading + 文案；只有拿到大于 0 的真实进度才显示百分比与进度条。
- 评论采集：快速模式保留 `已采集/上限` 真实进度条；全面采集没有总量，改为转圈 + 已采集条数，
  采集结束/失败后自动隐藏进度区。
- 标签云：移除结果区固定 100% 的假 `<progress>`，完成状态改为文字汇总。
- 抽奖快速筛选、中奖名单校验：静态 18%/25% 改为转圈 loading；筛选/抽奖任务仍使用后端真实回调进度。
- 热点词云、UP 拆解、排名、活动、选题、LLM 用量等入口：无真实进度的加载态统一显示转圈，不再停在 0%。
- 前端静态资源版本号更新为 `progress-20261026`，避免旧 JS/CSS 缓存。

### 统一启动入口

- 新增根目录 `app.py` 作为唯一启动入口：
  - `python app.py`：默认桌面版，同时自动启动 `http://localhost:8000` 的 Web 服务；
  - `python app.py web`：仅启动 Web 版，支持 `--host/--port/--reload/--init-only`；
  - `python app.py init`：只初始化配置、数据库和运行目录。
- 删除根目录 `main.py`、`start_web.py`、`start_desktop.py`；桌面源码模式改为通过
  `app.py web --port <port>` 启动 Web 子进程，仍保持日志落盘与子进程退出检测。
- `web/main.py` 仅保留为 FastAPI 应用模块，移除其 `__main__` 直接启动入口；
  `desktop/main_window.py`、`desktop/pet_window/runner.py` 也移除重复入口。
- `build.spec` 改为只打包 `app.py`，并补充 `web.main` hidden import；README、QUICKSTART、
  STRUCTURE、交接文档同步更新。

### 异常处理统一（低优先级 logger / 高优先级 toast）

- 新增 `core/error_policy.py`：统一的 `report_exception` / `log_low_priority` /
  `report_high_priority`，按异常类型或显式参数分级；HIGH 级别写 ERROR 日志并投递 toast。
- 新增 `core/toast.py`：进程内 toast 总线（注册/注销 sink、短窗口去重、sink 故障隔离）；
  无 UI 环境只记日志，不抛异常。
- Web 侧 `web/main.py`：lifespan 注册 `/ws` 广播 sink；`HTTPException` 5xx 与未处理异常
  统一写 ERROR 日志并推送 toast；WebSocket 广播失败会记录堆栈并移除坏连接。
- 前端 `app.ui.js` / `style.modal.css`：新增右上角 `showToast()` 与 `/ws` 自动重连订阅；
  桌宠新增 `type=toast` 通知气泡，主窗口新增非阻塞 `show_toast()` 托盘提示。
- 前端 `apiRequest`：5xx 改走 toast（与后端 `/ws` 同文案去重），4xx 保留既有模态提示；
  FastAPI 结构化 `detail` 不再被拼成 `[object Object]`。
- 清理全部生产代码中的静默宽泛捕获：54 处宽泛 silent except 降为 0（测试/工具目录除外），
  89 处 ERROR 级日志补齐 `exc_info=True`；解析类兜底、旧库缺列降级等补 `debug` 日志。
- 修复 `ConfigNotFoundError` / `RecordNotFoundError` 构造时父类不接受 `code` 参数导致的
  `TypeError`；`ConfigError` / `DatabaseError` 现允许子类传入更具体的错误码。
- 修复评论预警入库/查询、标签云落库、增量断点查询在 `get_session()` 失败时
  `session` 未绑定、`except/finally` 二次抛 `UnboundLocalError` 的问题。
- 修复桌面版源码模式启动 Web 子进程时 `stdout/stderr=subprocess.PIPE` 且从不读取，
  日志写满管道后 Web 服务阻塞、表现为「页面能打开但所有网络请求超时」；子进程输出
  改为落盘 `data/logs/web_subprocess.log`，并在子进程退出时把退出码与日志末尾展示出来。
- 新增 `tools/check_exception_handling.py` 与 `tests/test_error_policy.py`：静态拦截裸
  except / 静默宽泛捕获 / 只写 pass，并覆盖 toast 去重与分级行为。
- 规范文档：`docs/EXCEPTION_HANDLING.md`。

**口径：热点生命周期双轨 —— 默认 `lifecycle_v2`，可切回 `heuristic_v1`；单视频持续追踪链（watch）固定走 v2。**

- `GET /api/hotspot/lifecycle` 与 `HotspotService` 门面的默认 `algorithm` 由 `heuristic_v1`
  切为 `lifecycle_v2`；热点页生命周期区块新增算法下拉（复用既有 `hotspot-collect-select`
  样式，会话级记忆），可一键对照 `heuristic_v1` 回放。
- 算法注册表新增 `lifecycle_v2` 注册；`create_detector()` 的**无参默认仍为 `heuristic_v1`**
  （仅注册表默认，与接口 / 门面默认解耦，既有契约测试压着）。
- 生命周期卡片按 `algorithm_version` **分流渲染指标**：v2 走 `relative_change` /
  `coverage_ratio` / `observed_windows` / `sample_count`，v1 仍走 `growth` / `up_count` /
  `observed_days` / `window_span_days`；趋势徽标的观测基数统一收敛为「有效观测单位」
  （v2 取 `observed_windows`，v1 取 `observed_days`/`days`），不再因键集不匹配而整体
  退化为 `0` /「数据不足」。
- `confidence` 第一版仍固定 `0.0` / `not_estimated`（本批不真算）；前端按 `confidence_kind`
  分流显示为「未估算」，不再展示 `0%`。
- `GET /api/hotspot/watch`（列表 / 详情 / 创建 / 释放）与事件工作台评估、机会响应统一补
  `algorithm_version`：watch 链路与事件窗口内核恒 `lifecycle_v2`，版本号写死取自各自链路，
  不经算法注册表。
- 热点页单视频跟踪列表上屏后端早已下发的 `last_confirmed_stage` / `coverage_ratio` /
  `coverage_state`（纯前端展示，字段缺失时留空，不伪造 `0`）。
- 常驻 watch 循环装配补齐默认预算门（`RequestBudget`）与事件需求整编 hook
  （`EventWatchDemandReconciler.reconcile`）；缺省采集端口复用编排层同一 `RequestBudget`。

### 热点稿龄与出现期门（08 执行案 H2 + B0—B7）

> 口径前置：本批有两种完成度，**必须分开看**——「字段完成」只代表数据链与消费者接通，
> 「阶段门完成」才代表该条件真正参与阶段判定。**不许**合成一句「年龄门槛已经限制出现期」。

**【字段完成】稿龄字段可用（B1—B5）**

- `confidence` 语义归位：仍固定 `0.0`，并统一输出 `ConfidenceKind.NOT_ESTIMATED.value`
  （wire 恒为小写 `not_estimated`）。这是**有意不估算**，不是未实现、不是缺陷、也不是 TODO；
  前端按 `confidence_kind` 显示「未估算」，不展示 `0%`，无 `kind` 的旧卡片显示「暂无数据」。
- 发布时间落库：`video_stats` 新增 `pubdate_epoch_s` / `pubdate_status`（幂等加列，旧行保持
  NULL 不回填、不重写）；读端旧 NULL 一律判 `unknown`，不猜 UTC、不造默认 `ok`。
- 稿龄计算：`resolve_publication_age()` 纯函数 + `age_status` 全取值（`ok` / `missing` /
  `invalid` / `unknown` / `future_publication` / `conflicting_publication_time` /
  `as_of_unavailable`）；`Detection.metadata` 增 `age_days` / `age_status` / `age_source` /
  `as_of_epoch_s` / `age_reference="evaluation_as_of"`，`metrics` 仍为纯数值通道。
- 三条 Snapshot 通路（直接构造 / watch 加载 / HTTP 入口）同数据同 `as_of` 对照一致。
- **H2**：`analyze_one` 先定 `as_of` 再筛可见行，晚于 `as_of` 的快照不再抢占 `latest_row`
  元信息、也不再进入 `rates`。
- **H4**：watch 检测器改为**逐目标构造**，本轮采集写入的新快照当轮即可进入评估，不再错开一轮。

**【阶段门完成】出现期门（B6a / B6b，独立行为版本）**

- **B6a**：`emerge_age_days` 真正接入出现期判定——首次形成有效强度、达到 `emerge_rate`，
  且作品在该窗口端点年龄 `<= emerge_age_days`（默认 7 日）才判出现期；
  `emergence_basis` 取 `publication` / `none`。
- **B6b**：新增「新发现老视频」通道 `emerge_discovery_days`（默认 2 日）——`first_seen_epoch_s`
  落在窗口端点内同样可判出现期，`emergence_basis` 增加 `discovery`。该通道属**产品判断**，
  会扩大出现期覆盖面（发布一年的视频可能因今天首次入池而被标为出现期）；命中时必须标依据
  `discovery`，**不得**表述为「视频刚发布 / 事件刚发生」。
- `threshold_version`：`lifecycle_v2_defaults_1` → `lifecycle_v2_age_gate_1`（B6a）→
  `lifecycle_v2_age_gate_2`（B6b）；**两批各自独立版本，不合并**。
- 旧 / 不同版本 `state_json` 不沿用 `prev` / `candidate` / `count` / `stable_count` / `stage`
  等计数，按可用历史显式重算；旧 `last_confirmed_stage` 仅作 legacy 历史，不伪称新版本已确认。

**历史时区与回放限制**

- 每个窗口的出现期门使用该窗口 `end_s` 的当时证据，**不是今天年龄**；`age_days` 输出才以请求
  `as_of` 为准。二者分别命名，不用今天的年龄改写旧窗的新旧判断。
- 历史回放只采信 `capture <= end_s`（`as_of`）的 pubdate 证据，不把后来才采到或纠正的元信息
  当作过去已知；该窗口没有可信发布时间的，仍可使用可信 `first_seen` 条件。
- `pubdate` / `first_seen` **两项均未知时不输出出现期**，只保留观察 / 数据不足；也不因此永久
  卡死——后续有足够有效窗仍可进入上升 / 成熟 / 衰退。
- 边界一律**精确秒比较**，不截整天；负年龄与未来 `first_seen` 视为无效，不硬写
  `pubdate=first_seen` 或 `pubdate=now` 让旧账号过门。

**发布方式（§M1）**

- B1 可单独发布；B2/B3 向后兼容先接好；B4/B5 发布「稿龄字段可用」；B6 独立发布并明确行为
  版本，不隐藏在「补个字段」的提交里。
- 不因缺历史发布时间无限暂停全部生命周期：继续输出可用强度、未知稿龄与理由。

**回退（§M2）**

- 两列新增后回退代码**不删列、不重写旧数据**。
- 回退 B4/B5 时可暂 `age=None`、标 `disabled` / `unknown`，但 `confidence` 仍 `not_estimated`，
  **不得**恢复覆盖度折算的假概率。
- 回退 B6 必须按算法版本处理旧 / 新 state，不把新门候选计数塞回旧算法继续；原快照不删。
- 删除 Snapshot 新字段前，先回退所有生产构造调用与序列化使用。

**验证**

- 全量 **2443 passed / 2 skipped / 0 failed**（282.54s）。
- 反例与 E 系列定向（A01 / A06 / A08 / A11 / E02 / E03 / E06 / E07）→ **13 passed**。
- 尚未验证：真实 B 站接口、live 网络、跨重启持久；前端仅 `node --check` 级检查。

### watch 预算修复（07 执行案 W1—W4）——三项状态**必须分开看**

> 口径前置：本批只修 **L 层逻辑预算**（内存账本、重启清零），
> **H 层 HTTP 配额数字与语义一律未动**（1800 与五类 480 / 1000 / 150 / 72 / 98 逐字不变）。
> **不许**把下面三条合成一句「配额已修复」。

**【已实现】单请求单扣（L 层）**

- 一次逻辑采集只消费**一份** L 层额度：watch 调度 `reserve` 一次，采集以 `redeem` 兑换**同一份**票据，
  不再走第二次 `acquire`。
- 凭证只兑换一次（issuer + operation_key + reserved 状态三项校验）；未兑换的在 `finally` 里
  `release_unused` 释放；已兑换不退款（逻辑尝试，不等同实际发送数）。
- 预留带硬超时（`reserve_deadline_mono`），超时清扫兜底，超时后 `redeem` 返回 `admission_expired`。

**【已启用】类别策略（L 层，`mode: partitioned`）**

- `config/budget.yaml` 新增 `watch_scheduler` 段（**纯追加，不改 quota 任何数字**）：
  `normal_watch` / `fast_watch` / `general` 类别窗 + 类间轮转 + 按类查询候选；`partitioned` 已启用。
- 分配数值：10/8/2、150/120/30、1500/1200/300，为 §10.1 提案值，**维护者 2026-10-05 批准照该组走**
  （`approved_maintainer_2026-10-05`；原 `pending_maintainer_approval` 字样保留供追溯）。
  三类各窗口合计恰等于 total（20 / 300 / 3000），自洽无超额；**非仓库既有常量、非实盘最优值**。
- `mode: shared_only` 为兼容模式，**必须显式 `category_isolation=false`**，不默认静默降级。

**【未实施】HTTP 子额度（H 层）**

- H 层 watch 父额度（1000）仍由 normal / fast 两类**共享**；跨重启 / 跨进程的实际 HTTP 子预算
  **本阶段没做**（对应执行案 §12 第二阶段，无独立审批与迁移验收则不执行）。
- **不得**表述为「已修复」或「HTTP 已分桶」。

**观测口径变化（W2 引入）**

- `budget_skipped` 语义变更：从「扫描窗内目标数」改为「**被跳过的类别队首数**」
  （因每类读取上限 = `limit`）。

**验证**

- 全量 **2359 passed / 2 skipped / 0 failed**（285.21s，口径 B 含桌面）；
  H 层聚焦 **76 passed**；W1—W3 聚焦 **116 passed**。
- 详见 `docs/watch_budget_regression_2026-10-05.md`。

**尚未验证**

- live 网络、真实 B 站、H 层真实发包计数、跨进程 / 跨重启持久；桌面 Qt 交互单跑未做。

### Cookie 池凭据不再明文落库（issue #5 第 3 点）

- `cookie_pool.sessdata` 列改存 `sha256:<hex>` 摘要，仅用于去重；`bili_jct` 列恒为空串。
  完整 Cookie 只保存在 Fernet 加密的 `cookie_data` 中，运行时字段由 `load_from_db` 解密后解析。
- 加密失败时**不再降级写明文**：本次登录态只保留在内存池并记 warning，重启后需重新登录。
- `DatabaseManager.create_tables()` 新增幂等迁移：已有明文 `sessdata` 改写为摘要、
  `bili_jct` 清空；已是 `sha256:` 前缀的行跳过，重复启动零改动。

**发布说明**

- 迁移用 UPDATE 原位改写，旧明文**可能残留在 SQLite 空闲页 / WAL 文件**中。升级并启动一次后，
  建议关闭程序再对 `data/bili_ops.db` 执行 `VACUUM`（例如 `sqlite3 data/bili_ops.db "VACUUM;"`）。
- 迁移只处理当前库文件；`data/backup_*.db` 等**历史备份仍含明文凭据**，需用户自行删除或妥善保管。

## [0.2.4] - 2026-10-02

**主题：02 单视频时序跟踪上线；06 发现通道接通常驻调度与 watch 候选入池。**

这一版把「发现 → 跟踪」串成闭环：06 的聚合入口广度发现接入常驻调度，其中
`popular` / `ranking_all` 的条目自动成为 watch 候选；02 侧补齐单视频时序采样的
算法、落库、编排、Web 端点与常驻循环。两条新增常驻任务**默认关闭**，
不显式配置则不会采样、不会占用配额，行为与上一版完全一致。

### 新增

**02 · 单视频时序跟踪**

- **`modules/hotspot/watch_service.py`** —— 编排层。一轮 tick 顺序为
  先清 → 捞 → 领 → 采 → 评 → 写 → 排；同一轮单目标只有一条事务，任一步失败即
  rollback 并计入失败隔离
- **`modules/hotspot/watch_store.py`** —— watch 表读写；`upsert_watch` 幂等，
  已存在的目标不被重置
- **`core/database/models_hotspot_watch.py`** —— `hotspot_watch` 表
- **`modules/hotspot/algorithm/lifecycle_v2.py`** —— 单视频时序判定算法
- **`web/routers/hotspot/routes_watch.py`** —— `GET /watch`、`GET /watch/{bvid}`、
  `POST /watch`、`POST /watch/{bvid}/release`
- **前端 `index.html`** —— watch 管理区块（列表 / 三态筛选 / 新增 / 释放）

**06 · 接线**

- **`modules/hotspot/discovery/watch_ingest.py`** —— 候选入 watch 池的桥接层，
  只吃 `popular` / `ranking_all` 两个来源；读的是内存结果，**不发 HTTP**

### 变更

- **`modules/hotspot/algorithm/base.py`** —— `Detection` 新增 `metadata` 通道，
  与约定「仅数值或 None」的 `metrics` 分离，二者键集互斥
- **`modules/hotspot/algorithm/lifecycle_v2.py`** —— `coverage_state` 移出 `metrics`，
  与 `confidence_kind` 一并走 `metadata`；自此 `metrics` 为**纯数值通道**
- **`modules/hotspot/discovery/service.py`** —— `run_discovery_loop` 新增 `on_snapshot`
  回调（sync / async 均支持；不传则行为与改动前逐字一致）；`list_keyword_candidates()`
  未动
- **`core/monitor_service.py`** —— 常驻任务扩至 4 条

### 常驻任务与开关

| 任务 | task 名 | 开关 | 默认 |
| --- | --- | --- | --- |
| 评论增量采集 | `bili-comment-monitor` | `monitor.enable` | 关 |
| Cookie 巡检 | `bili-cookie-check` | 无（总是起） | — |
| watch 时序采样 | `bili-watch` | `monitor.watch_enable` | **关** |
| 发现轮询 | `bili-discovery` | `monitor.discovery_enable` | **关** |

入池不快照 discovery 单独开关：它不发请求、不花钱，跟着 `discovery_enable` 走；
真正花钱的采样由 `monitor.watch_enable` 单独把关，两个开关职责不重叠。

### 指标口径

- `confidence_kind` 与 `coverage_state` 属非数值项，走 `Detection.metadata`，
  不得写进 `metrics`（`metrics` 全键皆为数值或 `None`，前端可直接绘图）
- watch 三态 `active` / `expired` / `manual_stop`：手动停追只由 release 端点写
  `manual_stop`，与自动到期分离
- fencing 用 `state_revision` 当代际：写回代际不匹配即丢弃，不写任何列

### 验证

- 全量测试 **1871 passed / 1 skipped / 0 failed**
- `on_snapshot` 不传时行为与改动前逐字一致（有对照用例）
- 候选入池零 HTTP（有测试钉死）
- 默认配置下无任何路径拉起 watch / discovery 循环（各有反向用例兜底）

### 尚未验证

- **06 → 04 的消费接口仍未接线** —— `DiscoveryService.list_keyword_candidates()`
  已备好但零调用点，接 04 需改动 04 代码。
  即 v0.2.3 记的「06 与 04 的消费接口尚未接线」在本版收掉一半：**06 → 02 已接**
- 真实 B 站接口 —— 本版测试全程 stub
- watch 循环实盘节奏 —— 未跑满 24h 观察 `watch` 类配额 1000 的实际占用

### 发布说明

- 两条新增常驻任务均默认关闭；不配置则行为与本版之前完全一致
- 新增 `hotspot_watch` 表，`manager.py` 幂等建表，旧库可原地升级；历史数据不回改
- 回滚只需关掉 `monitor.watch_enable` / `monitor.discovery_enable`，或切回旧界面

## [0.2.3] - 2026-10-02

**主题：配额账本落地（唯一账本 + 总闸挪位）与 06 采集广度发现通道；另含 01 排名方案 A。**

这一版把 HTTP 尝试配额从「内存里的计数器」变成**落盘、可重启续算的唯一账本**，
并新增**免 Cookie 的聚合入口发现通道**（06）——广度靠读、深度靠采，不再自采全站。
三条线相互独立，均不改变既有采集链路的调用方式。

### 新增

**01 · 账号排名（方案 A 阶段性）**

只新增一个排名子包和一张快照表，不改既有采集链路，不引入独立部署。
排名是**明确参评集合内**的账号对账号排名，不是全站排名，也不代表抽样代表性。

- **`modules/self_diagnosis/benchmark/`** —— 排名子包
  - `contracts.py` —— `BenchmarkPolicy` / `CreatorSample` / `BenchmarkResult`（schema=3）
  - `metrics.py` —— `median_twice` / `rank_one`，用 `score_twice` 整数比较，避免浮点导致并列误判
  - `collector.py` —— 只采排名必需字段，不跑整套自诊；含 `discover_candidates`
  - `store.py` —— `BenchmarkRun` 短事务，`id+status+lease_token` 条件 UPDATE 并检查 rowcount
  - `service.py` —— 编排、冻结、重试、取消，以及 2 小时观测窗约束
- **`core/database/models_benchmark.py`** —— `BenchmarkRun` 快照表，已从 `core/database/__init__.py` 导出
- **`core/request_budget.py`** —— ContextVar 请求预算，默认 `None`，不改变旧调用行为
- **`web/routers/benchmark.py`** —— `/api/analysis/benchmark` 候选发现、发起、查询、取消、重试
- **`web/local_guard.py`** —— 本机写端点校验（同源 + CSRF + session token）

**配额账本**

- **`config/budget.yaml`** —— 配额唯一账本（单账号总闸 1800 attempt / 滚动 24h）
  - 五类配额：`discovery` 480 / `watch` 1000 / `ranking` 150 / `maintenance` 72 / `flex` 98
  - 分域冷却与节流：`cookie` / `no_cookie` 两域各自独立，互不连坐
  - 硬约束：业务与基础设施代码不得再出现硬编码配额数字
- **`core/quota_store.py`** —— 小时桶窗口统计与 `http_attempt_log` 读写
- **`core/database/models_quota.py`** —— `http_attempt_log` 模型

**06 · 采集广度**

- **`modules/hotspot/discovery/`** —— 聚合入口发现通道
  - `contracts.py` —— `BroadKeyword` / `BroadVideo` DTO、质量三态、来源优先级、按 bvid 合并
  - `sources.py` —— 三源读取与容错解析（纯 I/O，不含聚合逻辑）
  - `store.py` —— 关键词与视频信号写入器
  - `snapshot.py` —— 全局发现快照（原子 JSON，含更新与保留协议）
  - `service.py` —— 调度、共享轮询缓存、去重落库、快照冻结
- **`HotKeywordSignal`**（表 `hot_keyword_signal`）—— 热搜词观测事实表，唯一键 `(keyword, captured_epoch_s)`
- `tests/data/discovery/sources_fixtures.json` —— 三源离线 fixture
- `tools/discovery_smoke.py` —— 一次性低频联网冒烟（不发送 Cookie）

### 变更

- **`bilibili/api/client.py` / `signer.py`** —— 在真实 HTTP 发送点接预算钩子；`RequestBudgetExceeded` 保留类型，不被包装成可重试的普通 API 错误
- **`web/main.py`** —— 装配 `BenchmarkService`；清理改 `try/finally`，只关闭自己拥有的 client
- **`web/routers/analysis.py`** —— 自诊与导出可带 `benchmark_run_id`，读取同一份冻结结果，不重新触发采集
- **`report_generator.py`** —— 新增 `creator_ranking` 段；keyword-only 参数，旧位置参数不受影响
- **前端** —— `app.up.js` / `index.html` / `style.up.css` 增加同行名单、指标说明、发起/取消/重试

**配额账本**

- **`core/request_budget.py`** —— 配额判定接入落盘账本；**总闸校验从请求路径挪到配置加载期**
  （分类配额本就是硬上限，留在请求路径上是死代码）
- **`core/database/manager.py`** —— 幂等建表，旧库可原地升级
- **`core/monitor_service.py`** —— 启动时读回窗口，重启后配额续算

**06 · 采集广度**

- **`core/database/models_hotspot_signal.py`** —— 新增 `HotKeywordSignal`
- **`core/database/__init__.py`** —— 显式导入注册（不注册 `create_all` 不建表）
- **`.gitignore`** —— `data/` 收窄为 `/data/`。此前裸 `data/` 连带忽略 `tests/data/`，
  以致测试 fixture 从未进过仓库，clone 后 fixture 缺失

### 指标口径

`recent10_age7_30_median_views_v1`：请求时刻往前 30 天内、稿龄 7—30 天的最近最多 10 条
公开稿件，取**中位累计播放**；至少 3 条；选中稿件缺失不打替补，该作者本轮不参与自动排名。
播放取自同次详情请求的 `stat.view`，真实 0 有效。名次为 competition rank，并列同名次。

**06 · 采集广度（易错点，写死）**

- `ranking` 全站榜**不复用** `get_ranking()`：其实现强制带 `day` + `pn`，实测三种组合全部 `-352`；
  改为裸 URL 并单独覆写 `Referer` 为 `.../v/popular/rank/all` 才通。**属易变项**，由冒烟脚本持续验证。
- 三源 parser 统一吃「已拆 data + 补 code 的统一 envelope」，逐源在 docstring 写明内层路径
  （`data.trending.list` / `data.list` / `data.list` + `others`）。
- 同一视频按 bvid 去重，**发现来源全部保留**；展示值按固定来源优先级
  `ranking_all > popular > ranking_all_others` 取值，冲突打标记，**不按最大播放量挑**
  （会引入向上选择偏差）。
- `heat_score` 只作「平台接口返回热搜分数」，**不进入播放增量计算**；
  缺失记 `NULL` / `missing`，非法值跳过且不写 0。
- `pid_v2` / `tidv2` / legacy `tid` 三套分类字段并存，不合并成单一 tid。
- 失败状态落**全局发现快照**，可区分四种情形：本轮真空榜 / 请求失败 / 仅第一页成功 / 用的是上轮缓存。

### 验证

- 全量测试 1586 passed / 0 failed（本机未装 PyQt5，桌面组四个文件排除在外）
- 数值验收：`rank=4 / rank_end=5 / total=6 / percentile=30`；全等并列 1—N 且 P50；
  单 peer 有名次无百分位；peer 列表含目标 UID 重复时只算一次
- `tools/verify_creator_ranking.py` 端到端 16 步全绿
- **本版发布前全量复跑：1672 passed / 1 skipped / 0 failed**（含桌面组全部文件）
- 配额持久化：独立复跑 17 passed；总闸判定挪位后行为不变
- 06 联网冒烟（三入口各 1 次，含礼貌间隔，不发送 Cookie）：
  `search/square` code=0 / 10 条、`popular` code=0 / 20 条、
  `ranking/v2?rid=0` code=0 / 主榜 100 条 + `others` 5 条

### 尚未验证

- 真实 B 站接口 —— 本轮全程 stub，未使用真实账号与 Cookie
- 浏览器 UI 交互 —— JS 仅通过 `node --check` 并与后端契约对齐
- PDF 真实格式 —— 本机未安装 pdfkit / wkhtmltopdf
- **06 的易变项** —— `ranking` 端点的 `Referer` 要求由平台侧决定，本次实测通过不代表长期可用，
  由 `tools/discovery_smoke.py` 持续验证
- **06 与 04 的消费接口尚未接线** —— `DiscoveryService.iter_video_candidates()` /
  `list_keyword_candidates()` 已备好，但接入 04 需改动 04 代码，本版未做
- **06 的请求节奏实盘表现** —— 仅做过一次性冒烟，未跑满 24h 观察 discovery 480 的实际占用

### 发布说明

- 本 tag 覆盖 v0.2.2 之后的全部提交，含此前以阶段性提交合入 main、未单独打 tag 的 01 排名方案 A
- 配额账本引入落盘表 `http_attempt_log`，`manager.py` 提供幂等建表，旧库可原地升级
- 06 的发现通道在接 04 之前**不影响**既有链路：新模块无人调用，调度需显式启动
- 回滚只需切回旧界面或关闭新功能，不影响既有自诊、词云与抽奖
- 回滚配额改动时注意：旧库中 `http_attempt_log` 可保留不用，不影响旧行为

## [0.2.2] - 2026-09-29

**主题：把「未知」和「真实的 0」分开。**

此前上游缺失的字段会被伪造成 `0`，下游把「没采到」当成「真的是 0」参与计算，
自诊的分位、均值、配对率因此系统性失真。这一版从采集、落库、算法到前端，
全线拆开这两个概念。

### 新增

- **`core/data_quality.py`** —— 严格的数值与时间契约
  - `parse_count` / `parse_ratio` / `to_epoch_s`，显式区分 `missing` / `invalid` / `ok`
  - 时间戳解析处理时区歧义（回拨重复时刻、不存在时刻）与无时区输入
- **`modules/hotspot/snapshot_store.py`** —— flush-only 快照写入器
  - 缺失指标写 SQL `NULL`，而不是落列默认值 `0`
  - 不自行提交事务，scope 与 transaction 归调用方
- `VideoStats` 新增 4 个可空元数据列：
  `captured_epoch_s`、`collection_tid`、`raw_tid`、`metric_status`
- 新增索引 `ix_video_stats_video_captured`

### 变更

- **`bilibili/api/user.py`** —— wrapper 在补默认值**之前**记录原始字段状态，
  返回体新增 `_meta.source` 与 `_meta.field_status`；旧兼容默认值保留，但缺失不再被 0 穿透
- **`modules/hotspot/collector.py`** —— 去掉 `_read_stat_int(...) or 0`，缺值不再落 0 入库
- **`web/routers/hotspot/routes_lifecycle.py`** —— 读端按质量优先级解析状态，
  新增 `inconsistent_quality`；坏点转为 `None` 同时保留 epoch，避免虚假的 -100% 与跨缺口平滑
- **`modules/lottery/cache.py`** —— 缓存 schema 升级到 v3：
  full / draw 双空间、TTL、seq 定序（迟到的旧结果不覆盖新结果）、原子写（临时文件 + `os.replace`）
- **账号自诊** —— 有效样本求均值、同稿配对分母、粉丝 `None` 与 `0` 不再输出伪 0 比例、采集覆盖度写入 payload
- **前端** —— `app.up.js` / `app.lottery.js` / `app.hotspot.js` 指标缺失显示为空值或三态，不再显示 0

### 修复

- 上游字段缺失被伪造成 0，连锁导致自诊百分位、均值、配对率失真
- 旧抽奖缓存全字段为 0 时，无法区分「未验证」与「真实为 0」

### 迁移说明

- `core/database/manager.py` 提供幂等 ALTER 与幂等建索引，旧库可原地升级
- **历史数据不回改**；新写入的缺失值以 `NULL` 表达
- 旧抽奖缓存不会自动转为有效值，统一标记 `legacy_unverified`，需重采或判定为 indeterminate

### 发布说明

- 写端新增 `NULL` 与旧读端语义不兼容，因此本版**整体发布**，不拆成部分更新
- 回滚时保留新增列与历史数据，不回滚成「缺失即伪 0」的旧行为

[0.2.2]: https://github.com/FORK-is-coding/FishTool/compare/v0.2.1...v0.2.2
[0.2.3]: https://github.com/FORK-is-coding/FishTool/compare/v0.2.2...v0.2.3
[0.2.4]: https://github.com/FORK-is-coding/FishTool/compare/v0.2.3...v0.2.4
