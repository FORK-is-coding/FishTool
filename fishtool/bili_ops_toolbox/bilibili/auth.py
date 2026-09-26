"""
B站API封装 - 扫码登录
实现B站官方扫码登录流程
参考: https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/login/login_action/QR.md

本模块提供完整的B站扫码登录能力，包含：

一、QRLoginStatus 状态常量
- NOT_SCANNED: 未扫码，等待用户打开APP扫码
- SCANNED: 已扫码未确认，等待用户在手机上确认
- CONFIRMED: 已确认，登录成功
- EXPIRED: 二维码过期，需要重新生成
- ERROR: 其他错误

二、QRCodeLogin 扫码登录管理器
- generate_qrcode: 向B站 passport 接口申请二维码，
  返回二维码 URL 与 PNG 图片字节（可直接显示/保存）
- poll_login_status: 轮询登录状态，支持超时、间隔、
  状态回调（回调可拿到状态码与提示消息）
- login_with_qrcode: 完整流程封装（生成+轮询）
- get_qrcode_image_base64: 转 base64 供 Web 展示

三、CookieLoginHelper Cookie 辅助工具
- validate_cookie: 调用 nav 接口验证登录态
- get_user_info: 获取当前登录用户信息
- parse_cookie_to_dict / extract_important_fields:
  Cookie 字符串解析与关键字段提取
  # 从数据中取出目标字段，供后续逻辑使用

典型流程：
    qr = QRCodeLogin()
    url, img = await qr.generate_qrcode()   # 展示二维码
    result = await qr.poll_login_status()   # 轮询直至成功
    cookie = result['cookie']               # 拿到 Cookie

注意：
- 轮询期间二维码有过期时间（约3分钟），超时抛异常
- Cookie 提取依赖登录成功响应 url 参数中的
# 从数据中取出目标字段，供后续逻辑使用
  DedeUserID/SESSDATA/bili_jct 字段
"""
import asyncio
import qrcode
from io import BytesIO
from typing import Optional, Callable, List
from datetime import datetime
import logging


from core.exceptions import AuthenticationError, CookieExpiredError
from core.logger import get_logger
from bilibili.api import BilibiliAPI

logger = get_logger(__name__)


class QRLoginStatus:
    """扫码登录状态
    
    定义扫码登录过程中的各状态码常量，
    供回调函数与调用方判断当前阶段。
    
    状态码：
    - NOT_SCANNED=0: 未扫码
    - SCANNED=1: 已扫码未确认
    - CONFIRMED=2: 已确认成功
    - EXPIRED=3: 二维码过期
    - ERROR=-1: 其他错误
    """
    NOT_SCANNED = 0      # 未扫码
    SCANNED = 1          # 已扫码未确认
    CONFIRMED = 2        # 已确认
    EXPIRED = 3          # 二维码已过期
    ERROR = -1           # 错误


