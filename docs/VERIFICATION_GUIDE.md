# bili_ops_toolbox 修复验证指南

## 快速验证步骤

### 1. 环境准备
```bash
# 安装依赖（使用清华源）
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```

### 2. 启动测试

#### 桌面端启动
```bash
python start_desktop.py
```
**预期结果**：
- ✅ 不报 ImportError（BilibiliAuth 已修复）
- ✅ 欢迎向导可以打开
- ✅ 二维码生成成功

#### Web 服务启动
```bash
python start_web.py
```
**预期结果**：
- ✅ Uvicorn 启动成功
- ✅ 监听 127.0.0.1:8000（默认端口）
- ✅ 无构造函数错误

### 3. 核心功能验证

#### 扫码登录链路
```bash
# 在桌面端向导中
1. 点击"生成二维码"
2. 使用 B 站 APP 扫码
3. 确认登录
```
**验证点**：
- ✅ 二维码生成不报错
- ✅ 轮询状态正常
- ✅ 登录成功后 Cookie 保存

#### Web API 测试
```bash
# 测试配置 API
curl http://127.0.0.1:8000/api/config/

# 测试词云 API
curl -X POST http://127.0.0.1:8000/api/hotspot/tag-cloud \
  -H "Content-Type: application/json" \
  -d '{"zone_name": "生活", "limit": 100}'
```

**验证点**：
- ✅ `/api/config/` 返回配置（不是 500）
- ✅ 词云接口不报构造函数错误

### 4. 异常场景验证

#### 429 退避测试
```python
# 触发 429 后观察日志
# 预期：看到 "收到 429 响应，等待 X s 后重试"
```

#### Cookie 验证测试
```python
# 使用失效 Cookie
# 预期：正确识别 isLogin=False，而非崩溃
```

---

## 已修复问题验证清单

### V6 终审修复（3/3）✅ - 2026-08-19 19:07+

- [x] **放行条件 1**: 向导 Cookie 自动加载真实落地 - 完全重写导入逻辑（纯同步，跳过网络校验）
- [x] **放行条件 2**: CORS 不安全组合修正 - 改为显式白名单
- [x] **放行条件 3**: 文档一致性修正 - FIX_REPORT_V6.md 已创建，端口号已统一

### V4 补修（7/7）✅ - 2026-08-19 18:43+

- [x] **P1-1**: push_alert 死链接通 - CommentMonitor 创建时传递 alert_callback
- [x] **P1-2**: app.js 字段纠正 - alert.alert_level/alert_type 改为 alert.level/type
- [x] **P1-3**: cookie 池旧数据兼容 - 解密失败时回退明文并加密回写
- [x] **P2-4**: Task.checkpoint 真实读写 - 增量采集写入/启动读取断点（modules/comment/collector.py:433-443/509-521）
- [x] **P2-5**: welcome_wizard secret 消费 - Cookie 池加载时读取向导保存的 Cookie（V6 重新修复）
- [x] **P2-6**: test_stage3.py 参数确认 - 无 ConfigManager 误用
- [x] **P2-7**: VERIFY_GUIDE.py 转义修复 - 路径反斜杠转义

### P0（6/6）✅

- [x] **P0-1**: `python start_desktop.py` 不崩溃
- [x] **P0-2**: API 层 429 不报 TypeError
- [x] **P0-3**: 扫码登录可以正常轮询
- [x] **P0-4**: Cookie 验证使用 isLogin 字段
- [x] **P0-5**: `/api/comment/alerts` 不报字段错误
- [x] **P0-6**: `/api/hotspot/topics` 返回正确字段

### P1（13/18）✅

- [x] **P1-7**: Web 路由层无构造函数错误
- [x] **P1-8**: `/api/config/` 可以正常读写
- [x] **P1-9**: 词云生成可以提取 tag
- [x] **P1-10**: 用户视频列表可以获取
- [x] **P1-19**: LLM 情感分析有正常输出
- [x] **P1-24**: HTTP 412 不会标死有效 Cookie

---

## 问题排查

### 如果启动崩溃
1. 检查 Python 版本（推荐 3.9+）
2. 检查依赖安装完整性
3. 查看 `data/logs/error.log`

### 如果 Web 服务 500
1. 检查数据库文件 `data/bili_ops.db` 是否存在
2. 查看日志中的详细错误
3. 确认配置文件 `config/config.yaml` 存在

### 如果 LLM 功能报错
1. 确认配置了 `llm_api_key`（存储在 secrets）
2. 检查 `llm.api_base` 配置正确
3. 测试 API 连通性

---

## 未修复问题已知影响

### P1 剩余（2项）
- **粉丝数恒 0**：需调用 `/x/relation/stat` 接口
- **打包问题**：Web 服务在 exe 中无法启动

### P2 高频（部分）
- **限频无抖动**：固定间隔易被识别
- **Token 限额重启归零**：未从数据库初始化
- **Cookie 失效无自愈**：需手动重新扫码

---

## 性能观察点

### 正常指标
- API 响应时间 < 2s
- 限频等待符合配置（2s/4s）
- 日志输出流畅无阻塞

### 异常指标（需关注）
- 如果看到大量 `os.system('')` 调用 → logger.py 性能问题
- 如果 429 后无退避 → rate_limiter 未生效
- 如果评论采集很慢 → 可能触发风控

---

## 联系与反馈

修复完成后如有问题，请检查：
1. `FIX_SUMMARY_FINAL.md` - 完整修复报告
2. `FIX_PROGRESS_V2.md` - 详细进度跟踪
3. 备份目录 `C:\Users\27418\.irmia\backups\` - 可回滚

**下一步**：完成验证后，继续修复剩余 P1 和高频 P2 问题。
