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
from fastapi import APIRouter, HTTPException, Query
# 从 pydantic 导入符号
from pydantic import BaseModel, Field, StrictInt, field_validator
# 从 typing 导入符号
from typing import Optional, Dict, Any, List
# 从 datetime 导入符号
from datetime import datetime
# copy 用于在响应副本上脱敏，绝不写回活配置（#88）
import copy
# ipaddress 用于识别云元数据 / link-local 地址（#81）
import ipaddress
# asyncio 用于异步解析域名，校验解析结果；re 用于识别非标准数字 IP 写法
import asyncio
import re
# httpx 用于异步请求 OpenAI 兼容服务的模型列表
import httpx
# URL 处理工具用于规范化 /v1/models 请求路径
from urllib.parse import urljoin, urlsplit, urlunsplit

# 从 core.config 导入符号
from core.config import (
    ConfigManager,
    LLM_DAILY_TOKEN_LIMIT_MAX,
    LLM_DAILY_TOKEN_LIMIT_MIGRATED_KEY,
    LLM_MAX_TOKENS_MAX,
)
from core.logger import get_logger
# 从 core.database 导入符号
from core.database import get_session, LLMUsage
# 从 sqlalchemy 导入符号
from sqlalchemy import func

# 独立的路由实例，由 web/main.py 挂载到 /api/config 前缀
router = APIRouter()
logger = get_logger(__name__)

# 不允许经通用单键接口 PUT /api/config/ 写入的配置路径：
# LLM 地址与已保存密钥是绑定关系，改地址必须走 POST /llm 同时提交密钥，
# 否则调用方可先改 api_base 再借 /llm/models 把已保存密钥发往任意地址。
_PROTECTED_CONFIG_KEYS = {"llm", "llm.api_base", "llm.api_key"}

# 全局每日 token 上限的配置键；经 PUT /api/config/ 写入时做取值校验。
_DAILY_LIMIT_KEY = "llm.daily_token_limit"
# 写上限时一并写入迁移标记：用户主动保存的值（包括 100000）以后不再被当作旧默认值迁移。
_DAILY_LIMIT_MIGRATED_KEY = f"llm.{LLM_DAILY_TOKEN_LIMIT_MIGRATED_KEY}"


def _valid_daily_limit(value: Any) -> bool:
    """每日上限只接受 [0, MAX] 的整数（0 表示不限制），拒绝布尔、小数和字符串。"""
    return (not isinstance(value, bool) and isinstance(value, int)
            and 0 <= value <= LLM_DAILY_TOKEN_LIMIT_MAX)


# 单次输出上限的配置键：没有显式传 max_tokens 的 AI 功能都按它限制输出长度。
_MAX_TOKENS_KEY = "llm.max_tokens"


def _valid_max_tokens(value: Any) -> bool:
    """单次输出上限只接受 [1, MAX] 的整数。0 会让请求必然失败，所以不允许。"""
    return (not isinstance(value, bool) and isinstance(value, int)
            and 1 <= value <= LLM_MAX_TOKENS_MAX)


def _normalize_base_url(url: str) -> str:
    """规范化 Base URL，用于判断请求地址是否就是已保存的地址。

    只消除书写差异（scheme/host 大小写、末尾斜杠、fragment），
    端口、userinfo、路径等任何实质差异都视为不同地址。
    """
    parts = urlsplit(url.strip())
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.rstrip('/'),
        parts.query,
        '',
    ))


# ============ 外部地址校验与脱敏（#81 / #88） ============

# 模型列表探测只允许 http/https：其他协议（file://、gopher://、dict:// 等）
# 在部分客户端实现里能读取本地文件或触发奇怪协议行为。
_ALLOWED_SCHEMES = {"http", "https"}

# 云厂商元数据服务地址：一旦被当成 Base URL，服务端会带着自己的身份去访问它，
# 属于典型的 SSRF 目标，直接拒绝（含阿里云 100.100.100.200 与常见别名）。
_METADATA_HOSTNAMES = {
    "169.254.169.254",
    "100.100.100.200",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
}

# 元数据服务的 IP 形式：按地址比较而不是按字符串，避免大小写 / 压缩写法绕过。
# fd00:ec2::254 是 AWS 的 IPv6 元数据端点。
_METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(item) for item in ("169.254.169.254", "100.100.100.200", "fd00:ec2::254")
)