class QRCodeLogin:
    """二维码登录管理器
    
    负责生成二维码、轮询登录状态、提取登录 Cookie。
    # 从数据中取出目标字段，供后续逻辑使用
    状态保存在实例字段中，支持长连接场景下的
    # 持久化数据，防止丢失
    多阶段调用（先出码，后轮询）。
    """
    
    def __init__(self):
        """初始化二维码登录管理器"""
        # 二维码关键 key，轮询接口必需
        self.qrcode_key: Optional[str] = None
        # 二维码内容 URL（扫码跳转地址）
        self.qrcode_url: Optional[str] = None
        # 登录成功后的完整响应数据
        self.login_result: Optional[dict] = None
        # 提取出的 Cookie 字符串
        # 从数据中取出目标字段，供后续逻辑使用
        self.cookie: Optional[str] = None
    
    async def generate_qrcode(self) -> tuple[str, bytes]:
        """生成登录二维码
        
        调用 passport 生成接口获取 qrcode_key 与 url，
        再用 qrcode 库渲染成 PNG 图片字节。
        
        Returns:
            (二维码URL, 二维码图片字节)
        """
        async with BilibiliAPI() as api:
            # 获取二维码 - api.get() 已经返回 data 字段
            data = await api.get(
                'https://passport.bilibili.com/x/passport-login/web/qrcode/generate'
            )
            
            self.qrcode_url = data.get('url')
            self.qrcode_key = data.get('qrcode_key')
            
            # 关键字段缺失说明接口异常
            if not self.qrcode_url or not self.qrcode_key:
                raise AuthenticationError("二维码数据不完整")
            
            # 生成二维码图片
            # 参数：version=1 最小版本，纠错级别 L，像素 10，边框 4
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=4,
            )
            # 添加data
            qr.add_data(self.qrcode_url)
            qr.make(fit=True)
            
            img = qr.make_image(fill_color="black", back_color="white")
            
            # 转换为字节
            # 用 BytesIO 避免落盘，直接得到 PNG 字节流
            img_bytes = BytesIO()
            # 保存数据
            img.save(img_bytes, format='PNG')
            img_bytes = img_bytes.getvalue()
            
            logger.info("二维码生成成功")
            return self.qrcode_url, img_bytes
    
    async def poll_login_status(self, 
                                timeout: int = 180,
                                interval: int = 2,
                                callback: Optional[Callable] = None) -> dict:
        """轮询登录状态
        
        按固定间隔轮询 passport 状态接口，根据返回码
        判断当前登录阶段，直到成功、超时或二维码失效。
        
        Args:
            timeout: 超时时间(秒)
            interval: 轮询间隔(秒)
            callback: 状态回调函数 callback(status, message)
            
        Returns:
            登录结果字典，包含cookie等信息
        """
        if not self.qrcode_key:
            raise AuthenticationError("请先生成二维码")
        
        # 取当前时间
        start_time = datetime.now()
        
        # 上下文管理：确保资源自动释放
        async with BilibiliAPI() as api:
            # 循环处理，满足条件后退出
            while True:
                # 检查超时
                # 二维码默认约3分钟过期，超时直接放弃
                elapsed = (datetime.now() - start_time).total_seconds()
                if elapsed > timeout:
                    if callback:
                        callback(QRLoginStatus.EXPIRED, "二维码已过期")
                    raise AuthenticationError("登录超时，二维码已过期")
                
                try:
                    # 统一由 BilibiliAPI 发起请求，同时保留 Set-Cookie 响应头。
                    async with BilibiliAPI() as api:
                        raw, set_cookie_headers = await api.poll_qrcode(self.qrcode_key)
                    
                    # 兼容两种结构：B站新版真实状态码在 data.code，外层 code 恒为 0
                    data = raw.get('data') or {}
                    code = data.get('code', raw.get('code'))
                    message = data.get('message') or raw.get('message', '')
                    
                    # 0: 成功
                    # 提取 Cookie 并组装登录结果
                    # 从数据中取出目标字段，供后续逻辑使用
                    if code == 0:
                        # 提取Cookie（url query + Set-Cookie 双通道）
                        # 从数据中取出目标字段，供后续逻辑使用
                        self.cookie = self._extract_cookie_from_response(data, set_cookie_headers)
                        self.login_result = data
                        
                        if callback:
                            callback(QRLoginStatus.CONFIRMED, "登录成功")
                        
                        logger.info("扫码登录成功")
                        
                        return {
                            'success': True,
                            'cookie': self.cookie,
                            'refresh_token': data.get('refresh_token'),
                            'timestamp': data.get('timestamp'),
                            'url': data.get('url')
                        }
                    
                    # 86101: 未扫码
                    # 用户在手机上尚未扫描二维码
                    elif code == 86101:
                        if callback:
                            callback(QRLoginStatus.NOT_SCANNED, "等待扫码")
                        logger.debug("等待用户扫码...")
                    
                    # 86090: 已扫码未确认
                    # 已扫码但未在手机上点击确认
                    elif code == 86090:
                        if callback:
                            callback(QRLoginStatus.SCANNED, "已扫码，等待确认")
                        logger.info("用户已扫码，等待确认...")
                    
                    # 86038: 二维码已失效
                    # 超过有效期，需要重新生成
                    elif code == 86038:
                        if callback:
                            callback(QRLoginStatus.EXPIRED, "二维码已过期")
                        raise AuthenticationError("二维码已过期")
                    
                    else:
                        # 未知状态码，记录日志但继续轮询
                        logger.warning(f"未知状态码: {code}, 消息: {message}")
                        if callback:
                            callback(QRLoginStatus.ERROR, message)
                    
                except Exception as e:
                    # 网络抖动等异常，记录后继续下一轮
                    logger.error(f"查询登录状态时出错: {e}")
                    if callback:
                        callback(QRLoginStatus.ERROR, str(e))
                
                # 等待下次轮询
                # 阻塞直到条件满足或超时
                await asyncio.sleep(interval)
    
    def _extract_cookie_from_response(self, data: dict,
                                      set_cookie_headers: Optional[List[str]] = None) -> str:
        """从响应中提取Cookie
        # 从数据中取出目标字段，供后续逻辑使用
        
        登录成功后 B站会在 data.url 中返回带
        Cookie 参数的跳转地址，从中解析出
        DedeUserID/SESSDATA/bili_jct 三个关键字段。
        若 url 中没有（B站改版后常见），则回退到
        响应头 Set-Cookie 提取同一批字段。
        
        Args:
            data: API响应的 data 字段
            set_cookie_headers: 可选，HTTP 响应头 Set-Cookie 列表，
                作为 url 提取失败的兜底通道
            
        Returns:
            Cookie字符串
        """
        # B站登录接口会在data中返回url参数，包含cookie信息
        url = data.get('url', '')
        
        if not url:
            logger.warning("响应中没有url字段，无法提取Cookie")
            cookie = self._extract_cookie_from_set_cookie(set_cookie_headers) if set_cookie_headers else ""
            if cookie:
                logger.info("从Set-Cookie响应头提取Cookie成功")
            return cookie
        
        # 从url中提取cookie参数
        # 格式: https://passport.bilibili.com/...?DedeUserID=xxx&SESSDATA=xxx&bili_jct=xxx&...
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            
            # 提取关键cookie字段
            # 只取登录态必需的三项，拼接成 Cookie 字符串
            # 组合多个片段生成完整结果
            cookie_parts = []
            if 'DedeUserID' in params:
                cookie_parts.append(f"DedeUserID={params['DedeUserID'][0]}")
            if 'SESSDATA' in params:
                cookie_parts.append(f"SESSDATA={params['SESSDATA'][0]}")
            if 'bili_jct' in params:
                cookie_parts.append(f"bili_jct={params['bili_jct'][0]}")
            
            # 拼接字符串/路径
            # 组合多个片段生成完整结果
            cookie_str = "; ".join(cookie_parts)
            
            if cookie_str:
                logger.info(f"成功从响应中提取Cookie，长度: {len(cookie_str)}")
                return cookie_str
            else:
                logger.warning("url参数中没有找到cookie字段，回退Set-Cookie通道")
                cookie = self._extract_cookie_from_set_cookie(set_cookie_headers) if set_cookie_headers else ""
                if cookie:
                    logger.info("从Set-Cookie响应头提取Cookie成功")
                return cookie
                
        except Exception as e:
            logger.error(f"解析Cookie失败: {e}")
            return ""

    @staticmethod
    def _extract_cookie_from_set_cookie(set_cookie_headers: Optional[List[str]]) -> str:
        """从 HTTP 响应头 Set-Cookie 提取登录态 Cookie
        
        B站 passport 登录成功时，除了 data.url 带参数外，
        响应头 Set-Cookie 也会下发 DedeUserID/SESSDATA/bili_jct。
        作为 url 通道失效时的兜底方案。
        
        Args:
            set_cookie_headers: Set-Cookie 响应头列表
            
        Returns:
            Cookie字符串（缺失字段跳过）
        """
        if not set_cookie_headers:
            return ""
        
        cookie_values = {}
        # 遍历 Set-Cookie 头
        for header in set_cookie_headers:
            try:
                # 按 ; 分割，首段是 key=value
                first = header.split(';', 1)[0].strip()
                # 判断是否存在
                if '=' in first:
                    key, value = first.split('=', 1)
                    cookie_values[key.strip()] = value.strip()
            except Exception:
                # 单条头解析失败不影响其他
                continue
        
        # 只提取登录态必需的三项
        # 组合多个片段生成完整结果
        cookie_parts = []
        # 从数据中取出目标字段，供后续逻辑使用
        for key in ('DedeUserID', 'SESSDATA', 'bili_jct'):
            # 判断是否存在
            if key in cookie_values:
                cookie_parts.append(f"{key}={cookie_values[key]}")
        
        return "; ".join(cookie_parts)
    
    async def login_with_qrcode(self,
                               callback: Optional[Callable] = None,
                               timeout: int = 180) -> dict:
        """执行完整的扫码登录流程
        
        生成二维码 -> 回调通知 -> 轮询直至成功。
        
        Args:
            callback: 状态回调函数
            timeout: 超时时间(秒)
            
        Returns:
            登录结果
        """
        # 生成二维码
        qr_url, qr_img = await self.generate_qrcode()
        
        # 通知调用方二维码已就绪（含图片字节）
        if callback:
            callback(QRLoginStatus.NOT_SCANNED, "二维码已生成", qr_img)
        
        # 轮询登录状态
        result = await self.poll_login_status(
            timeout=timeout,
            interval=2,
            callback=callback
        )
        
        return result
    
    def get_qrcode_image_base64(self, qr_img_bytes: bytes) -> str:
        """将二维码图片转换为base64字符串（用于Web显示）
        
        Args:
            qr_img_bytes: 二维码图片字节
            
        Returns:
            base64字符串
        """
        import base64
        return base64.b64encode(qr_img_bytes).decode('utf-8')


