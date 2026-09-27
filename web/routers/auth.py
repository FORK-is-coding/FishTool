"""
B站登录态管理 API 路由

本模块为 Web 前端提供 B站账号登录态管理接口，包含四个端点：

1. GET  /api/auth/status   查询当前登录态（本地 Cookie 是否存在 +
                           nav 接口实测是否有效），返回用户名/UID
2. GET  /api/auth/qrcode   生成登录二维码（B站 passport 官方接口），
                           返回 qrcode_key 与 base64 图片
3. POST /api/auth/poll     按 qrcode_key 单次查询扫码状态；
                           code==0 时提取 Cookie、加密落盘、
                           nav 验证通过后才报"登录成功"
4. POST /api/auth/logout   清除本地加密存储的登录态

与桌面端扫码向导的关键区别（保证扫码成功率的设计）：
- poll 是"单次查询"：一次请求只调一次 B站 poll 接口，绝不在后端
  循环等待。轮询节奏由前端 setInterval 控制（2 秒一次），前端
  同时维护 180 秒本地倒计时。
- 这绕开了旧版 bilibili/auth.py 的 poll_login_status 内部
  while 循环 2 秒后抛"登录超时，二维码已过期"异常、导致 UI 把
  "本地轮询超时"误判成"B站 86038 真过期"的根因问题。
- 只有 B站返回 86038 或前端 180 秒倒计时归零，才提示二维码过期；
  86101/86090 正常推进，网络异常只记日志不打断扫码。
- 登录成功三重校验：code==0 -> SESSDATA 提取非空 -> nav 接口
  isLogin=True。任何一步不满足都不报成功，杜绝"假完成"。

依赖：
- bilibili.auth.QRCodeLogin / CookieLoginHelper
- core.config.ConfigManager（加密存储）
"""
from fastapi import APIRouter, HTTPException
# 从 pydantic 导入符号
from pydantic import BaseModel
# 导入模块


# 从 core.config 导入符号
from core.config import ConfigManager
# 从 core.logger 导入符号
from core.logger import get_logger
# 从 bilibili.auth 导入符号
from bilibili.auth import QRCodeLogin, CookieLoginHelper
# 从 bilibili.api 导入符号
from bilibili.api import BilibiliAPI

# 独立的路由实例，由 web/main.py 挂载到 /api/auth 前缀
router = APIRouter()

logger = get_logger(__name__)

# B站 Cookie 在 ConfigManager secrets 中的键名（与桌面端向导共用同一份）
COOKIE_SECRET_KEY = 'bilibili.cookie'


class QrPollRequest(BaseModel):
    """扫码状态轮询请求体

    字段说明：
    - qrcode_key: 生成二维码时返回的密钥，B站 poll 接口必需
    """
    qrcode_key: str


def _mask_cookie(cookie: str) -> str:
    """脱敏 Cookie 字符串

    只向前端暴露 SESSDATA 的前 4 位与后 4 位，
    避免完整登录态泄漏到浏览器侧。
    """
    if not cookie:
        return ''
    parts = []
    # 遍历 Cookie 键值对，SESSDATA 值做中间打码，其余只留键名
    for item in cookie.split(';'):
        item = item.strip()
        # 边界/有效性检查
        if '=' in item:
            k, v = item.split('=', 1)
            # SESSDATA 足够长才打码，短值一律隐藏
            if k.strip() == 'SESSDATA' and len(v) > 8:
                parts.append(f"SESSDATA={v[:4]}***{v[-4:]}")
            else:
                parts.append(k.strip())
    return '; '.join(parts) if parts else '已登录'


@router.get("/status")
async def get_auth_status():
    """查询 B站登录态状态

    实现步骤：
    1. 从加密存储读取 bilibili.cookie，不存在则直接返回未登录
    2. 存在则调用 nav 接口实测有效性（isLogin）
    3. 有效时返回用户信息（UID/昵称/头像/等级）与脱敏 Cookie
    4. 失效时不删除 Cookie，返回 logged_in=False + invalid=True，
       前端提示"本地Cookie已失效，请重新扫码"

    返回结构：
    - success: 是否成功
    - logged_in: 是否已登录
    - user: 用户信息字典（未登录为 None）
    - cookie_masked: 脱敏后的 Cookie（未登录为空串）
    """
    try:
        config = ConfigManager()
        cookie = config.get_secret(COOKIE_SECRET_KEY)

        # 没有存 Cookie：直接未登录
        if not cookie:
            return {"success": True, "logged_in": False, "user": None, "cookie_masked": ""}

        # 实测 Cookie 有效性（nav 接口 isLogin）
        valid = await CookieLoginHelper.validate_cookie(cookie)
        if not valid:
            logger.warning("[auth] 本地Cookie已失效，需重新扫码")
            return {"success": True, "logged_in": False, "user": None,
                    "cookie_masked": "", "invalid": True}

        # 拉取用户信息
        user = await CookieLoginHelper.get_user_info(cookie)
        return {
            "success": True,
            "logged_in": True,
            "user": user,
            "cookie_masked": _mask_cookie(cookie),
        }
    except Exception as e:
        logger.error(f"[auth] 查询登录态失败: {e}")
        raise HTTPException(status_code=500, detail=f"查询登录态失败: {str(e)}")


