"""
配置管理 API 路由

本模块提供 B站运营工具箱 Web 前端的配置管理接口，
负责 LLM 密钥、API 地址、模型名、每日 Token 限额等
运行时配置的读取、更新、用量统计与重载。
# 用新值覆盖旧值，保持数据一致

核心能力：
1. 配置读取（脱敏）：返回全量配置，但 api_key 只显示
# 将结果交回调用方
   前 8 位 + 星号 + 后 4 位，避免敏感信息泄漏到前端
2. LLM 配置专项：读写 llm.api_base / llm.model /
   llm.daily_token_limit，密钥单独走 save_secret 加密存储
3. 用量统计：按天聚合 LLMUsage 表的 token 消耗，
   返回每日明细 + 总量汇总，用于前端展示消耗趋势
   # 将结果交回调用方
4. 单键更新与重载：支持任意配置键的 PUT 更新，
# 用新值覆盖旧值，保持数据一致
   以及不重启进程的配置重载

设计约定：
- 所有写操作走 ConfigManager.save_config() 落盘
- 密钥类配置走 save_secret()，与普通配置分离存储
- 路由统一返回 {"success": bool, ...} 结构
# 将结果交回调用方
- 异常统一转 HTTPException(500)，避免堆栈泄漏

依赖：
- core.config.ConfigManager：配置读写与持久化
- core.database.get_session / LLMUsage：用量统计
"""
from fastapi import APIRouter, HTTPException
# 从 pydantic 导入符号
from pydantic import BaseModel
# 从 typing 导入符号
from typing import Optional, Dict, Any, List
# 从 datetime 导入符号
from datetime import datetime
# httpx 用于异步请求 OpenAI 兼容服务的模型列表
import httpx
# URL 处理工具用于规范化 /v1/models 请求路径
from urllib.parse import urljoin

# 从 core.config 导入符号
from core.config import ConfigManager
# 从 core.database 导入符号
from core.database import get_session, LLMUsage
# 从 sqlalchemy 导入符号
from sqlalchemy import func

# 独立的路由实例，由 web/main.py 挂载到 /api/config 前缀
router = APIRouter()


# ============ 请求/响应模型 ============

class LLMConfigRequest(BaseModel):
    """LLM 配置更新请求体
    # 用新值覆盖旧值，保持数据一致

    字段说明：
    - api_key: 必填，大模型 API 密钥，保存时走加密存储
    # 持久化数据，防止丢失
    - base_url: API 地址，默认 OpenAI 兼容地址
    - model: 模型名，默认 gpt-3.5-turbo
    - daily_token_limit: 每日 token 上限，可选
    """
    api_key: str
    # 赋值并准备后续使用
    base_url: str = "https://api.openai.com/v1"
    # 赋值并准备后续使用
    model: str = "gpt-3.5-turbo"
    # 赋值并准备后续使用
    daily_token_limit: Optional[int] = None


class LLMModelsRequest(BaseModel):
    """获取模型列表的请求体。

    前端切换 Base URL 时会传入当前输入框内容；如果字段为空，
    后端会回退读取已保存的 LLM 配置，便于直接调用接口探测。
    """
    api_key: Optional[str] = None
    # 允许前端传入尚未保存的新地址，避免必须先保存才能刷新模型
    base_url: Optional[str] = None


class ConfigUpdateRequest(BaseModel):
    """通用配置更新请求体
    # 用新值覆盖旧值，保持数据一致

    用于 PUT /api/config/ 单键更新场景，
    # 用新值覆盖旧值，保持数据一致
    key 为配置路径（支持点分路径如 llm.model），
    value 为任意 JSON 可序列化值。
    """
    key: str
    value: Any


# ============ 路由端点 ============

