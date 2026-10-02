"""
B站API封装 - WBISigner WBI签名器

拆分自 api.py 原始 L72-L264。
B站部分接口（如投稿搜索）要求 WBI 签名：
- 从 nav 接口获取 img_key / sub_key 密钥对
- 用混淆表对密钥进行字符重排得到 mixin_key
- 参数排序 + 过滤特殊字符 + 拼时间戳 wts
- MD5(query + mixin_key) 得到 w_rid 签名
- 密钥每小时刷新一次（need_update 判断）
"""
import time
import hashlib
import urllib.parse

import aiohttp
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from functools import reduce

from core.exceptions import WBISignError
from core.logger import get_logger
from core.request_budget import RequestBudgetExceeded, before_http_attempt

logger = get_logger(__name__)

#: 凭证域取值域（与 core/quota_store 口径一致）。
CREDENTIAL_DOMAIN_COOKIE = 'cookie'
CREDENTIAL_DOMAIN_NO_COOKIE = 'no_cookie'

#: nav（WBI 密钥）请求归入「账号维护」类别（规格 C9：cookie 校验 48 + wbi 刷新 24 = 72）。
CATEGORY_MAINTENANCE = 'maintenance'


class WBISigner:
    """WBI签名器 - 实现B站WBI签名算法
    
    负责从 nav 接口获取密钥并生成请求签名。
    # 读取数据并赋值给当前作用域变量
    密钥有效期为1小时，过期后自动刷新。
    """
    
    # 混淆表
    # 官方给定的64位重排表，用于打乱 img_key+sub_key
    MIXIN_KEY_ENC_TAB = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
        33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
        61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
        36, 20, 34, 44, 52
    ]
    
    def __init__(self, domain: str = CREDENTIAL_DOMAIN_COOKIE):
        """初始化 WBI 签名器，密钥初始为空，需先刷新
        # 设置初始值/默认状态，避免后续空引用

        通过 refresh_keys() 从 nav 接口获取密钥后再签名。
        # 读取数据并赋值给当前作用域变量

        Args:
            domain: 凭证域（cookie / no_cookie），由所属客户端注入；
                nav 请求是一次真实 HTTP 尝试，需带域参与分域冷却判定。
        """
        # 两个基础密钥，从 nav 接口 wbi_img 字段提取
        # 从数据中取出目标字段，供后续逻辑使用
        self.img_key: Optional[str] = None
        self.sub_key: Optional[str] = None
        # 混淆后的合成密钥，实际参与签名
        self.mixin_key: Optional[str] = None
        # 上次刷新时间，用于过期判断
        # 根据条件走向不同处理分支
        self.last_update: Optional[datetime] = None
        # 密钥刷新间隔：1小时
        self.update_interval = timedelta(hours=1)  # WBI密钥刷新间隔
        # 凭证域：由所属客户端注入，nav 请求按它参与分域冷却 / 配额判定。
        self.domain = domain
    
    def get_mixin_key(self, orig: str) -> str:
        """对 imgKey 和 subKey 进行字符顺序打乱编码
        
        按混淆表索引逐字符取出组成新字符串，
        取前32位作为 mixin_key。
        
        Args:
            orig: 原始密钥
            
        Returns:
            混淆后的密钥
        """
        return reduce(lambda s, i: s + orig[i], self.MIXIN_KEY_ENC_TAB, '')[:32]
    
    def enc_wbi(self, params: Dict[str, Any], img_key: str, sub_key: str) -> Dict[str, Any]:
        """为请求参数进行WBI签名
        
        完整签名流程：
        1. 合成 mixin_key（img_key + sub_key 混淆）
        2. 添加时间戳 wts
        # 将元素加入容器/布局
        3. 按 key 排序参数
        4. 过滤 !'()* 等特殊字符
        # 剔除不符合条件的数据
        5. urlencode 拼接查询串
        # 组合多个片段生成完整结果
        6. MD5(query + mixin_key) 生成 w_rid
        
        Args:
            params: 请求参数
            img_key: img_key
            sub_key: sub_key
            
        Returns:
            签名后的参数
        """
        # 先合成混淆密钥，用于后续签名
        mixin_key = self.get_mixin_key(img_key + sub_key)
        # 取当前 Unix 时间戳
        curr_time = round(time.time())
        
        # 添加时间戳
        # wts 参与签名计算，防止重放
        # 对输入做运算得到结果
        params['wts'] = curr_time
        
        # 按照key排序
        # 签名算法要求参数按字典序排列
        params = dict(sorted(params.items()))
        
        # 过滤value中的特殊字符
        # !'()* 在 urlencode 前后可能变化，需要先剔除
        params = {
            k: ''.join(filter(lambda ch: ch not in "!'()*", str(v)))
            for k, v in params.items()
        }
        
        # 拼接参数
        # 组合多个片段生成完整结果
        query = urllib.parse.urlencode(params)
        
        # 计算签名
        # MD5(query + mixin_key) 是 WBI 签名的核心
        wbi_sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
        
        # 添加签名
        # 将元素加入容器/布局
        params['w_rid'] = wbi_sign
        
        return params
    
    async def update_wbi_keys(self, session: aiohttp.ClientSession):
        """从B站API获取最新的WBI密钥
        # 读取数据并赋值给当前作用域变量
        
        调用 nav 接口，从 wbi_img.img_url / sub_url
        的文件名中提取密钥（去掉路径与扩展名）。
        # 从数据中取出目标字段，供后续逻辑使用
        
        Args:
            session: aiohttp会话
        """
        try:
            nav_url = 'https://api.bilibili.com/x/web-interface/nav'
            # 直接取 WBI 密钥是一次真实 HTTP 尝试，同样接预算钩子；
            # 经 client 走的已覆盖请求不会重复到这里。
            # 带凭证域 + 账号维护类别（C9：wbi 刷新 24/天）。
            before_http_attempt(self.domain, CATEGORY_MAINTENANCE)
            # 上下文管理：确保资源自动释放
            async with session.get(nav_url) as resp:
                # 边界/有效性检查
                if resp.status != 200:
                    # 抛出异常中断流程
                    raise WBISignError(f"获取WBI密钥失败: HTTP {resp.status}")
                
                data = await resp.json()
                # B站 nav 接口未登录时 code=-101，但 data.wbi_img 依然正常返回
                # WBI 签名密钥对匿名访问也开放，不能因为未登录就中断签名流程
                wbi_img = (data.get('data') or {}).get('wbi_img')
                # 边界/有效性检查
                if not wbi_img or not wbi_img.get('img_url') or not wbi_img.get('sub_url'):
                    # 抛出异常中断流程
                    raise WBISignError(f"获取WBI密钥失败: {data.get('message', 'nav接口未返回wbi_img')}")
                
                # 提取密钥
                # nav 返回 wbi_img.img_url / sub_url 两个图片地址，
                # 密钥即图片文件名主体（不含扩展名）
                # 从响应中取出 wbi_img 字段
                # 分别提取 img 与 sub 两个图片地址
                # 从数据中取出目标字段，供后续逻辑使用
                img_url = wbi_img['img_url']
                sub_url = wbi_img['sub_url']
                
                # 从URL中提取密钥
                # 取文件名主体作为密钥：去掉路径和扩展名
                self.img_key = img_url.rsplit('/', 1)[1].split('.')[0]
                self.sub_key = sub_url.rsplit('/', 1)[1].split('.')[0]
                self.mixin_key = self.get_mixin_key(self.img_key + self.sub_key)
                self.last_update = datetime.now()
                
                logger.info("WBI密钥更新成功")
                
        except RequestBudgetExceeded:
            # 预算耗尽不是签名失败：保留类型向上抛出，避免被转成 WBISignError 后重试。
            raise
        except Exception as e:
            logger.error(f"更新WBI密钥失败: {e}")
            # 抛出异常中断流程
            raise WBISignError(f"更新WBI密钥失败: {e}")
    
    def need_update(self) -> bool:
        """检查是否需要更新WBI密钥
        # 验证状态/条件，决定下一步分支
        
        从未获取过或超过1小时未刷新时返回 True。
        # 读取数据并赋值给当前作用域变量
        
        Returns:
            是否需要更新
            # 用新值覆盖旧值，保持数据一致
        """
        if self.last_update is None:
            return True
        return datetime.now() - self.last_update > self.update_interval
    
    async def sign_params(self, params: Dict[str, Any], session: aiohttp.ClientSession) -> Dict[str, Any]:
        """对参数进行签名（自动更新密钥）
        # 用新值覆盖旧值，保持数据一致
        
        Args:
            params: 请求参数
            session: aiohttp会话
            
        Returns:
            签名后的参数
        """
        # 检查是否需要更新密钥
        # 首次使用或过期时自动拉取新密钥
        # 需要时先刷新，保证签名用最新的密钥
        if self.need_update():
            await self.update_wbi_keys(session)
        
        # 用当前密钥完成 WBI 签名
        return self.enc_wbi(params, self.img_key, self.sub_key)