# 低 32 位就是目标 IPv4 的 IPv6 网段：NAT64 知名前缀与本地前缀（RFC 6052 / RFC 8215），
# 以及 SIIT 的 IPv4 转换地址 ::ffff:0:a.b.c.d（RFC 2765）。
_EMBEDDED_LOW32_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
    ipaddress.ip_network("::ffff:0:0:0/96"),
)

# inet_aton 接受的单段数字写法：十进制、0 开头八进制、0x 开头十六进制。
_NUMERIC_LABEL_RE = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)")

# IPv4/IPv6 的 link-local 网段（169.254.0.0/16、fe80::/10）。
# 注意：这里刻意不封 127.0.0.1 和私网段——本地 Ollama / LM Studio / 局域网模型服务
# 是合法用法，且前端允许传入尚未保存的新地址来刷新模型列表。
_LINK_LOCAL_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
)


def _validate_models_url(base_url: str) -> str:
    """校验外部模型服务的 Base URL，并拼出标准的 /models 端点（#81）。

    校验规则：
    - 只允许 http / https；
    - 拒绝 URL 内嵌用户名/密码（避免把凭据塞进请求地址）；
    - 拒绝云元数据服务地址（169.254.169.254、metadata.google.internal 等）；
    - 拒绝 IPv4/IPv6 link-local 与未指定地址（0.0.0.0、::）。

    环回与私网地址照常放行：本地 / 局域网模型服务是合法用法；本接口只接受本机同源
    请求，且使用已保存密钥时 base_url 必须等于已保存地址（见 get_llm_models）。

    返回：
        拼接好的 /models 请求地址。

    异常：
        HTTPException(400)：地址不被允许。
    """
    parts = urlsplit(base_url.strip())

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise HTTPException(status_code=400, detail="Base URL 只支持 http/https 地址")

    # urlsplit 对 userinfo 不做解码，直接用原始 netloc 判断 '@' 更稳妥
    if parts.username is not None or parts.password is not None or '@' in parts.netloc:
        raise HTTPException(status_code=400, detail="Base URL 不能包含用户名或密码")

    hostname = (parts.hostname or '').strip().lower().rstrip('.')
    if not hostname:
        raise HTTPException(status_code=400, detail="Base URL 缺少主机名")

    if hostname in _METADATA_HOSTNAMES:
        raise HTTPException(status_code=400, detail="Base URL 指向云服务元数据地址，已拒绝")

    try:
        address = ipaddress.ip_address(hostname.split('%', 1)[0])
    except ValueError:
        address = None
    if address is None and _looks_like_numeric_ipv4(hostname):
        # 2852039166 / 0xa9fea9fe / 0251.0376.0251.0376 / 169.254.43518 这类写法
        # 标准库判不出来，但 inet_aton 会把它们解析成 169.254.169.254。
        raise HTTPException(status_code=400, detail="Base URL 使用了非标准的 IP 写法，请改用点分十进制")
    if address is not None:
        reason = _blocked_address_reason(address)
        if reason:
            raise HTTPException(status_code=400, detail=reason)

    # 兼容用户填写带或不带末尾斜杠的地址，并固定访问标准模型端点。
    return urljoin(parts.geturl().rstrip('/') + '/', 'models')


def _is_ip_literal(hostname: str) -> bool:
    """主机名本身是不是一个 IP 字面量（含 IPv6 zone id 写法）。"""
    try:
        ipaddress.ip_address(hostname.split('%', 1)[0])
    except ValueError:
        return False
    return True


def _looks_like_numeric_ipv4(hostname: str) -> bool:
    """主机名的每一段都是十进制 / 八进制 / 十六进制数字时视为 IPv4 写法。"""
    labels = hostname.split('.')
    return 1 <= len(labels) <= 4 and all(_NUMERIC_LABEL_RE.fullmatch(label) for label in labels)


def _embedded_ipv4(address: "ipaddress.IPv6Address") -> List["ipaddress.IPv4Address"]:
    """取出 IPv6 地址里内嵌的 IPv4（映射、兼容、6to4、NAT64、Teredo）。"""
    found = []
    if address.ipv4_mapped is not None:
        found.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        found.append(address.sixtofour)
    if address.teredo is not None:
        found.append(address.teredo[1])
    packed = int(address)
    low32 = ipaddress.IPv4Address(packed & 0xFFFFFFFF)
    if any(address in network for network in _EMBEDDED_LOW32_NETWORKS):
        found.append(low32)
    # IPv4 兼容地址 ::a.b.c.d（已废弃但仍可能被解析），排除 :: 与 ::1。
    if packed >> 32 == 0 and packed > 1:
        found.append(low32)
    return found