@router.get("/")
async def get_all_config():
    """获取所有配置（敏感信息脱敏）
    # 读取数据并赋值给当前作用域变量

    实现步骤：
    1. 实例化 ConfigManager 并读取 .all 全量配置
    2. 单独从 secrets 读取 llm_api_key，若存在则
       在返回字典里拼出 api_key_masked 脱敏字段
       # 将结果交回调用方
    3. 脱敏规则：前 8 位明文 + *** + 后 4 位明文

    返回结构：
    # 将结果交回调用方
    - success: 是否成功
    - config: 脱敏后的全量配置字典
    """
    try:
        # 赋值并准备后续使用
        config = ConfigManager()
        
        # 获取配置并脱敏 - 使用 .all 属性
        # .all 返回的是配置字典的浅拷贝，直接改它不会污染内部状态
        # 将结果交回调用方
        all_config = config.all
        
        # 脱敏处理 - 检查是否有 api_key（从 secrets 获取）
        # 注意：api_key 存在加密 secrets 里，不在普通配置中，
        # 所以必须单独 get_secret 取出来再拼进返回结构
        # 将结果交回调用方
        api_key = config.get_secret('llm_api_key')
        # 判断 api_key
        # 根据条件走向不同处理分支
        if api_key:
            # 前端展示需要 llm 节点存在，这里做惰性初始化
            if 'llm' not in all_config:
                # 赋值并准备后续使用
                all_config['llm'] = {}
            # 只暴露脱敏串，完整密钥永不返回给前端
            # 将结果交回调用方
            all_config['llm']['api_key_masked'] = api_key[:8] + '***' + api_key[-4:]
        
        return {
            "success": True,
            "config": all_config
        }
        
    except Exception as e:
        # 统一包装成 500，前端按 detail 展示错误信息
        # 将内容呈现到界面上
        raise HTTPException(status_code=500, detail=f"获取配置失败: {str(e)}")


@router.get("/llm")
async def get_llm_config():
    """获取LLM配置
    # 读取数据并赋值给当前作用域变量

    只返回 llm 命名空间下的配置项，
    # 将结果交回调用方
    同样会对 api_key 做脱敏处理，
    # 对数据进行加工/分发
    供前端设置页回显当前模型与地址。
    # 写入配置/属性，影响后续行为

    返回结构：
    # 将结果交回调用方
    - success: 是否成功
    - config: llm 配置字典（含 api_key_masked 脱敏字段）
    """
    try:
        # 赋值并准备后续使用
        config = ConfigManager()
        
        # 读取 llm 命名空间，不存在时返回空字典
        # 将结果交回调用方
        llm_config = config.get('llm', {})
        
        # 从 secrets 获取 api_key 并脱敏
        # get_secret 返回 None 表示未配置，跳过脱敏字段
        # 将结果交回调用方
        api_key = config.get_secret('llm_api_key')
        # 判断 api_key
        # 根据条件走向不同处理分支
        if api_key:
            # 赋值并准备后续使用
            llm_config['api_key_masked'] = api_key[:8] + '***' + api_key[-4:]
        
        return {
            "success": True,
            "config": llm_config
        }
        
    except Exception as e:
        # 抛出异常中断流程
        raise HTTPException(status_code=500, detail=f"获取LLM配置失败: {str(e)}")


