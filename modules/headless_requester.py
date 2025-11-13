"""
无头请求模块
使用 cloudscraper 直接发送请求到 LMArena API，自动绕过 Cloudflare 验证
"""

import asyncio
import json
import logging
import uuid
import random
import os
import codecs
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional, AsyncGenerator, Tuple

try:
    import cloudscraper
except ImportError:
    cloudscraper = None
    logging.warning("cloudscraper 未安装，无法使用 Cloudflare 绕过功能。请运行: pip install cloudscraper")

logger = logging.getLogger(__name__)


class ScraperPool:
    """Cloudscraper 连接池，支持高并发请求"""
    
    def __init__(self, pool_size: int, cookies: dict, config: dict):
        """
        初始化连接池
        
        Args:
            pool_size: 池大小
            cookies: Cookie 字典
            config: 配置字典
        """
        self.pool_size = pool_size
        self.cookies = cookies
        self.config = config
        self._pool = asyncio.Queue(maxsize=pool_size)
        self._lock = asyncio.Lock()
        self._initialized = False
        
    async def initialize(self):
        """初始化连接池（异步）"""
        if self._initialized:
            return
        
        async with self._lock:
            if self._initialized:  # 双重检查
                return
            
            logger.info(f"正在初始化 Scraper 连接池（大小: {self.pool_size}）...")
            
            for i in range(self.pool_size):
                scraper = self._create_scraper(i)
                await self._pool.put(scraper)
            
            self._initialized = True
            logger.info(f"✅ Scraper 连接池初始化完成（{self.pool_size} 个实例）")
    
    def _create_scraper(self, index: int):
        """
        创建单个 scraper 实例
        
        Args:
            index: 实例索引
        """
        if not cloudscraper:
            return None
        
        try:
            # 获取配置
            interpreter = self.config.get('cloudscraper_interpreter', 'js2py')
            delay = self.config.get('cloudscraper_delay', 5)
            debug = self.config.get('cloudscraper_debug', False)
            
            # 每个实例使用不同的浏览器指纹
            browser_config = self._get_random_browser_config()
            
            scraper_options = {
                'browser': browser_config,
                'interpreter': interpreter,
                'delay': delay,
                'debug': debug
            }
            
            scraper = cloudscraper.create_scraper(**scraper_options)
            
            # 加载 cookies
            if self.cookies:
                for name, value in self.cookies.items():
                    scraper.cookies.set(name, value, domain='.lmarena.ai')
            
            # 标记实例
            scraper._pool_index = index
            scraper._browser_config = browser_config
            
            logger.debug(f"创建 Scraper 实例 #{index} (browser: {browser_config['browser']}/{browser_config['platform']})")
            
            return scraper
            
        except Exception as e:
            logger.error(f"创建 Scraper 实例 #{index} 失败: {e}")
            return None
    
    def _get_random_browser_config(self):
        """随机选择浏览器配置"""
        browsers = ['chrome', 'firefox']
        platforms = ['windows', 'linux', 'darwin']
        
        return {
            'browser': random.choice(browsers),
            'platform': random.choice(platforms),
            'mobile': False,
            'desktop': True
        }
    
    async def acquire(self):
        """
        从池中获取一个 scraper
        
        Returns:
            scraper 实例
        """
        if not self._initialized:
            await self.initialize()
        
        scraper = await self._pool.get()
        logger.debug(f"获取 Scraper 实例 #{scraper._pool_index if scraper else 'None'} (池中剩余: {self._pool.qsize()})")
        return scraper
    
    async def release(self, scraper):
        """
        归还 scraper 到池中
        
        Args:
            scraper: 要归还的 scraper 实例
        """
        if scraper:
            await self._pool.put(scraper)
            logger.debug(f"归还 Scraper 实例 #{scraper._pool_index} (池中剩余: {self._pool.qsize()})")
    
    async def update_cookies(self, cookies: dict):
        """
        更新所有 scraper 的 cookies
        
        Args:
            cookies: 新的 cookie 字典
        """
        self.cookies = cookies
        
        # 获取所有 scraper，更新后归还
        scrapers = []
        for _ in range(self.pool_size):
            scraper = await self.acquire()
            if scraper:
                # 清除旧 cookies
                scraper.cookies.clear()
                # 设置新 cookies
                for name, value in cookies.items():
                    scraper.cookies.set(name, value, domain='.lmarena.ai')
                scrapers.append(scraper)
        
        # 归还所有 scraper
        for scraper in scrapers:
            await self.release(scraper)
        
        logger.info(f"已更新连接池中所有 {len(scrapers)} 个 Scraper 的 cookies")