def _blocked_address_reason(address) -> Optional[str]:
    """判断单个 IP 是否属于禁止访问的地址；允许时返回 None。"""
    candidates = [address]
    if address.version == 6:
        candidates.extend(_embedded_ipv4(address))
    for item in candidates:
        if item in _METADATA_ADDRESSES:
            return "Base URL 指向云服务元数据地址，已拒绝"
        if item.is_unspecified:
            return "Base URL 指向无效地址，已拒绝"
        if any(item in network for network in _LINK_LOCAL_NETWORKS if network.version == item.version):
            return "Base URL 指向 link-local 地址，已拒绝"
    return None


async def _resolve_host(hostname: str):
    """异步解析主机名（单独成函数，便于测试替换）。"""
    return await asyncio.get_running_loop().getaddrinfo(hostname, None)


async def _reject_blocked_resolution(base_url: str) -> None:
    """域名解析后再查一次：``169.254.169.254.nip.io`` 这类域名会解析到元数据地址。

    解析失败不在这里报错，交给随后的真实请求按“无法连接”处理。

    已知残余风险：这里解析一次、httpx 发请求时再解析一次，DNS 重绑定可以让两次结果不同。
    本接口只接受本机同源请求，且要求调用方提供 API Key，这层检查只防“填错地址”和低成本绕过。
    """
    hostname = (urlsplit(base_url.strip()).hostname or '').strip().rstrip('.')
    if not hostname or _is_ip_literal(hostname):
        return  # 字面量 IP 已在 _validate_models_url 里检查过
    try:
        infos = await _resolve_host(hostname)
    except (OSError, UnicodeError):
        return
    for info in infos:
        try:
            resolved = ipaddress.ip_address(str(info[4][0]).split('%', 1)[0])
        except ValueError:
            continue
        reason = _blocked_address_reason(resolved)
        if reason:
            raise HTTPException(status_code=400, detail=f"{reason}（域名解析结果）")


def _mask_api_key(value: Any) -> Optional[str]:
    """把密钥脱敏成前端展示用字符串；没有密钥时返回 None（不生成字段）。

    规则：长度 > 12 时保留前 8 位与后 4 位；更短的密钥整体按固定长度打码，
    避免旧规则（前 8 + 后 4）把短密钥完整拼出来（#88）。
    """
    if not isinstance(value, str):
        return None
    key = value.strip()
    if not key:
        return None
    if len(key) <= 12:
        return '*' * 8
    return key[:8] + '***' + key[-4:]


# ============ 请求/响应模型 ============

class LLMConfigRequest(BaseModel):
    """LLM 配置更新请求体
    # 用新值覆盖旧值，保持数据一致

    字段说明：
    - api_key: 必填，大模型 API 密钥，保存时走加密存储；空白视为非法（不是“清空密钥”）
    - base_url: API 地址，省略则保留已保存地址
    - model: 模型名，省略则保留已保存模型
    - daily_token_limit: 每日 token 上限，可选

    api_key 保持必填：改地址必须同时提供密钥，不能把已保存密钥与新地址拼接
    （与 PUT /config/ 拒写 llm.api_base 的约束一致）。
    base_url/model 不再给 OpenAI 默认值（#91）：否则“只换密钥”的请求会把
    已保存的地址与模型悄悄重置回默认值。
    """
    api_key: str
    base_url: Optional[str] = None
    model: Optional[str] = None
    # 全局每日 token 上限：0 表示不限制；None 表示不修改
    daily_token_limit: Optional[StrictInt] = Field(
        default=None, ge=0, le=LLM_DAILY_TOKEN_LIMIT_MAX)

    @field_validator('api_key', 'base_url', 'model')
    @classmethod
    def _strip_text_fields(cls, value: Optional[str]) -> Optional[str]:
        """统一去首尾空白；显式传空串视为非法，而不是“清空/重置”（#91）。"""
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError('字段不能为空字符串；不修改请省略该字段')
        return value