@router.post("/llm/models")
async def get_llm_models(request: LLMModelsRequest):
    """从 OpenAI 兼容服务获取可用模型名称。

    参数：
        request: 可选的临时 API Key 与 Base URL；缺省时读取已保存配置。
    返回：
        success、models 和 message 字段。任何网络或响应格式错误都转换为
        可读的失败结果，不把第三方异常直接抛给前端。
    """
    try:
        # 优先使用前端当前输入值，保证切换地址后无需先保存配置。
        config = ConfigManager()
        # None 表示请求未提供字段，空字符串则表示用户明确未填写，不能回退旧密钥。
        api_key = (config.get_secret('llm_api_key') if request.api_key is None else request.api_key or '').strip()
        base_url = (config.get('llm.api_base', '') if request.base_url is None else request.base_url or '').strip()
        if not base_url:
            return {"success": False, "models": [], "message": "请先填写 Base URL"}
        if not api_key:
            return {"success": False, "models": [], "message": "请先填写 API Key"}

        # 兼容用户填写带或不带末尾斜杠的地址，并固定访问标准模型端点。
        models_url = urljoin(base_url.rstrip('/') + '/', 'models')
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(models_url, headers=headers)
        if response.status_code >= 400:
            # 不返回完整响应正文，避免第三方错误内容污染前端或泄露敏感信息。
            return {
                "success": False,
                "models": [],
                "message": f"模型列表请求失败（HTTP {response.status_code}）"
            }

        payload = response.json()
        raw_models = payload.get('data', []) if isinstance(payload, dict) else []
        models: List[str] = []
        for item in raw_models:
            # OpenAI 兼容响应通常是 [{"id": "model-name"}]。
            model_name = item.get('id') if isinstance(item, dict) else item
            if isinstance(model_name, str) and model_name.strip():
                models.append(model_name.strip())
        models = sorted(set(models))
        if not models:
            return {"success": False, "models": [], "message": "接口返回为空或格式不兼容"}
        return {"success": True, "models": models, "message": f"已获取 {len(models)} 个模型"}
    except httpx.TimeoutException:
        return {"success": False, "models": [], "message": "获取模型列表超时，请检查 Base URL"}
    except httpx.RequestError as exc:
        return {"success": False, "models": [], "message": f"无法连接模型服务：{exc}"}
    except ValueError:
        return {"success": False, "models": [], "message": "模型服务返回的不是有效 JSON"}
    except Exception as exc:
        # 兜底保护路由稳定性，同时保留足够的错误上下文帮助排查。
        return {"success": False, "models": [], "message": f"获取模型列表失败：{exc}"}


@router.post("/llm")
async def update_llm_config(request: LLMConfigRequest):
    """更新LLM配置
    # 用新值覆盖旧值，保持数据一致

    实现步骤：
    1. save_secret 持久化 api_key（加密存储）
    2. set 写入 api_base / model / daily_token_limit
    3. save_config 统一落盘

    注意：
    - api_key 走 secrets 存储，与普通配置物理隔离
    - base_url 存到 llm.api_base（点分路径，ConfigManager 支持）
    - daily_token_limit 可选，传了才写入
    """
    try:
        # 赋值并准备后续使用
        config = ConfigManager()
        
        # 更新配置 - 使用正确的键名
        # 密钥必须走 save_secret，不能混进普通配置明文存储
        config.save_secret('llm_api_key', request.api_key)
        config.set('llm.api_base', request.base_url)
        config.set('llm.model', request.model)
        
        # 每日限额可选，为空不覆盖旧值
        if request.daily_token_limit:
            config.set('llm.daily_token_limit', request.daily_token_limit)
        
        # 保存配置 - 使用 save_config()
        # 所有 set 只是改内存，必须 save_config 才落盘
        config.save_config()
        
        # ===== 关键：让运行中的进程立即用上新配置 =====
        # 根因：llm/client.py 持有模块级 config 单例 + 全局 LLMClient 单例，
        # 上面用的是新建的 ConfigManager() 实例写盘，旧单例内存里还是旧 model，
        # 导致前端保存成功但实际请求仍用旧模型（qwen3.7 之类）。
        # 处理：reload 模块级 config 单例 + 重置 LLMClient 全局单例，
        # 下次 get_llm_client() 会用新配置重建。
        from core.config import config as runtime_config
        runtime_config.reload()
        import llm.client as llm_client_module
        llm_client_module._global_llm_client = None
        
        return {
            "success": True,
            "message": "LLM配置已更新"
        }
        
    except Exception as e:
        # 抛出异常中断流程
        raise HTTPException(status_code=500, detail=f"更新配置失败: {str(e)}")