class HeadlessRequester:
    """无头请求器：使用 cloudscraper 向 LMArena API 发送请求，自动绕过 Cloudflare"""
    
    def __init__(self, config: dict = None, cookie_getter: callable = None):
        """
        初始化无头请求器
        
        Args:
            config: 配置字典，包含 cloudscraper 相关配置
            cookie_getter: cookie 获取函数，返回 dict（必需）
        """
        self.base_url = "https://lmarena.ai"
        self.config = config or {}
        self.cookies = {}
        self.scraper = None  # 保留用于回退模式
        self.request_count = 0
        self._cookies_need_update = False  # Cookie 更新标志
        self.cookie_getter = cookie_getter  # 动态 cookie 获取函数
        
        # 连接池配置
        self.use_pool = self.config.get('use_scraper_pool', True)
        pool_size = self.config.get('scraper_pool_size', 10)
        
        # 创建专用线程池用于 cloudscraper 同步调用
        thread_pool_size = self.config.get('thread_pool_size', 50)
        self._executor = ThreadPoolExecutor(
            max_workers=thread_pool_size,
            thread_name_prefix='CloudScraperWorker'
        )
        logger.info(f"✅ 创建专用线程池（大小: {thread_pool_size}）")
        
        # 初始化 cookies
        self._load_cookies()
        
        if self.use_pool and cloudscraper:
            # 使用连接池模式
            self.scraper_pool = ScraperPool(pool_size, self.cookies, self.config)
            logger.info(f"启用 Scraper 连接池模式（池大小: {pool_size}）")
        else:
            # 使用单例模式（回退）
            self.scraper_pool = None
            self._init_scraper()
            if not self.use_pool:
                logger.info("已禁用连接池，使用单例模式")
    
    def _load_cookies(self, force_reload: bool = False):
        """
        从 .env 加载 cookies（通过 cookie_getter）
        
        Args:
            force_reload: 强制重新加载
        """
        if not self.cookie_getter:
            logger.error("❌ 未提供 cookie_getter，无法加载 cookies")
            return False
        
        try:
            dynamic_cookies = self.cookie_getter()
            if dynamic_cookies and isinstance(dynamic_cookies, dict):
                old_count = len(self.cookies)
                self.cookies = dynamic_cookies.copy()
                
                if old_count > 0:
                    logger.debug(f"🔄 从 .env 重新加载了 {len(self.cookies)} 个 cookies（原有 {old_count} 个）")
                else:
                    logger.info(f"✅ 从 .env 加载了 {len(self.cookies)} 个 cookies")
                
                self._cookies_need_update = True
                return True
            else:
                logger.warning("⚠️ 从 .env 获取的 cookies 为空或无效")
                return False
        except Exception as e:
            logger.error(f"❌ 从 .env 加载 cookies 失败: {e}")
            return False
    
    async def _update_scraper_cookies(self):
        """更新 scraper 的 cookies（仅更新池中的 cookies 配置，不更新正在使用的实例）"""
        try:
            if self.scraper_pool:
                # 只更新连接池的 cookies 配置，不获取所有实例
                # 新获取的 scraper 会自动使用新 cookies
                self.scraper_pool.cookies = self.cookies.copy()
                logger.debug(f"已更新连接池的 cookies 配置（{len(self.cookies)} 个）")
            elif self.scraper:
                # 单例模式：直接更新
                self.scraper.cookies.clear()
                for name, value in self.cookies.items():
                    self.scraper.cookies.set(name, value, domain='.lmarena.ai')
                logger.debug(f"已更新 scraper 的 {len(self.cookies)} 个 cookies")
        except Exception as e:
            logger.error(f"更新 scraper cookies 失败: {e}")
    
    def check_and_reload_cookies(self) -> bool:
        """
        从 .env 重新加载 cookies
        
        Returns:
            如果成功加载返回 True，否则返回 False
        """
        return self._load_cookies(force_reload=True)
    
    def force_reload_cookies(self) -> bool:
        """
        强制重新加载 cookies
        
        Returns:
            如果成功重新加载返回 True，否则返回 False
        """
        logger.info("🔄 强制重新加载 cookies...")
        return self._load_cookies(force_reload=True)
    
    def _init_scraper(self):
        """初始化 cloudscraper"""
        if not cloudscraper:
            logger.warning("cloudscraper 未安装，将使用基本的请求功能")
            return
        
        try:
            # 获取配置
            interpreter = self.config.get('cloudscraper_interpreter', 'js2py')
            delay = self.config.get('cloudscraper_delay', 5)
            debug = self.config.get('cloudscraper_debug', False)
            
            # 获取浏览器配置
            browser_config = self._get_random_browser_config()
            
            # 创建 scraper - 使用标准 cloudscraper 参数
            scraper_options = {
                'browser': browser_config,
                'interpreter': interpreter,
                'delay': delay,
                'debug': debug
            }
            
            self.scraper = cloudscraper.create_scraper(**scraper_options)
            
            # 加载 cookies 到 scraper
            if self.cookies:
                for name, value in self.cookies.items():
                    self.scraper.cookies.set(name, value, domain='.lmarena.ai')
            
            logger.info(f"成功初始化 cloudscraper (interpreter: {interpreter}, browser: {browser_config['browser']}/{browser_config['platform']})")
            
        except Exception as e:
            logger.error(f"初始化 cloudscraper 失败: {e}")
            logger.exception("详细错误信息:")
            self.scraper = None
    
    def _get_random_browser_config(self):
        """随机选择浏览器配置"""
        browsers = ['chrome', 'firefox']
        platforms = ['windows', 'linux', 'darwin']
        
        return {
            'browser': random.choice(browsers),
            'platform': random.choice(platforms),
            'mobile': False,
            'desktop': True
        }
    
    def _rotate_fingerprint(self):
        """轮换浏览器指纹"""
        if not self.scraper:
            return
        
        try:
            # 重新创建 scraper 以更换指纹
            logger.info("正在轮换浏览器指纹...")
            self._init_scraper()
            logger.info("浏览器指纹已更换")
        except Exception as e:
            logger.error(f"轮换指纹失败: {e}")
    
    def _construct_messages(
        self,
        message_templates: list,
        session_id: str,
        is_image_request: bool = False
    ) -> list:
        """
        构造 LMArena API 所需的消息格式
        
        Args:
            message_templates: 消息模板列表
            session_id: 会话 ID
            is_image_request: 是否为图像生成请求
            
        Returns:
            构造好的消息列表
        """
        new_messages = []
        last_msg_id = None
        
        for i, template in enumerate(message_templates):
            current_msg_id = str(uuid.uuid4())
            parent_ids = [last_msg_id] if last_msg_id else []
            
            # 如果是图像请求，所有消息状态都是 'success'
            # 否则，只有最后一条消息是 'pending'
            status = 'success' if is_image_request else ('pending' if i == len(message_templates) - 1 else 'success')
            
            new_messages.append({
                'role': template['role'],
                'content': template.get('content', ''),
                'id': current_msg_id,
                'evaluationId': None,
                'evaluationSessionId': session_id,
                'parentMessageIds': parent_ids,
                'experimental_attachments': template.get('attachments', []),
                'failureReason': None,
                'participantPosition': template.get('participantPosition', 'a'),
                'createdAt': datetime.utcnow().isoformat() + 'Z',
                'updatedAt': datetime.utcnow().isoformat() + 'Z',
                'status': status,
            })
            last_msg_id = current_msg_id
        
        return new_messages
    
    async def send_request(
        self,
        session_id: str,
        message_templates: list,
        target_model_id: Optional[str] = None,
        is_image_request: bool = False
    ) -> AsyncGenerator[Tuple[str, str], None]:
        """
        发送请求到 LMArena API 并流式返回结果
        
        Args:
            session_id: 会话 ID
            message_templates: 消息模板列表
            target_model_id: 目标模型 ID（可选）
            is_image_request: 是否为图像生成请求
            
        Yields:
            (事件类型, 数据) 元组，其中事件类型可以是:
            - 'content': 文本内容
            - 'image': 图像数据
            - 'finish': 完成信息
            - 'error': 错误信息
        """
        # 自动从 .env 重载 cookies
        cookie_reloaded = self.check_and_reload_cookies()
        if cookie_reloaded:
            logger.debug("✨ 已从 .env 重新加载 cookies")
            self._cookies_need_update = True
        
        # 如果 cookies 需要更新，更新所有 scraper
        if self._cookies_need_update and (self.scraper_pool or self.scraper):
            await self._update_scraper_cookies()
            self._cookies_need_update = False
        
        # 仅对单例模式轮换指纹（连接池中每个实例都有不同指纹）
        if not self.scraper_pool:
            fingerprint_rotation_interval = self.config.get('fingerprint_rotation_requests', 10)
            if fingerprint_rotation_interval > 0 and self.request_count > 0 and self.request_count % fingerprint_rotation_interval == 0:
                self._rotate_fingerprint()
        
        self.request_count += 1
        
        # 构造 API URL - 新格式：/nextjs-api/stream/post-to-evaluation/{session_id}
        api_url = f"{self.base_url}/nextjs-api/stream/post-to-evaluation/{session_id}"
        
        # 生成所需的 UUID
        user_message_id = str(uuid.uuid4())
        model_a_message_id = str(uuid.uuid4())
        model_b_message_id = str(uuid.uuid4())
        
        # 构造消息数组
        new_messages = []
        for i, template in enumerate(message_templates):
            message_id = str(uuid.uuid4())
            
            new_messages.append({
                'id': message_id,
                'evaluationSessionId': session_id,
                'role': template['role'],
                'parentMessageIds': [],
                'content': template.get('content', ''),
                'experimental_attachments': template.get('attachments', []),
                'participantPosition': template.get('participantPosition', 'b'),
            })
        
        # 构造新的请求体结构
        body = {
            'id': session_id,
            'mode': 'battle',
            'userMessageId': user_message_id,
            'modelAMessageId': model_a_message_id,
            'modelBMessageId': model_b_message_id,
            'messages': new_messages,
            'modality': 'chat'
        }
        
        logger.info(f"[Headless] 准备发送请求到: {api_url} (请求 #{self.request_count})")
        logger.debug(f"[Headless] 请求体: {json.dumps(body, ensure_ascii=False, indent=2)}")
        
        scraper = None
        try:
            # 使用连接池或单例
            if self.scraper_pool and cloudscraper:
                # 连接池模式
                scraper = await self.scraper_pool.acquire()
                
                if not scraper:
                    logger.error("[Headless] 从连接池获取 scraper 失败")
                    yield 'error', "无法获取可用的 scraper 实例"
                    return
                
                logger.info(f"[Headless] 使用连接池 Scraper #{scraper._pool_index} ({scraper._browser_config['browser']}/{scraper._browser_config['platform']})")
                
                # 发起请求（在线程池中）
                loop = asyncio.get_event_loop()
                
                # 从配置读取队列大小
                queue_size = self.config.get('stream_queue_size', 500)
                chunk_queue = asyncio.Queue(maxsize=queue_size)
                
                def make_request_and_stream():
                    """在线程中发起请求并流式读取"""
                    try:
                        response = scraper.post(
                            api_url,
                            data=json.dumps(body),
                            headers={
                                'Content-Type': 'text/plain;charset=UTF-8',
                                'Accept': '*/*',
                            },
                            stream=True,
                            timeout=360
                        )
                        
                        if response.status_code != 200:
                            error_msg = f"HTTP {response.status_code}: {response.text[:500]}"
                            # 使用 run_coroutine_threadsafe 安全地放入队列（会等待）
                            future = asyncio.run_coroutine_threadsafe(
                                chunk_queue.put(('error', error_msg)), loop
                            )
                            future.result()  # 等待完成
                            return
                        
                        # 流式读取并放入队列
                        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
                        
                        for chunk in response.iter_content(chunk_size=8192, decode_unicode=False):
                            if chunk:
                                text_chunk = decoder.decode(chunk, False)
                                if text_chunk:
                                    # 使用 run_coroutine_threadsafe 安全地放入队列（会等待）
                                    future = asyncio.run_coroutine_threadsafe(
                                        chunk_queue.put(('chunk', text_chunk)), loop
                                    )
                                    future.result()  # 等待队列有空间
                        
                        # 解码剩余字节
                        final_chunk = decoder.decode(b'', True)
                        if final_chunk:
                            future = asyncio.run_coroutine_threadsafe(
                                chunk_queue.put(('chunk', final_chunk)), loop
                            )
                            future.result()
                        
                        # 标记完成
                        future = asyncio.run_coroutine_threadsafe(
                            chunk_queue.put(('done', None)), loop
                        )
                        future.result()
                        
                    except Exception as e:
                        error_msg = f"请求异常: {str(e)}"
                        try:
                            future = asyncio.run_coroutine_threadsafe(
                                chunk_queue.put(('error', error_msg)), loop
                            )
                            future.result()
                        except:
                            pass  # 如果连错误都放不进去，就忽略
                
                # 在线程池中启动请求
                self._executor.submit(make_request_and_stream)
                
                logger.info(f"[Headless] 请求已提交，等待数据...")
                
                # 在主循环中从队列读取数据
                while True:
                    msg_type, data = await chunk_queue.get()
                    
                    if msg_type == 'error':
                        logger.error(f"[Headless] 请求失败: {data}")
                        yield 'error', data
                        break
                    elif msg_type == 'chunk':
                        yield 'raw_chunk', data
                    elif msg_type == 'done':
                        logger.info(f"[Headless] 流式数据接收完成")
                        break
                
            elif self.scraper and cloudscraper:
                # 单例模式（回退）
                logger.info("[Headless] 使用单例 Scraper（连接池已禁用）")
                
                loop = asyncio.get_event_loop()
                
                # 从配置读取队列大小
                queue_size = self.config.get('stream_queue_size', 500)
                chunk_queue = asyncio.Queue(maxsize=queue_size)
                
                def make_request_and_stream():
                    """在线程中发起请求并流式读取"""
                    try:
                        response = self.scraper.post(
                            api_url,
                            data=json.dumps(body),
                            headers={
                                'Content-Type': 'text/plain;charset=UTF-8',
                                'Accept': '*/*',
                            },
                            stream=True,
                            timeout=360
                        )
                        
                        if response.status_code != 200:
                            error_msg = f"HTTP {response.status_code}: {response.text[:500]}"
                            future = asyncio.run_coroutine_threadsafe(
                                chunk_queue.put(('error', error_msg)), loop
                            )
                            future.result()
                            return
                        
                        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
                        
                        for chunk in response.iter_content(chunk_size=8192, decode_unicode=False):
                            if chunk:
                                text_chunk = decoder.decode(chunk, False)
                                if text_chunk:
                                    future = asyncio.run_coroutine_threadsafe(
                                        chunk_queue.put(('chunk', text_chunk)), loop
                                    )
                                    future.result()
                        
                        final_chunk = decoder.decode(b'', True)
                        if final_chunk:
                            future = asyncio.run_coroutine_threadsafe(
                                chunk_queue.put(('chunk', final_chunk)), loop
                            )
                            future.result()
                        
                        future = asyncio.run_coroutine_threadsafe(
                            chunk_queue.put(('done', None)), loop
                        )
                        future.result()
                        
                    except Exception as e:
                        error_msg = f"请求异常: {str(e)}"
                        try:
                            future = asyncio.run_coroutine_threadsafe(
                                chunk_queue.put(('error', error_msg)), loop
                            )
                            future.result()
                        except:
                            pass
                
                # 在线程池中启动请求
                self._executor.submit(make_request_and_stream)
                
                logger.info(f"[Headless] 请求已提交，等待数据...")
                
                # 从队列读取数据
                while True:
                    msg_type, data = await chunk_queue.get()
                    
                    if msg_type == 'error':
                        logger.error(f"[Headless] 请求失败: {data}")
                        yield 'error', data
                        break
                    elif msg_type == 'chunk':
                        yield 'raw_chunk', data
                    elif msg_type == 'done':
                        logger.info(f"[Headless] 流式数据接收完成")
                        break
                
            else:
                # 回退到基本的 cookie 请求（不使用 cloudscraper）
                logger.warning("[Headless] cloudscraper 不可用，使用基本请求模式")
                
                if not self.cookies:
                    logger.error("没有可用的 cookies，无法发送请求。")
                    yield 'error', "没有可用的 cookies"
                    return
                
                import httpx
                
                headers = {
                    'Content-Type': 'text/plain;charset=UTF-8',
                    'Accept': '*/*',
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Origin': 'https://lmarena.ai',
                    'Referer': 'https://lmarena.ai/',
                }
                
                async with httpx.AsyncClient(cookies=self.cookies, timeout=360.0) as client:
                    async with client.stream(
                        'POST',
                        api_url,
                        headers=headers,
                        content=json.dumps(body),
                    ) as response:
                        
                        if response.status_code != 200:
                            error_text = await response.aread()
                            error_msg = f"HTTP {response.status_code}: {error_text.decode('utf-8', errors='ignore')}"
                            logger.error(f"[Headless] 请求失败: {error_msg}")
                            yield 'error', error_msg
                            return
                        
                        logger.info(f"[Headless] 请求成功，开始接收流式数据...")
                        
                        # 使用增量解码器处理流式 UTF-8 数据，避免多字节字符被切断
                        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
                        
                        async for chunk_bytes in response.aiter_bytes():
                            if chunk_bytes:
                                # 使用增量解码器，可以正确处理被切断的多字节字符
                                text_chunk = decoder.decode(chunk_bytes, False)
                                if text_chunk:  # 只有当有完整字符时才yield
                                    yield 'raw_chunk', text_chunk
                        
                        # 解码剩余的字节（如果有）
                        final_chunk = decoder.decode(b'', True)
                        if final_chunk:
                            yield 'raw_chunk', final_chunk
                        
                        logger.info(f"[Headless] 流式数据接收完成。")
                    
        except Exception as e:
            error_msg = f"请求错误: {str(e)}"
            logger.error(f"[Headless] {error_msg}", exc_info=True)
            yield 'error', error_msg
        finally:
            # 归还 scraper 到连接池
            if scraper and self.scraper_pool:
                await self.scraper_pool.release(scraper)
                logger.debug(f"已归还 Scraper #{scraper._pool_index} 到连接池")


# 全局实例
_headless_requester: Optional[HeadlessRequester] = None


def get_headless_requester(config: dict = None, cookie_getter: callable = None) -> HeadlessRequester:
    """
    获取全局无头请求器实例
    
    Args:
        config: 配置字典
        cookie_getter: cookie 获取函数（必需）
    """
    global _headless_requester
    if _headless_requester is None:
        _headless_requester = HeadlessRequester(config, cookie_getter)
    else:
        # 如果提供了新的 cookie_getter，更新它
        if cookie_getter is not None:
            _headless_requester.cookie_getter = cookie_getter
            logger.debug("已更新 cookie_getter")
    return _headless_requester