# AI 伪人巡检（抽奖工具子功能）

输入视频 BV 号扫描评论区，先用本地规则词典巡检（重点筛查「大肥鱼 / 肥鱼 / deepseek / 吃白饭 / 养鱼」相关昵称与内容），可再用已配置的 AI 估计每条评论的伪人率，由用户确认后举报。

## 流程

```
[开始扫描]  BV号 → 视频元数据 → 一级评论（+可选楼中楼）→ 落库 + 规则巡检
[AI甄别]    灰区送审 → 脱敏打包 → LLM 批量判定 → 融合 → 最终伪人率 + 分档
[一键举报]  勾选 → 校验 / 按账号去重 → 二次确认 → 原生举报（默认）或代发举报 → 台账
```

代码位置：`modules/lottery/patrol/`（规则、采集、甄别、举报、编排），`web/routers/lottery_patrol.py`（路由），`web/frontend/static/js/app.lottery.patrol.js`（前端）。

## 规则词典

- 文件：`modules/lottery/patrol/rules_default.py`（默认规则，Python 常量，打包版不会漏文件）。
- 版本：`contracts.RULES_VERSION`，每次改规则递增。
- 字段权重：昵称 / 签名强，正文弱（真人常在正文里骂机器人）。
- 同组只取最高：例如「大肥鱼」同时含「肥鱼」只计一次。
- 硬命中：只有带第一人称或句首的自我暴露（自称本鲸 / 自述 AI / DSH 自述 / 提示词泄漏）。
- 软信号合计上限 70（低于 bot 阈值 75），所以**只靠软信号永远到不了高度疑似**，必须有硬命中或经过 AI 甄别。
- 用户自定义关键词：只按字面匹配，权重 −30..30，不允许硬命中。
- AI 惯用短句腔调（`rules_default.PERSONA_PHRASES`，来自用户提供的典型 AI 发言语料）：
  - 整句腔调（如「这句话稳」「这个账我记住了」「睡吧，我守着」「你自己掂量」）单条 +12；
  - 日常短语（如「我记住了」「不催」「回去睡」「很平」）单条 +5；
  - 同一条评论命中的不同短语累加，合计封顶 35，仍是软信号，单靠腔调到不了「高度疑似」；
  - 去掉腔调片段后仍带口语词（哈 / 啊 / 吧 / 呢 / 嘛等）时一律按弱信号计；
  - 评论在吐槽、引用 AI 腔（含「AI 味」「人机」「机器人」「又是这套」等）时不计分。
  - AI 甄别的系统提示词同步列出这批腔调，并说明单个日常短语不算证据。

### 修改规则后必做

1. 跑 `python -m pytest -q tests/test_lottery_patrol_rules.py`。
2. 负样本回归：`tests/fixtures/patrol_negative_samples.json`（220 条含关键词或腔调短语的真人评论，由 `tools/gen_patrol_negative_samples.py` 生成），要求 0 条进入疑似档。
3. 评估：`python tools/patrol_eval.py --labeled data/patrol_eval/labeled.jsonl`，目标「高度疑似」档精确率 ≥ 95%。

## 第三方来源声明

本功能参考了以下 MIT 许可项目的规则思路与安全设计，未复制其完整代码：

- **liyupi/dafeiyu-cleaner**（https://github.com/liyupi/dafeiyu-cleaner，MIT）：离线加权规则打分、灰区送 LLM、45%/55% 融合、每批 8 条、7 天缓存。
- **NothingCooker/fxxk-off-dfy**（https://github.com/NothingCooker/fxxk-off-dfy，MIT）：默认演练、硬命中 / 软信号分离、mid/rpid 白名单、随机间隔、风控退避。

```
MIT License

Copyright (c) 2026 liyupi/dafeiyu-cleaner contributors
Copyright (c) 2026 NothingCooker/fxxk-off-dfy contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 举报接口实测记录

用测试账号对真实评论走完整的代发流程（首次同意风险说明 → 逐批确认 → 提交），记录如下：

| 日期 | 测试账号 mid 后四位 | 请求参数 | 返回码 | 结论 |
|------|---------------------|----------|--------|------|
| 2026-11-10 | 4907 | `POST /x/v2/reply/report`，表单 type=1, oid=视频 aid, rpid=评论 rpid, reason=0, content=疑似AI自动生成评论的机器人账号，请核实, csrf=*** | 0（4 条全部成功） | 接口参数正确；分两批各 2 条提交，每条约 0.2 秒返回，未触发限频或风控；台账状态均为「已举报」，审计记录 2 条批次均为完成 |

## 已知限制

- 伪人率是基于公开信息的估计值，未经大规模标注验证；不等于身份认定。
- 楼中楼只采集前若干楼，跨评论特征（复制粘贴、互相回复）依赖采集覆盖范围，结果可能偏低。
- B站举报理由表会变化：优先用 `metadata` 接口，失败才用离线兜底表。
- 代发举报使用当前登录账号，频繁或不当举报可能被平台限制；触发风控会让其他采集功能暂停至少 15 分钟。