@router.get("/llm/usage")
# 赋值并准备后续使用
async def get_llm_usage(days: int = 7):
    """获取LLM用量统计
    # 读取数据并赋值给当前作用域变量

    查询最近 N 天的 LLMUsage 记录，返回：
    # 将结果交回调用方
    1. daily_usage：按自然日聚合的 token 明细
       （prompt/completion/total 各自求和 + 请求次数）
    2. total：整个时间窗的总 token 与总请求数

    实现细节：
    - 日期过滤用 created_at >= start_date，闭区间
    # 剔除不符合条件的数据
    - func.date() 按天截断时间戳做 group_by
    - 空值统一兜底为 0，避免前端渲染 None
    """
    try:
        # 赋值并准备后续使用
        session = get_session()
        
        # 查询最近N天的用量
        from datetime import timedelta
        # 计算结果存入 start_date
        # 对输入做运算得到结果
        start_date = datetime.now() - timedelta(days=days)
        
        # 按天统计
        # 用 func.date(created_at) 做分组键，天然按天聚合
        daily_usage = session.query(
            func.date(LLMUsage.created_at).label('date'),
            func.sum(LLMUsage.prompt_tokens).label('prompt_tokens'),
            func.sum(LLMUsage.completion_tokens).label('completion_tokens'),
            func.sum(LLMUsage.total_tokens).label('total_tokens'),
            func.count(LLMUsage.id).label('request_count')
        ).filter(
            LLMUsage.created_at >= start_date
        ).group_by(
            func.date(LLMUsage.created_at)
        ).all()
        
        # 总计
        # 与按天查询共用同一个时间窗过滤条件
        # 剔除不符合条件的数据
        total = session.query(
            func.sum(LLMUsage.total_tokens).label('total_tokens'),
            func.count(LLMUsage.id).label('total_requests')
        ).filter(
            LLMUsage.created_at >= start_date
        ).first()
        
        # 关闭连接释放资源
        # 释放连接/窗口资源
        session.close()
        
        # 格式化数据
        # SQL 聚合结果转成前端友好的 dict 列表
        usage_data = []
        # 遍历 daily_usage 逐项处理
        # 对集合内每个元素执行相同处理
        for row in daily_usage:
            # 追加到列表
            usage_data.append({
                # SQLite 的 func.date 返回 str，兼容 datetime、date 和脏字符串。
                'date': (row.date.isoformat() if isinstance(row.date, datetime)
                         else str(row.date)[:10] if row.date else None),
                'prompt_tokens': row.prompt_tokens or 0,
                'completion_tokens': row.completion_tokens or 0,
                'total_tokens': row.total_tokens or 0,
                'request_count': row.request_count or 0
            })
        
        return {
            "success": True,
            "days": days,
            "daily_usage": usage_data,
            # total 可能为 None（无记录），三元表达式兜底
            "total_tokens": total.total_tokens or 0 if total else 0,
            "total_requests": total.total_requests or 0 if total else 0
        }
        
    except Exception as e:
        # 抛出异常中断流程
        raise HTTPException(status_code=500, detail=f"获取用量统计失败: {str(e)}")


@router.put("/")
async def update_config(request: ConfigUpdateRequest):
    """更新单个配置项
    # 用新值覆盖旧值，保持数据一致

    通用配置写入入口，支持点分路径（如 llm.model），
    适合前端做单项设置的即时保存。
    # 写入配置/属性，影响后续行为

    注意：此接口不处理密钥，密钥请走 POST /llm。
    # 对数据进行加工/分发
    """
    try:
        # 赋值并准备后续使用
        config = ConfigManager()
        
        # set 支持点分路径自动创建嵌套结构
        config.set(request.key, request.value)
        # 落盘持久化
        config.save_config()
        
        return {
            "success": True,
            "message": f"配置 {request.key} 已更新"
        }
        
    except Exception as e:
        # 抛出异常中断流程
        raise HTTPException(status_code=500, detail=f"更新配置失败: {str(e)}")


@router.post("/reload")
async def reload_config():
    """重新加载配置
    # 从存储/网络读入数据

    从磁盘重新读取配置文件，适用于配置被外部
    修改后需要热生效的场景，无需重启 Web 服务。

    注意：reload 会丢弃内存中未保存的修改。
    # 持久化数据，防止丢失
    """
    try:
        # 赋值并准备后续使用
        config = ConfigManager()
        # 触发重新读取，内部会清缓存并重载
        config.reload()
        
        return {
            "success": True,
            "message": "配置已重新加载"
        }
        
    except Exception as e:
        # 抛出异常中断流程
        raise HTTPException(status_code=500, detail=f"重载配置失败: {str(e)}")