@router.get("/qrcode")
async def get_qrcode():
    """生成 B站登录二维码

    实现步骤：
    1. 调用 B站 passport 官方接口申请二维码
    2. 返回 qrcode_key 与 base64 图片，前端负责展示与轮询

    返回结构：
    - qrcode_key: 轮询必需
    - qrcode_base64: 二维码 PNG 的 base64 字符串（data 部分）
    - qrcode_url: 二维码内容链接（调试用）
    """
    try:
        qr = QRCodeLogin()
        qr_url, qr_img = await qr.generate_qrcode()
        return {
            "success": True,
            "qrcode_key": qr.qrcode_key,
            "qrcode_url": qr_url,
            "qrcode_base64": qr.get_qrcode_image_base64(qr_img),
        }
    except Exception as e:
        logger.error(f"[auth] 生成二维码失败: {e}")
        raise HTTPException(status_code=500, detail=f"生成二维码失败: {str(e)}")


@router.post("/poll")
async def poll_login(request: QrPollRequest):
    """单次查询扫码登录状态

    核心设计（保证扫码成功率）：
    - 本接口一次请求只调一次 B站 poll 接口，不做任何循环等待；
      轮询节奏由前端 setInterval 控制，前端同时维护 180 秒倒计时
    - 旧版 poll_login_status 内部 while 循环 2 秒后抛超时异常，
      导致 UI 把"本地轮询超时"误判成"B站二维码过期"；本实现彻底
      绕开该路径，只有 B站返回 86038 或前端倒计时归零才算过期

    登录成功三重校验：
    1. B站返回 code == 0
    2. 从响应 url 提取到非空 Cookie（必须含 SESSDATA）
    3. nav 接口实测 isLogin == True

    返回结构：
    - status: confirmed / not_scanned / scanned / expired / error
    - message: 给用户看的提示文案
    - user: 登录成功时的用户信息（其余状态为 None）
    """
    qrcode_key = (request.qrcode_key or "").strip()
    if not qrcode_key:
        raise HTTPException(status_code=400, detail="缺少qrcode_key")

    try:
        # 统一由 BilibiliAPI 发起请求，同时保留 Set-Cookie 响应头。
        async with BilibiliAPI() as api:
            raw, set_cookie_headers = await api.poll_qrcode(qrcode_key)

        # 兼容两种结构：B站新版真实状态码在 data.code，外层 code 恒为 0
        # （2026-08 实测：{"code":0,"data":{"url":"","code":86101,...}}）
        data = raw.get('data') or {}
        code = data.get('code', raw.get('code'))

        # 0: 登录成功 —— 走三重校验
        if code == 0:
            # 校验1：提取 Cookie 且必须含 SESSDATA（url query + Set-Cookie 双通道）
            qr = QRCodeLogin()
            cookie = qr._extract_cookie_from_response(data, set_cookie_headers)
            if not cookie or 'SESSDATA=' not in cookie:
                logger.warning(f"[auth] code==0 但Cookie提取为空, url={data.get('url','')!r}, set_cookie={bool(set_cookie_headers)}")
                return {"success": True, "status": "error",
                        "message": "登录成功但Cookie提取失败，请重新扫码", "user": None}

            # 加密落盘（与桌面端向导共用同一份 secrets，重启不丢）
            config = ConfigManager()
            config.save_secret(COOKIE_SECRET_KEY, cookie)
            logger.info("[auth] 登录成功，Cookie已加密保存")

            # 同步写入 Cookie 池（持久化到 cookie_pool 表），
            # 让爬虫侧 get_cookie_pool() 立即可用，避免空池触发风控。
            try:
                from bilibili.cookie_pool import get_cookie_pool
                get_cookie_pool().add_cookie(cookie, persist=True)
            except Exception as _pool_err:
                logger.warning(f"[auth] 同步 Cookie 到池失败（secrets 已保存，不影响登录）: {_pool_err}")

            # 校验3：nav 实测
            valid = await CookieLoginHelper.validate_cookie(cookie)
            if not valid:
                return {"success": True, "status": "error",
                        "message": "Cookie验证未通过，请重新扫码", "user": None}

            user = await CookieLoginHelper.get_user_info(cookie)
            return {"success": True, "status": "confirmed",
                    "message": "✓获取登录态成功，已加密储存至本地", "user": user}

        # 86101: 未扫码，前端保持等待文案
        if code == 86101:
            return {"success": True, "status": "not_scanned", "message": "等待扫码", "user": None}

        # 86090: 已扫码未确认
        if code == 86090:
            return {"success": True, "status": "scanned", "message": "已扫码，请在手机上确认", "user": None}

        # 86038: 二维码真过期
        if code == 86038:
            return {"success": True, "status": "expired", "message": "二维码已过期，请重新生成", "user": None}

        # 其他：未知状态，前端可继续轮询
        logger.warning(f"[auth] 未知状态码: {code}, 消息: {data.get('message') or raw.get('message')}")
        return {"success": True, "status": "error", "message": data.get('message') or raw.get('message') or "未知状态", "user": None}

    except Exception as e:
        # 网络抖动等异常不抛 500，返回 error 让前端继续/重试
        logger.error(f"[auth] 查询登录状态失败: {e}")
        return {"success": True, "status": "error",
                "message": f"查询失败: {str(e)}", "user": None}


@router.post("/logout")
async def logout():
    """清除 B站登录态

    将 secrets 中的 bilibili.cookie 置空并加密落盘，
    get_secret 返回空串会被 status 接口判定为未登录。
    """
    try:
        config = ConfigManager()
        config.save_secret(COOKIE_SECRET_KEY, '')
        logger.info("[auth] 已清除登录态")
        return {"success": True, "message": "已清除登录态"}
    except Exception as e:
        logger.error(f"[auth] 清除登录态失败: {e}")
        raise HTTPException(status_code=500, detail=f"清除登录态失败: {str(e)}")