class CookieLoginHelper:
    """Cookie登录辅助工具
    
    提供手动粘贴 Cookie 场景下的验证与信息获取：
    - 验证 Cookie 是否有效（nav 接口 isLogin）
    - 获取当前登录用户信息（UID/昵称/头像/等级等）
    - Cookie 解析与关键字段提取
    # 从数据中取出目标字段，供后续逻辑使用
    """
    
    @staticmethod
    async def validate_cookie(cookie: str) -> bool:
        """验证Cookie是否有效
        
        Args:
            cookie: Cookie字符串
            
        Returns:
            是否有效
        """
        try:
            # 上下文管理：确保资源自动释放
            async with BilibiliAPI(cookie=cookie) as api:
                # api.get() 已经返回 data 字段
                data = await api.get(
                    'https://api.bilibili.com/x/web-interface/nav',
                    retry_times=1
                )
                
                # 检查是否登录成功 - data 中有 isLogin 字段
                is_login = data.get('isLogin', False)
                return is_login
                
        except CookieExpiredError:
            return False
        except Exception as e:
            logger.error(f"验证Cookie失败: {e}")
            return False
    
    @staticmethod
    async def get_user_info(cookie: str) -> dict:
        """获取用户信息
        
        Args:
            cookie: Cookie字符串
            
        Returns:
            用户信息字典
        """
        async with BilibiliAPI(cookie=cookie) as api:
            result = await api.get('https://api.bilibili.com/x/web-interface/nav')
            
            # api.get() 已经剥离外层，直接返回 data 部分
            # 检查 isLogin 字段判断登录状态
            if not result.get('isLogin', False):
                raise AuthenticationError(f"Cookie 已失效或未登录")
            
            # 组装用户信息，供界面展示与账号绑定
            return {
                'uid': result.get('mid'),
                'username': result.get('uname'),
                'face': result.get('face'),
                'level': result.get('level_info', {}).get('current_level'),
                'vip_type': result.get('vip', {}).get('type'),
                'is_login': result.get('isLogin', False)
            }
    
    @staticmethod
    def parse_cookie_to_dict(cookie_str: str) -> dict:
        """解析Cookie字符串为字典
        
        Args:
            cookie_str: Cookie字符串
            
        Returns:
            Cookie字典
        """
        cookie_dict = {}
        for item in cookie_str.split(';'):
            # 去除首尾空白
            item = item.strip()
            if '=' in item:
                key, value = item.split('=', 1)
                cookie_dict[key.strip()] = value.strip()
        return cookie_dict
    
    @staticmethod
    def extract_important_fields(cookie_str: str) -> dict:
        """提取Cookie中的重要字段
        # 从数据中取出目标字段，供后续逻辑使用
        
        从完整 Cookie 中挑出登录态与鉴权必需字段，
        供 CookiePool 入库时拆分存储。
        
        Args:
            cookie_str: Cookie字符串
            
        Returns:
            重要字段字典
        """
        cookie_dict = CookieLoginHelper.parse_cookie_to_dict(cookie_str)
        
        return {
            'sessdata': cookie_dict.get('SESSDATA', ''),
            'bili_jct': cookie_dict.get('bili_jct', ''),
            'buvid3': cookie_dict.get('buvid3', ''),
            'buvid4': cookie_dict.get('buvid4', ''),
            'DedeUserID': cookie_dict.get('DedeUserID', ''),
            'DedeUserID__ckMd5': cookie_dict.get('DedeUserID__ckMd5', '')
        }