class LLMModelsRequest(BaseModel):
    """获取模型列表的请求体。

    前端切换 Base URL 时会传入当前输入框内容。字段省略（None）时后端回退读取已保存的
    LLM 配置；空字符串表示用户明确没填，不回退（#87：前端在输入框为空时省略 api_key）。
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

    @field_validator('key')
    @classmethod
    def _normalize_key(cls, value: str) -> str:
        """规范化点分路径：每段去空白，任一段为空即 422（#98）。

        旧实现只用 strip 后的键做校验、却用原始键写入，`" llm"` 会写出
        顶层垃圾键且 `llm.*` 的重置判定失效。
        """
        key = (value or '').strip()
        if not key:
            raise ValueError('配置键不能为空')
        segments = [segment.strip() for segment in key.split('.')]
        if any(not segment for segment in segments):
            raise ValueError('配置键的每一段都不能为空')
        return '.'.join(segments)


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
        
        # 取配置快照：.all 已改为深拷贝，这里再 copy 一次只是显式表明
        # “下面所有改动都只作用于响应副本”（#88）
        all_config = copy.deepcopy(config.all)
        
        # 脱敏处理 - 检查是否有 api_key（从 secrets 获取）
        # 注意：api_key 存在加密 secrets 里，不在普通配置中，
        # 所以必须单独 get_secret 取出来再拼进返回结构
        llm_node = all_config.get('llm')
        has_llm_node = isinstance(llm_node, dict)
        # 历史版本曾把明文写进 llm.api_key，这里连同一并剔除，响应里只留脱敏串（#88）
        legacy_key = llm_node.pop('api_key', None) if has_llm_node else None
        if has_llm_node:
            # 避免上一次污染留下的字段被原样带出
            llm_node.pop('api_key_masked', None)
        masked = _mask_api_key(config.get_secret('llm_api_key') or legacy_key)
        if masked:
            if not has_llm_node:
                # 前端展示需要 llm 节点存在，这里做惰性初始化（只作用于副本）
                llm_node = {}
                all_config['llm'] = llm_node
            # 只暴露脱敏串，完整密钥永不返回给前端
            llm_node['api_key_masked'] = masked
        
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
    供前端配置页回显当前模型与地址。
    # 写入配置/属性，影响后续行为

    返回结构：
    # 将结果交回调用方
    - success: 是否成功
    - config: llm 配置字典（含 api_key_masked 脱敏字段）
    """
    try:
        # 赋值并准备后续使用
        config = ConfigManager()
        
        # 读取 llm 命名空间的副本（#88）：必须在副本上脱敏，
        # 旧实现的 config.get('llm', {}) 是活引用，写 api_key_masked 会污染活配置。
        llm_config = copy.deepcopy(config.get('llm', {}) or {})
        if not isinstance(llm_config, dict):
            llm_config = {}
        
        # 从 secrets 获取 api_key 并脱敏；历史明文 llm.api_key 也只留脱敏串
        legacy_key = llm_config.pop('api_key', None)
        llm_config.pop('api_key_masked', None)
        masked = _mask_api_key(config.get_secret('llm_api_key') or legacy_key)
        if masked:
            llm_config['api_key_masked'] = masked
        
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
        saved_base_url = (config.get('llm.api_base', '') or '').strip()
        base_url = (saved_base_url if request.base_url is None else request.base_url or '').strip()
        if not base_url:
            return {"success": False, "models": [], "message": "请先填写 Base URL"}

        if request.api_key is None:
            # None 表示请求未提供密钥，回退已保存密钥；此时只允许访问已保存的地址，
            # 防止调用方指定任意 base_url 把已保存密钥随 Authorization 头发出去。
            # 前端在输入框为空时省略 api_key 字段（#87），走的就是这条回退路径。
            api_key = (config.get_secret('llm_api_key') or '').strip()
            if api_key and _normalize_base_url(base_url) != _normalize_base_url(saved_base_url):
                raise HTTPException(
                    status_code=400,
                    detail="使用已保存的 API Key 时只能查询已保存的 Base URL；切换地址请同时填写 API Key",
                )
        else:
            # 空字符串表示用户明确未填写，不能回退旧密钥。
            api_key = request.api_key.strip()
        if not api_key:
            return {"success": False, "models": [], "message": "请先填写 API Key"}

        # 地址校验（#81）：只允许 http/https，拒绝内嵌凭据、云元数据地址、
        # link-local 与未指定地址；本地 / 局域网模型服务照常可用。
        models_url = _validate_models_url(base_url)
        await _reject_blocked_resolution(base_url)
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        # 禁止跟随重定向：否则公开地址可以 302 到元数据地址，绕过上面的地址校验。
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.get(models_url, headers=headers)
        if response.status_code >= 300:
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
    except HTTPException:
        # 参数校验类拒绝需原样返回状态码，不能被下方兜底转成 success=False 的 200
        raise
    except httpx.TimeoutException as exc:
        logger.warning("获取模型列表超时: %s", exc, exc_info=True)
        return {"success": False, "models": [], "message": "获取模型列表超时，请检查 Base URL"}
    except httpx.RequestError as exc:
        logger.warning("连接模型服务失败: %s", exc, exc_info=True)
        return {"success": False, "models": [], "message": f"无法连接模型服务：{exc}"}
    except ValueError as exc:
        logger.warning("模型服务返回非法 JSON: %s", exc, exc_info=True)
        return {"success": False, "models": [], "message": "模型服务返回的不是有效 JSON"}
    except Exception as exc:
        # 兜底保护路由稳定性，同时保留足够的错误上下文帮助排查。
        logger.error("获取模型列表失败: %s", exc, exc_info=True)
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
        
        # 密钥必填并总是写入；base_url/model 只写请求里真正提供的（#91）：
        # 旧实现给这两个字段填了 OpenAI 默认值并总是写入，导致“只想换密钥”
        # 的请求把已保存的地址与模型一起重置掉。
        config.save_secret('llm_api_key', request.api_key)
        if request.base_url is not None:
            config.set('llm.api_base', request.base_url)
        if request.model is not None:
            config.set('llm.model', request.model)
        
        # 每日限额可选：未传不覆盖旧值；0 是合法值（不限制），不能用真值判断
        if request.daily_token_limit is not None:
            config.set('llm.daily_token_limit', request.daily_token_limit)
            # 用户主动设置过：写迁移标记，防止下次启动把主动保存的 100000 当旧默认值迁移掉
            config.set(_DAILY_LIMIT_MIGRATED_KEY, 1)
        
        # 保存配置 - 使用 save_config()
        # 所有 set 只是改内存，必须 save_config 才落盘
        config.save_config()
        
        # ===== 关键：让运行中的进程立即用上新配置 =====
        # 根因：llm/client.py 持有模块级 config 单例 + 全局 LLMClient 单例，
        # 上面用的是新建的 ConfigManager() 实例写盘，旧单例内存里还是旧 model，
        # 导致前端保存成功但实际请求仍用旧模型（qwen3.7 之类）。
        # 处理：reload 模块级 config 单例 + 重置 LLMClient 全局单例并关闭旧连接，
        # 下次 get_llm_client() 会用新配置重建（热点、评论模块都委托该单例）。
        from core.config import config as runtime_config
        runtime_config.reload()
        from llm.client import reset_llm_client
        await reset_llm_client()
        
        return {
            "success": True,
            "message": "LLM配置已更新"
        }
        
    except Exception as e:
        # 抛出异常中断流程
        raise HTTPException(status_code=500, detail=f"更新配置失败: {str(e)}")


