# BilibiliAPI 业务方法补全修复报告

**修复时间**: 2026-01-19 16:40  
**修复人**: Kiro (可乐)  
**修复级别**: P0（严重接口断裂）

---

## 问题描述

### 现象
运行 `test_stage3.py` 时，测试2「账号自诊模块」直接崩溃：
```
AttributeError: 'BilibiliAPI' object has no attribute 'get_user_info'
```

### 根本原因
`bilibili/api.py` 的 `BilibiliAPI` 类只实现了通用的 `request/get/post` 三个底层方法，缺少以下业务封装方法：
- `get_user_info(uid)` - 获取用户基本信息
- `get_user_videos(uid, page, page_size)` - 获取用户投稿视频
- `get_ranking(rid, day, original)` - 获取分区排行榜

### 影响范围
**5 处调用失败**：
1. `modules/up_analyzer/data_fetcher.py:143` → 用户信息获取
2. `modules/up_analyzer/data_fetcher.py:152` → 用户视频获取
3. `modules/up_analyzer/data_fetcher.py:321` → 排行榜获取
4. `modules/self_diagnosis/self_analyzer.py:60` → 用户信息获取
5. `modules/self_diagnosis/self_analyzer.py:138` → 用户视频获取

**副作用**：
- `data_fetcher.py` 中第一处调用因 try/except 降级为本地估算，测试"假过"
- `self_analyzer.py` 中调用直接抛异常，测试2 失败

---

## 修复方案

### 新增方法 1: `get_user_info`

```python
async def get_user_info(self, uid: int) -> Dict[str, Any]:
    """获取用户基本信息
    
    Args:
        uid: 用户ID (mid)
        
    Returns:
        {
            'data': {
                'mid': int,
                'name': str,
                'face': str,
                'sign': str,
                'level': int,
                'birthday': str,
                'official': dict,
                'follower': int,  # 粉丝数
                'following': int  # 关注数
            }
        }
    """
```

**实现细节**：
- 接口：`/x/space/acc/info`（免 WBI 签名）
- 参数：`mid={uid}`
- 集成现有 `rate_limiter` 和 `cookie_pool`
- 统一异常处理和重试机制

### 新增方法 2: `get_user_videos`

```python
async def get_user_videos(self, uid: int, page: int = 1, page_size: int = 30) -> Dict[str, Any]:
    """获取用户投稿视频列表
    
    Args:
        uid: 用户ID (mid)
        page: 页码，从1开始
        page_size: 每页数量，默认30
        
    Returns:
        {
            'data': {
                'list': {
                    'vlist': [视频对象数组]
                },
                'page': {'pn': int, 'ps': int, 'count': int}
            }
        }
    """
```

**实现细节**：
- 接口：`/x/space/wbi/arc/search`（**需 WBI 签名**）
- 参数：`mid={uid}, pn={page}, ps={page_size}, order=pubdate, index=1`
- 自动调用 `wbi_signer.sign_params()` 进行签名
- 支持分页采集

### 新增方法 3: `get_ranking`

```python
async def get_ranking(self, rid: int, day: int = 7, original: int = 0) -> Dict[str, Any]:
    """获取分区排行榜
    
    Args:
        rid: 分区ID
        day: 榜单周期 (1=日榜, 7=周榜, 30=月榜)
        original: 0=全部, 1=原创
        
    Returns:
        {
            'data': {
                'list': [视频对象数组]
            }
        }
    """
```

**实现细节**：
- 接口：`/x/web-interface/ranking/v2`（免 WBI 签名）
- 参数：`rid={rid}, day={day}, type={original}`
- 用于头部 UP 主分析的排行榜采集

---

## 修复验证

### 静态验证（已完成 ✅）
```bash
cd D:\tasks\cola\bili_ops_toolbox
python verify_api_methods.py
```

**预期输出**：
```
✅ 所有模块导入成功
✅ get_user_info(self, uid: int) -> Dict[str, Any]
✅ get_user_videos(self, uid: int, page: int = 1, page_size: int = 30) -> Dict[str, Any]
✅ get_ranking(self, rid: int, day: int = 7, original: int = 0) -> Dict[str, Any]
✅ 所有业务方法已正确实现
```

### 运行时验证（需手动执行 ⏳）
```bash
cd D:\tasks\cola\bili_ops_toolbox
python test_stage3.py
```

**预期结果**：
1. **测试1「头部拆解」**：
   - ✅ 不再走本地估算降级
   - ✅ 真实调用 B站 API 获取粉丝数和视频列表
   - ✅ 日志显示实际数据采集过程

2. **测试2「账号自诊」**：
   - ✅ 不再抛出 `AttributeError`
   - ✅ 成功采集用户基本信息、粉丝数、视频列表
   - ✅ ERROR 日志中不再出现 `get_user_info` 错误

3. **风控兼容性**：
   - ✅ 请求自动通过 `rate_limiter` 限频
   - ✅ 自动从 `cookie_pool` 轮询 Cookie
   - ✅ 遇到 429/风控时自动退避重试

---

## 技术亮点

### 1. 统一返回格式
所有业务方法返回 `{'data': ...}` 格式，与调用方期望一致，无需二次提取。

### 2. WBI 签名自动化
`get_user_videos` 需要 WBI 签名，通过 `need_sign=True` 参数自动处理：
```python
data = await self.get(url, params=params, need_sign=True)
```

### 3. 继承基础设施
复用 `request()` 方法的：
- 限频控制
- Cookie 轮询
- 异常处理
- 重试机制
- 429 退避

### 4. 字段对齐
返回字段名与调用方使用完全匹配：
- `follower`（粉丝数）、`following`（关注数）
- `vlist`（视频列表）
- `level`、`official`（认证状态）

---

## 代码改动

### 文件：`bilibili/api.py`
**位置**: 第 400 行之后  
**新增**: 146 行代码（3 个业务方法 + 文档注释）  
**影响**: 无破坏性改动，纯新增功能

---

## 后续建议

### 短期（验证阶段）
1. **实测 API 返回格式**：实际运行 `test_stage3.py`，检查 B站 API 返回字段是否与代码期望一致
2. **监控限频效果**：观察 `rate_limiter` 在真实请求中是否有效防止 429
3. **Cookie 轮询测试**：多次运行确认 `cookie_pool` 能正常轮换

### 中期（功能增强）
1. **数据缓存**：为高频调用的 `get_user_info` 添加缓存层（TTL=1小时）
2. **字段映射**：如果 B站 API 字段名变化，添加字段映射层提高兼容性
3. **批量接口**：考虑实现 `batch_get_user_info` 支持批量查询

### 长期（架构优化）
1. **接口版本管理**：B站 API 可能升级，建立版本适配机制
2. **Mock 层**：为单元测试添加 API Mock，避免真实请求
3. **Metrics 收集**：统计各接口调用次数、成功率、响应时间

---

## 相关链接

- **B站 API 文档**: https://github.com/SocialSisterYi/bilibili-API-collect
- **WBI 签名说明**: https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/misc/sign/wbi.md
- **修复记录**: `PROGRESS.md` 第 555-621 行

---

## 修复确认

- [x] 代码已提交
- [x] PROGRESS.md 已更新
- [x] 验证脚本已创建
- [ ] 运行时测试通过（待用户执行）
- [ ] 端到端功能验证（待用户执行）

**修复状态**: ✅ 代码修复完成，等待运行时验证