# 使用示例
async def example_qr_login():
    """扫码登录使用示例
    
    演示完整流程：生成二维码保存到本地文件，
    # 持久化数据，防止丢失
    轮询期间按状态打印提示。
    """
    
    def status_callback(status, message, qr_img=None):
        """状态回调
        
        根据状态码打印对应提示。
        """
        if status == QRLoginStatus.NOT_SCANNED:
            print(f"[等待扫码] {message}")
            if qr_img:
                # 保存二维码图片
                with open('qrcode.png', 'wb') as f:
                    # 写入数据
                    f.write(qr_img)
                print("二维码已保存到 qrcode.png")
        elif status == QRLoginStatus.SCANNED:
            print(f"[已扫码] {message}")
        elif status == QRLoginStatus.CONFIRMED:
            print(f"[登录成功] {message}")
        elif status == QRLoginStatus.EXPIRED:
            print(f"[已过期] {message}")
        else:
            print(f"[错误] {message}")
    
    # 执行登录
    qr_login = QRCodeLogin()
    try:
        result = await qr_login.login_with_qrcode(
            callback=status_callback,
            timeout=180
        )
        print(f"登录结果: {result}")
    except Exception as e:
        print(f"登录失败: {e}")


if __name__ == '__main__':
    # 测试
    asyncio.run(example_qr_login())