@router.get("/llm/usage")
# 赋值并准备后续使用
async def get_llm_usage(days: int = Query(default=7, ge=1, le=365)):
    """获取LLM用量统计
    # 读取数据并赋值给当前作用域变量

    查询最近 N 天的 LLMUsage 记录（N 限定 1..365，避免 timedelta 溢出或空窗口），返回：
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
    except Exception as e:
        # 获取连接失败与查询失败同样落 500，避免半途抛出未包装异常。
        raise HTTPException(status_code=500, detail=f"获取用量统计失败: {str(e)}")

    try:
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

        # 今日用量与上限（与每日限额检查同口径），供配置页显示“今日已用 / 上限”
        today_usage = _today_usage(session)
        
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
            "total_requests": total.total_requests or 0 if total else 0,
            "today": today_usage,
        }

    except Exception as e:
        # 抛出异常中断流程
        raise HTTPException(status_code=500, detail=f"获取用量统计失败: {str(e)}")
    finally:
        # 连接必须释放：查询抛错（含上方 500 分支）时也不能漏掉 close。
        session.close()


def _today_usage(session) -> Dict[str, Any]:
    """今日全局用量与上限，口径与 LLMClient 的每日限额检查一致。

    当日用量用限额检查同一个按日期字符串取记录的函数（不按 created_at），
    上限按 LLMClient 同样的规整规则读取；上限为 0 时 remaining 为 None，表示不限制。
    不新建 LLMClient（构造要求已配置密钥）。
    """
    from datetime import date
    from llm.client import _coerce_daily_limit, _remaining, _usage_records_on
    from core.config import LLM_DAILY_TOKEN_LIMIT_DEFAULT

    limit = _coerce_daily_limit(
        ConfigManager().get('llm.daily_token_limit', LLM_DAILY_TOKEN_LIMIT_DEFAULT))
    used = sum(int(r.total_tokens or 0) for r in _usage_records_on(session, date.today()))
    return {"total_tokens": used, "limit": limit, "remaining": _remaining(limit, used)}


@router.put("/")
async def update_config(request: ConfigUpdateRequest):
    """更新单个配置项
    # 用新值覆盖旧值，保持数据一致

    通用配置写入入口，支持点分路径（如 llm.model），
    适合前端做单项设置的即时保存。
    # 写入配置/属性，影响后续行为

    注意：此接口不处理密钥，密钥请走 POST /llm。
    # 对数据进行加工/分发
    LLM 地址（llm / llm.api_base / llm.api_key）与 secrets.* 拒绝写入，
    避免绕过 POST /llm 把已保存密钥与新地址拼接。
    """
    # ConfigUpdateRequest 已在模型层规范化点分路径（每段 strip、任一段为空即 422），
    # 这里直接使用规范化后的键，校验、写入与重置判定必须同一个键（#98）。
    key = request.key
    if key in _PROTECTED_CONFIG_KEYS or key.split('.')[0] == 'secrets':
        raise HTTPException(status_code=400, detail="LLM 地址与密钥请通过 POST /api/config/llm 修改")
    if key == _DAILY_LIMIT_KEY and not _valid_daily_limit(request.value):
        raise HTTPException(
            status_code=422,
            detail=f"每日 token 上限必须是 0 到 {LLM_DAILY_TOKEN_LIMIT_MAX} 之间的整数（0 表示不限制）",
        )
    if key == _MAX_TOKENS_KEY and not _valid_max_tokens(request.value):
        raise HTTPException(
            status_code=422,
            detail=f"单次输出上限必须是 1 到 {LLM_MAX_TOKENS_MAX} 之间的整数",
        )
    try:
        # 赋值并准备后续使用
        config = ConfigManager()
        
        # set 支持点分路径自动创建嵌套结构
        if key == _DAILY_LIMIT_KEY:
            config.set(key, request.value)
            # 用户主动设置过：写迁移标记，防止下次启动把主动保存的 100000 当旧默认值迁移掉
            config.set(_DAILY_LIMIT_MIGRATED_KEY, 1)
        else:
            # 一律写规范化后的键：旧实现用未 strip 的原始键，" llm" 会生成顶层垃圾键
            config.set(key, request.value)
        # 落盘持久化
        config.save_config()
        # 同 update_llm_config：上面用新建实例写盘，运行中的模块级单例需 reload 才能读到新值
        from core.config import config as runtime_config
        runtime_config.reload()
        # llm.* 会影响全局 LLMClient 构造时固定的地址、模型与请求头，需丢弃后按新配置重建
        if key.split('.')[0] == 'llm':
            from llm.client import reset_llm_client
            await reset_llm_client()
        
        return {
            "success": True,
            "message": f"配置 {key} 已更新"
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
        # 重载运行中的模块级配置单例；新建实例再 reload 不会影响任何调用方
        from core.config import config as runtime_config
        # 触发重新读取，内部会清缓存并重载
        runtime_config.reload()
        # 磁盘上的 llm.* 可能已被外部修改，重建全局 LLMClient 以用上新配置
        from llm.client import reset_llm_client
        await reset_llm_client()
        
        return {
            "success": True,
            "message": "配置已重新加载"
        }
        
    except Exception as e:
        # 抛出异常中断流程
        raise HTTPException(status_code=500, detail=f"重载配置失败: {str(e)}")