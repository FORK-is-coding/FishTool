# FIX_REPORT: 抽奖候选用户等级与大会员状态

## 根因结论

问题在后端持久化链路，前端不是主因。

1. B 站评论解析器已能读取 `member.level_info.current_level` 和 `member.vip`。
2. 修复前 `Comment` ORM 与实际 SQLite `comments` 表均没有 `level_info`、`vip` 列。
3. 评论入库只写了 UID、昵称、正文、时间等字段，等级和会员信息在解析后被丢弃。
4. 抽奖模块从本地视频评论读取时也没有返回等级和会员字段，因此前端只能显示 `Lv?/会员未知`。
5. 前端候选列表已读取 `level/is_vip/vip_type/vip_label/ctime`，也已有等级色阶和会员样式，不是本次未知状态的根因。

## 修复内容

- `comments` 增加 `level_info JSON`、`vip JSON`，启动时执行幂等轻量迁移，旧评论不删除、不重建。
- 评论解析保留 B 站接口原始 `level_info`、`vip`，新评论直接落库。
- 再次采集遇到相同 `rpid` 时，不重复插入，只补齐该旧行缺失的等级/会员字段。
- 抽奖优先读取本地评论中的原始字段并标准化为 `level/is_vip/vip_type/vip_label`。
- 历史行缺字段时，按去重 UID 优先使用 `data/lottery_cache/user_profiles.json`；缓存也没有才请求最小用户资料，并原位回写同 UID 的旧评论。不会全量重爬评论区。
- 候选和中奖卡片显示：
  - Lv1 灰、Lv2 浅绿、Lv3 蓝、Lv4 橙、Lv5/Lv6 红，均为白字圆角框。
  - 非会员为灰底 `◇非会员`，大会员为粉底 `♦大会员`，年度大会员为粉底 `◆年度大会员`。
  - 评论时间以 `◷完整日期时间` 放入圆角矩形框。
- 候选卡片加入现有鼠标跟随光晕选择器，保持莫兰迪暖色体系。

## 改动文件

- `core/database.py`
- `modules/comment/collector.py`
- `modules/lottery/service.py`
- `web/frontend/static/js/app.js`
- `web/frontend/static/css/style.css`
- `test_lottery_tool.py`
- `tools/verify_lottery_ui.py`

## 数据库状态

实际库 `data/bili_ops.db` 已完成原位迁移：

```sql
SELECT name, type
FROM pragma_table_info('comments')
WHERE name IN ('level_info', 'vip');
```

结果包含 `level_info JSON`、`vip JSON`。迁移前已有 202 条评论、198 个独立 UID 均被保留；这些历史行原先没有相关数据，下一次参与抽奖时才按 UID 最小补齐并回写，避免主动全量请求。

抽样检查：

```sql
SELECT rpid, uid,
       json_extract(level_info, '$.current_level') AS level,
       json_extract(vip, '$.vipStatus') AS vip_status,
       json_extract(vip, '$.vipType') AS vip_type,
       ctime
FROM comments
WHERE level_info IS NOT NULL AND vip IS NOT NULL
LIMIT 20;
```

## 验证结果

- `pytest test_lottery_tool.py -q`: `9 passed`。
- 数据库迁移测试覆盖：列创建、Lv6、年度大会员、评论时间本地读取。
- Playwright 桌面 `1440x1000` 与移动 `390x844` 验收通过。
- 浏览器运行态检查：Lv6 红底、年度大会员粉底、完整时间框、暖色主题、鼠标光晕、无横向溢出均通过。
- 运行态证据：`evidence/lottery/verification.json`、`lottery_desktop.png`、`lottery_mobile.png`。

## 使用说明

无需新增依赖或环境变量。重启 Web/桌面程序后迁移自动幂等执行；对包含旧评论的目标执行一次抽奖，系统会仅按缺失 UID 补齐等级与会员资料并回写数据库，后续直接复用本地数据。
