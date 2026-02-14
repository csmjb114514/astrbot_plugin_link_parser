from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Plain, At, Reply
from astrbot.api.event import MessageChain
import aiohttp
import json
import re
import asyncio
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlencode
import time
from enum import Enum

class TaskStatus(Enum):
    """任务状态枚举"""
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"

@dataclass
class ParseTask:
    """解析任务类"""
    user_id: str
    user_name: str
    url: str
    event_origin: str
    message_id: Optional[str] = None
    attempts: int = 0
    max_attempts: int = 10
    status: TaskStatus = TaskStatus.PENDING
    create_time: float = None
    last_attempt_time: float = None
    error_history: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        self.create_time = time.time()
    
    def is_active(self) -> bool:
        """检查任务是否处于活跃状态（等待或处理中）"""
        return self.status in [TaskStatus.PENDING, TaskStatus.PROCESSING]

@register("link_parser", "AstrBot开发者", "链接解析插件，支持解卡功能和任务排队", "1.2.0")
class LinkParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        # 从配置读取，不提供默认值（必须配置）
        self.api_key = config.get("api_key", "")
        self.api_url = config.get("api_url", "")
        self.debug_mode = config.get("debug_mode", False)
        self.max_attempts = config.get("max_attempts", 10)
        self.task_interval = config.get("task_interval", 30)
        self.max_queue_size = config.get("max_queue_size", 10)
        self.task_timeout = config.get("task_timeout", 1800)
        
        # 验证必要配置
        if not self.api_key or not self.api_url:
            logger.error("请先在配置中设置 api_key 和 api_url")
        
        # 允许的域名列表（纯域名，不带协议）
        self.allowed_domains = config.get("allowed_domains", [
            "auth.platoboost.com",
            "auth.platorelay.com", 
            "auth.platoboost.net",
            "auth.platoboost.click",
            "auth.platoboost.app",
            "auth.platoboost.me",
            "deltaios-executor.com"
        ])
        
        # 任务队列相关
        self.task_queue: asyncio.Queue = asyncio.Queue()
        self.current_task: Optional[ParseTask] = None
        self.processing_lock = asyncio.Lock()
        self.user_tasks: Dict[str, List[ParseTask]] = {}
        self.last_process_time = 0
        
        # 后台任务控制
        self._running = True
        self._processor_task = None
        
        # 创建共享的aiohttp session
        self.session = aiohttp.ClientSession()
        
        # 启动任务处理器
        self._processor_task = asyncio.create_task(self._process_task_queue())
        
        if self.debug_mode:
            logger.info("链接解析插件初始化完成")
    
    def _get_masked_key(self) -> str:
        """获取脱敏后的API key"""
        if len(self.api_key) <= 8:
            return "****"
        return self.api_key[:4] + "****" + self.api_key[-4:]
    
    def _is_allowed_domain(self, url: str) -> bool:
        """严格验证域名是否在允许列表中"""
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname or ""
            
            # 移除 www. 前缀
            if hostname.startswith('www.'):
                hostname = hostname[4:]
            
            # 检查是否在允许列表中（支持子域名）
            for domain in self.allowed_domains:
                if hostname == domain or hostname.endswith('.' + domain):
                    return True
            return False
        except Exception:
            return False
    
    async def _make_request(self, url: str) -> dict:
        """发送HTTP请求（使用共享session）"""
        try:
            # 使用params参数而不是手动拼接
            params = {
                'url': url,
                'api_key': self.api_key
            }
            
            if self.debug_mode:
                masked_key = self._get_masked_key()
                logger.info(f"请求URL: {self.api_url}, 参数: url={url}, api_key={masked_key}")
            
            async with self.session.get(self.api_url, params=params, timeout=30) as response:
                response_status = response.status
                response_text = await response.text()
                
                if self.debug_mode:
                    logger.info(f"API响应状态码: {response_status}")
                    if len(response_text) < 500:
                        logger.info(f"API响应内容: {response_text}")
                
                if response_status != 200:
                    return {
                        "success": False,
                        "message": f"API请求失败，状态码: {response_status}"
                    }
                
                return self._parse_api_response(response_text)
                
        except asyncio.TimeoutError:
            return {
                "success": False,
                "message": "请求超时"
            }
        except aiohttp.ClientError as e:
            return {
                "success": False,
                "message": f"网络请求错误: {str(e)}"
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"解析过程出错: {str(e)}"
            }
    
    @filter.command("解卡")
    async def parse_link(self, event: AstrMessageEvent, url: str):
        """
        解析链接并解卡（支持任务排队）
        """
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        
        # 验证配置
        if not self.api_key or not self.api_url:
            yield event.plain_result("❌ 插件未配置API密钥或URL，请联系管理员")
            return
        
        # 验证URL格式
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        # 验证是否为允许的域名
        if not self._is_allowed_domain(url):
            domains_list = "\n".join(self.allowed_domains)
            yield event.plain_result(f"❌ 你的链接不在允许的域名列表中\n支持的域名：\n{domains_list}")
            return
        
        # 检查队列是否已满
        current_queue_size = self.task_queue.qsize()
        if current_queue_size >= self.max_queue_size:
            yield event.plain_result(f"⚠️ 当前排队人数较多（{current_queue_size}人），请稍后再试")
            return
        
        # 清理已完成的任务记录（保留最近5条）
        if user_id in self.user_tasks:
            active_tasks = [t for t in self.user_tasks[user_id] if t.is_active()]
            completed_tasks = [t for t in self.user_tasks[user_id] if not t.is_active()][-5:]
            self.user_tasks[user_id] = active_tasks + completed_tasks
            
            # 检查活跃任务数量
            if len(active_tasks) >= 2:
                yield event.plain_result("⚠️ 你已有任务在排队中，请等待当前任务完成")
                return
        
        # 创建任务
        task = ParseTask(
            user_id=user_id,
            user_name=user_name,
            url=url,
            event_origin=event.unified_msg_origin,
            message_id=event.message_obj.message_id,
            max_attempts=self.max_attempts
        )
        
        # 获取排队位置（任务加入前）
        queue_position = self.task_queue.qsize() + 1
        
        # 添加到队列
        await self.task_queue.put(task)
        
        # 记录用户任务
        if user_id not in self.user_tasks:
            self.user_tasks[user_id] = []
        self.user_tasks[user_id].append(task)
        
        # 预估等待时间（仅队列等待）
        estimated_wait = queue_position * self.task_interval
        
        yield event.plain_result(
            f"✅ 链接已加入解析队列\n"
            f"📊 当前排队位置：第{queue_position}位\n"
            f"⏱️ 预计队列等待：约{estimated_wait}秒\n"
            f"🔄 任务最多尝试{self.max_attempts}次\n"
            f"⏰ 任务超时时间：{self.task_timeout//60}分钟"
        )
        
        if self.debug_mode:
            logger.info(f"用户 {user_name}({user_id}) 添加任务到队列，位置：{queue_position}")
    
    async def _process_task_queue(self):
        """处理任务队列的后台任务"""
        while self._running:
            try:
                # 获取下一个任务
                task: ParseTask = await self.task_queue.get()
                
                # 检查任务是否已被取消
                if task.status == TaskStatus.CANCELLED:
                    self.task_queue.task_done()
                    continue
                
                # 确保任务间隔
                current_time = time.time()
                time_since_last = current_time - self.last_process_time
                if time_since_last < self.task_interval and self.last_process_time > 0:
                    wait_time = self.task_interval - time_since_last
                    if self.debug_mode:
                        logger.info(f"等待任务间隔 {wait_time:.1f}秒")
                    await asyncio.sleep(wait_time)
                
                # 处理任务
                async with self.processing_lock:
                    self.current_task = task
                    task.status = TaskStatus.PROCESSING
                    
                    if self.debug_mode:
                        logger.info(f"开始处理任务: {task.url}, 用户: {task.user_name}")
                    
                    # 执行解析
                    success = await self._execute_parse_with_retry(task)
                    
                    # 更新任务状态（如果未被取消）
                    if task.status != TaskStatus.CANCELLED:
                        task.status = TaskStatus.SUCCESS if success else TaskStatus.FAILED
                    
                    self.last_process_time = time.time()
                    self.current_task = None
                
                self.task_queue.task_done()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"处理任务队列时出错: {str(e)}", exc_info=True)
                await asyncio.sleep(5)
    
    async def _execute_parse_with_retry(self, task: ParseTask) -> bool:
        """执行解析任务（带重试）"""
        consecutive_failures = 0
        
        while task.attempts < task.max_attempts and self._running:
            # 检查是否被取消
            if task.status == TaskStatus.CANCELLED:
                return False
            
            try:
                task.attempts += 1
                task.last_attempt_time = time.time()
                
                if self.debug_mode:
                    logger.info(f"第{task.attempts}次尝试解析: {task.url}")
                
                # 执行解析
                result = await self._make_request(task.url)
                
                # 记录错误
                if not result["success"]:
                    task.error_history.append(f"尝试{task.attempts}: {result['message']}")
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0
                
                # 解析成功
                if result["success"]:
                    await self._send_result_to_user(task, result)
                    return True
                
                # 检查是否超时
                if time.time() - task.create_time > self.task_timeout:
                    task.status = TaskStatus.TIMEOUT
                    await self._send_timeout_message(task)
                    return False
                
                # 检查连续失败
                if consecutive_failures >= 3 and task.attempts < task.max_attempts:
                    await self._send_progress_to_user(
                        task,
                        f"⚠️ 检测到连续{consecutive_failures}次失败，可能是链接已失效或服务器问题"
                    )
                
                # 计算等待时间
                wait_time = self._calculate_wait_time(result, task)
                
                # 发送重试通知
                if task.attempts < task.max_attempts:
                    await self._send_progress_to_user(
                        task,
                        self._format_retry_message(task, result, wait_time)
                    )
                    
                    if self.debug_mode:
                        logger.info(f"等待{wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)
                else:
                    # 达到最大尝试次数
                    await self._send_final_failure_message(task, result)
                    return False
                    
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"解析任务执行出错: {str(e)}", exc_info=True)
                task.error_history.append(f"异常错误: {str(e)}")
                
                if task.attempts < task.max_attempts:
                    await self._send_progress_to_user(
                        task,
                        f"❌ 解析过程出现错误: {str(e)}\n⏱️ {self.task_interval}秒后将自动重试"
                    )
                    await asyncio.sleep(self.task_interval)
                else:
                    await self._send_final_failure_message(task, {"message": f"系统错误: {str(e)}"})
                    return False
        
        return False
    
    def _parse_api_response(self, response_text: str) -> dict:
        """解析API响应"""
        if "API Offline" in response_text:
            return {
                "success": False,
                "message": "API服务暂时不可用"
            }
            
        elif "你在短时间内已经请求过同一链接了" in response_text:
            return {
                "success": False,
                "message": "请勿频繁请求同一链接"
            }
            
        elif "Invalid Delta Link" in response_text:
            return {
                "success": False,
                "message": "无效的忍者链接，请重新获取"
            }
            
        elif "该链接为过期链接，请重新获取新链接" in response_text:
            return {
                "success": False,
                "message": "链接已过期，请重新获取"
            }
            
        elif self._is_success_response(response_text):
            card_key = self._extract_value(response_text, "key")
            time_taken = self._extract_value(response_text, "time")
            
            success_msg = (
                f"✅ 解卡成功！\n"
                f"🔑 卡密：{card_key}\n"
                f"⏱️ 耗时：{time_taken}\n"
                f"🎮 祝你游玩愉快"
            )
            
            return {
                "success": True,
                "message": success_msg
            }
            
        else:
            return {
                "success": False,
                "message": "未知的响应类型"
            }
    
    def _is_success_response(self, response_text: str) -> bool:
        """判断是否为成功响应"""
        if '"status":"success"' in response_text.lower() or "'status':'success'" in response_text.lower():
            return True
        
        key_match = re.search(r'"key"\s*:\s*"([^"]+)"', response_text, re.IGNORECASE)
        time_match = re.search(r'"time"\s*:\s*"([^"]+)"', response_text, re.IGNORECASE)
        
        return bool(key_match and time_match)
    
    def _extract_value(self, text: str, key: str) -> str:
        """从响应中提取值"""
        try:
            data = json.loads(text)
            return str(data.get(key, "未知"))
        except json.JSONDecodeError:
            patterns = [
                f'"{key}"\\s*:\\s*"([^"]+)"',
                f"'{key}'\\s*:\\s*'([^']+)'",
            ]
            
            for pattern in patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    return match.group(1)
            
            return "未知"
    
    def _calculate_wait_time(self, result: dict, task: ParseTask) -> int:
        """根据失败类型计算等待时间"""
        message = result.get("message", "")
        
        if "API服务暂时不可用" in message:
            return 60
        elif "请勿频繁请求" in message:
            return 120
        elif "请求超时" in message:
            return 45
        elif "网络请求错误" in message:
            return 30
        else:
            base_wait = self.task_interval
            if task.attempts > 5:
                return base_wait * 2
            return base_wait
    
    def _format_retry_message(self, task: ParseTask, result: dict, wait_time: int) -> str:
        """格式化重试消息"""
        message = result.get("message", "未知错误")
        
        return (
            f"🔄 第{task.attempts}次尝试失败\n"
            f"❌ 原因：{message}\n"
            f"⏱️ {wait_time}秒后将进行第{task.attempts + 1}次尝试\n"
            f"📊 已尝试{task.attempts}/{task.max_attempts}次"
        )
    
    async def _send_result_to_user(self, task: ParseTask, result: dict):
        """发送结果给用户"""
        try:
            message = result["message"]
            
            chain = []
            
            # 添加引用
            if task.message_id:
                chain.append(Reply(id=task.message_id))
            
            # 添加@用户（确保是字符串）
            chain.append(At(qq=str(task.user_id)))
            
            # 添加内容
            chain.append(Plain("\n" + message))
            
            message_chain = MessageChain(chain)
            await self.context.send_message(task.event_origin, message_chain)
            
            if self.debug_mode:
                logger.info(f"已发送结果给用户 {task.user_name}")
                
        except Exception as e:
            logger.error(f"发送结果给用户失败: {str(e)}")
    
    async def _send_progress_to_user(self, task: ParseTask, message: str):
        """发送进度通知给用户"""
        try:
            chain = [
                At(qq=str(task.user_id)),
                Plain("\n" + message)
            ]
            
            message_chain = MessageChain(chain)
            await self.context.send_message(task.event_origin, message_chain)
        except Exception as e:
            logger.error(f"发送进度通知失败: {str(e)}")
    
    async def _send_timeout_message(self, task: ParseTask):
        """发送超时消息"""
        timeout_msg = (
            f"⏰ 任务已超时（超过{self.task_timeout//60}分钟）\n"
            f"❌ 链接解析失败\n"
            f"🔗 链接：{task.url}\n"
            f"📊 已尝试次数：{task.attempts}\n"
            f"💡 建议：请重新获取新链接后再试"
        )
        await self._send_result_to_user(task, {"success": False, "message": timeout_msg})
    
    async def _send_final_failure_message(self, task: ParseTask, result: dict):
        """发送最终失败消息"""
        error_history = "\n".join(task.error_history[-3:]) if task.error_history else "无"
        
        final_msg = (
            f"❌ 经过{task.max_attempts}次尝试，解析失败\n"
            f"🔗 链接：{task.url}\n"
            f"📊 最后一次错误：{result.get('message', '未知错误')}\n"
            f"📝 最近错误：\n{error_history}\n"
            f"💡 建议：\n"
            f"1. 确认链接是否有效\n"
            f"2. 重新获取新链接再试\n"
            f"3. 如果问题持续，请联系管理员"
        )
        await self._send_result_to_user(task, {"success": False, "message": final_msg})
    
    @filter.command("队列状态")
    async def queue_status(self, event: AstrMessageEvent):
        """查看队列状态"""
        queue_size = self.task_queue.qsize()
        
        status_msg = (
            f"📊 当前队列状态\n"
            f"等待任务数：{queue_size}\n"
            f"正在处理：{'是' if self.current_task else '否'}\n"
            f"任务间隔：{self.task_interval}秒\n"
            f"最大尝试次数：{self.max_attempts}次\n"
            f"任务超时：{self.task_timeout//60}分钟"
        )
        
        if self.current_task and self.current_task.status == TaskStatus.PROCESSING:
            status_msg += f"\n当前处理：{self.current_task.url[:50]}..."
            status_msg += f"\n已尝试：{self.current_task.attempts}次"
        
        yield event.plain_result(status_msg)
    
    @filter.command("取消任务")
    async def cancel_task(self, event: AstrMessageEvent):
        """取消用户的任务"""
        user_id = event.get_sender_id()
        
        if user_id not in self.user_tasks or not self.user_tasks[user_id]:
            yield event.plain_result("❌ 你当前没有正在进行的任务")
            return
        
        # 找到用户所有活跃的任务
        active_tasks = [t for t in self.user_tasks[user_id] if t.is_active()]
        
        if not active_tasks:
            yield event.plain_result("❌ 你当前没有正在等待或处理的任务")
            return
        
        # 取消任务
        cancelled_count = 0
        for task in active_tasks:
            if task.status == TaskStatus.PENDING:
                task.status = TaskStatus.CANCELLED
                cancelled_count += 1
            elif task.status == TaskStatus.PROCESSING:
                task.status = TaskStatus.CANCELLED
                cancelled_count += 1
        
        yield event.plain_result(f"✅ 已取消{cancelled_count}个待处理任务")
    
    async def terminate(self):
        """插件卸载时调用"""
        logger.info("正在卸载链接解析插件...")
        self._running = False
        
        # 取消后台任务
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
        
        # 关闭aiohttp session
        await self.session.close()
        
        # 清空任务队列
        while not self.task_queue.empty():
            try:
                self.task_queue.get_nowait()
                self.task_queue.task_done()
            except asyncio.QueueEmpty:
                break
        
        logger.info("链接解析插件已卸载")    last_attempt_time: float = None
    result: Optional[str] = None
    error_history: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        self.create_time = time.time()
    
    def is_active(self) -> bool:
        """检查任务是否处于活跃状态（等待或处理中）"""
        return self.status in [TaskStatus.PENDING, TaskStatus.PROCESSING]

@register("link_parser", "AstrBot开发者", "链接解析插件，支持解卡功能和任务排队", "1.2.0")
class LinkParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.api_key = config.get("api_key", "")  # 不再提供默认值
        self.api_url = config.get("api_url", "")
        self.debug_mode = config.get("debug_mode", 0)
        self.max_attempts = config.get("max_attempts", 10)  # 统一使用 max_attempts
        self.task_interval = config.get("task_interval", 30)
        self.max_queue_size = config.get("max_queue_size", 10)
        self.task_timeout = config.get("task_timeout", 1800)
        
        # 验证必要配置
        if not self.api_key or not self.api_url:
            logger.error("请先在配置中设置 api_key 和 api_url")
        
        # 允许的域名列表
        self.allowed_domains = config.get("allowed_domains", [
            "auth.platoboost.com",
            "auth.platorelay.com", 
            "auth.platoboost.net",
            "auth.platoboost.click",
            "auth.platoboost.app",
            "auth.platoboost.me",
            "deltaios-executor.com"
        ])
        
        # 任务队列相关
        self.task_queue: asyncio.Queue = asyncio.Queue()
        self.current_task: Optional[ParseTask] = None
        self.processing_lock = asyncio.Lock()
        self.user_tasks: Dict[str, List[ParseTask]] = {}
        self.last_process_time = 0
        
        # 保存后台任务句柄
        self._processor_task = None
        self._running = True
        
        # 创建共享的aiohttp session
        self.session = aiohttp.ClientSession()
        
        # 启动任务处理器
        self._processor_task = asyncio.create_task(self._process_task_queue())
        
        if self.debug_mode:
            logger.info(f"链接解析插件初始化完成")
    
    def _is_allowed_domain(self, url: str) -> bool:
        """严格验证域名是否在允许列表中"""
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname or ""
            
            # 移除 www. 前缀（如果需要）
            if hostname.startswith('www.'):
                hostname = hostname[4:]
            
            # 检查是否在允许列表中（支持子域名）
            for domain in self.allowed_domains:
                if hostname == domain or hostname.endswith('.' + domain):
                    return True
            return False
        except Exception:
            return False
    
    async def _make_request(self, url: str) -> dict:
        """发送HTTP请求（使用共享session）"""
        try:
            # 使用 params 参数而不是手动拼接
            params = {
                'url': url,
                'api_key': self.api_key
            }
            
            if self.debug_mode:
                # 日志中隐藏部分API key
                masked_key = self.api_key[:4] + "****" + self.api_key[-4:] if len(self.api_key) > 8 else "****"
                logger.info(f"请求URL: {self.api_url}, 参数: url={url}, api_key={masked_key}")
            
            async with self.session.get(self.api_url, params=params, timeout=30) as response:
                response_status = response.status
                response_text = await response.text()
                
                if self.debug_mode:
                    logger.info(f"API响应状态码: {response_status}")
                
                if response_status != 200:
                    return {
                        "success": False,
                        "message": f"API请求失败，状态码: {response_status}"
                    }
                
                return self._parse_api_response(response_text)
                
        except asyncio.TimeoutError:
            return {
                "success": False,
                "message": "请求超时"
            }
        except aiohttp.ClientError as e:
            return {
                "success": False,
                "message": f"网络请求错误: {str(e)}"
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"解析过程出错: {str(e)}"
            }
    
    @filter.command("解卡")
    async def parse_link(self, event: AstrMessageEvent, url: str):
        """
        解析链接并解卡（支持任务排队）
        """
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        
        # 验证配置
        if not self.api_key or not self.api_url:
            yield event.plain_result("❌ 插件未配置API密钥或URL，请联系管理员")
            return
        
        # 验证URL格式
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        # 验证是否为允许的域名
        if not self._is_allowed_domain(url):
            domains_list = "\n".join(self.allowed_domains)
            yield event.plain_result(f"❌ 你的链接不在允许的域名列表中\n支持的域名：\n{domains_list}")
            return
        
        # 检查队列是否已满
        current_queue_size = self.task_queue.qsize()
        if current_queue_size >= self.max_queue_size:
            yield event.plain_result(f"⚠️ 当前排队人数较多（{current_queue_size}人），请稍后再试")
            return
        
        # 清理已完成的任务记录（保留最近10条）
        if user_id in self.user_tasks:
            # 只保留未完成的任务和最近5条已完成的任务
            active_tasks = [t for t in self.user_tasks[user_id] if t.is_active()]
            completed_tasks = [t for t in self.user_tasks[user_id] if not t.is_active()][-5:]
            self.user_tasks[user_id] = active_tasks + completed_tasks
            
            # 检查活跃任务数量
            if len(active_tasks) >= 2:
                yield event.plain_result("⚠️ 你已有任务在排队中，请等待当前任务完成")
                return
        
        # 创建任务
        task = ParseTask(
            user_id=user_id,
            user_name=user_name,
            url=url,
            event_origin=event.unified_msg_origin,
            message_id=event.message_obj.message_id,
            max_attempts=self.max_attempts
        )
        
        # 添加到队列前先获取当前队列大小作为排队位置
        queue_position = self.task_queue.qsize() + 1
        
        # 添加到队列
        await self.task_queue.put(task)
        
        # 记录用户任务
        if user_id not in self.user_tasks:
            self.user_tasks[user_id] = []
        self.user_tasks[user_id].append(task)
        
        # 预估等待时间（只考虑队列等待，实际会更长）
        estimated_wait = queue_position * self.task_interval
        
        yield event.plain_result(
            f"✅ 链接已加入解析队列\n"
            f"📊 当前排队位置：第{queue_position}位\n"
            f"⏱️ 预计队列等待：约{estimated_wait}秒\n"
            f"🔄 任务最多尝试{self.max_attempts}次\n"
            f"⏰ 任务超时时间：{self.task_timeout//60}分钟"
        )
        
        if self.debug_mode:
            logger.info(f"用户 {user_name}({user_id}) 添加任务到队列，位置：{queue_position}")
    
    async def _process_task_queue(self):
        """处理任务队列的后台任务"""
        while self._running:
            try:
                # 获取下一个任务
                task: ParseTask = await self.task_queue.get()
                
                # 检查任务是否已被取消
                if task.status == TaskStatus.CANCELLED:
                    self.task_queue.task_done()
                    continue
                
                # 确保任务间隔
                current_time = time.time()
                time_since_last = current_time - self.last_process_time
                if time_since_last < self.task_interval and self.last_process_time > 0:
                    wait_time = self.task_interval - time_since_last
                    if self.debug_mode:
                        logger.info(f"等待任务间隔 {wait_time:.1f}秒")
                    await asyncio.sleep(wait_time)
                
                # 处理任务
                async with self.processing_lock:
                    self.current_task = task
                    task.status = TaskStatus.PROCESSING
                    
                    if self.debug_mode:
                        logger.info(f"开始处理任务: {task.url}, 用户: {task.user_name}")
                    
                    # 执行解析，带重试
                    success = await self._execute_parse_with_retry(task)
                    
                    # 更新任务状态
                    if success:
                        task.status = TaskStatus.SUCCESS
                    else:
                        task.status = TaskStatus.FAILED
                    
                    self.last_process_time = time.time()
                    self.current_task = None
                    
                self.task_queue.task_done()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"处理任务队列时出错: {str(e)}", exc_info=True)
                await asyncio.sleep(5)
    
    async def _execute_parse_with_retry(self, task: ParseTask) -> bool:
        """执行解析任务（带重试）"""
        consecutive_failures = 0
        
        while task.total_attempts < task.max_attempts and self._running:
            # 再次检查是否被取消
            if task.status == TaskStatus.CANCELLED:
                return False
            
            try:
                task.total_attempts += 1
                task.last_attempt_time = time.time()
                
                if self.debug_mode:
                    logger.info(f"第{task.total_attempts}次尝试解析: {task.url}")
                
                # 执行解析
                result = await self._make_request(task.url)
                
                # 记录错误
                if not result["success"]:
                    task.error_history.append(f"尝试{task.total_attempts}: {result['message']}")
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0
                
                # 解析成功
                if result["success"]:
                    await self._send_result_to_user(task, result)
                    return True
                
                # 检查是否超时
                if time.time() - task.create_time > self.task_timeout:
                    task.status = TaskStatus.TIMEOUT
                    await self._send_timeout_message(task)
                    return False
                
                # 检查连续失败
                if consecutive_failures >= 3 and task.total_attempts < task.max_attempts:
                    await self._send_progress_to_user(
                        task,
                        f"⚠️ 检测到连续{consecutive_failures}次失败，可能是链接已失效或服务器问题"
                    )
                
                # 计算等待时间
                wait_time = self._calculate_wait_time(result, task)
                
                # 发送重试通知
                if task.total_attempts < task.max_attempts:
                    await self._send_progress_to_user(
                        task,
                        self._format_retry_message(task, result, wait_time)
                    )
                    
                    if self.debug_mode:
                        logger.info(f"等待{wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)
                else:
                    # 达到最大尝试次数
                    await self._send_final_failure_message(task, result)
                    return False
                    
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"解析任务执行出错: {str(e)}", exc_info=True)
                task.error_history.append(f"异常错误: {str(e)}")
                
                if task.total_attempts < task.max_attempts:
                    await self._send_progress_to_user(
                        task,
                        f"❌ 解析过程出现错误: {str(e)}\n⏱️ {self.task_interval}秒后将自动重试"
                    )
                    await asyncio.sleep(self.task_interval)
                else:
                    await self._send_final_failure_message(task, {"message": f"系统错误: {str(e)}"})
                    return False
        
        return False
    
    def _parse_api_response(self, response_text: str) -> dict:
        """解析API响应"""
        if "API Offline" in response_text:
            return {
                "success": False,
                "message": "API服务暂时不可用"
            }
            
        elif "你在短时间内已经请求过同一链接了" in response_text:
            return {
                "success": False,
                "message": "请勿频繁请求同一链接"
            }
            
        elif "Invalid Delta Link" in response_text:
            return {
                "success": False,
                "message": "无效的忍者链接，请重新获取"
            }
            
        elif "该链接为过期链接，请重新获取新链接" in response_text:
            return {
                "success": False,
                "message": "链接已过期，请重新获取"
            }
            
        elif self._is_success_response(response_text):
            card_key = self._extract_value(response_text, "key", "卡密")
            time_taken = self._extract_value(response_text, "time", "耗时")
            
            success_msg = (
                f"✅ 解卡成功！\n"
                f"🔑 卡密：{card_key}\n"
                f"⏱️ 耗时：{time_taken}\n"
                f"🎮 祝你游玩愉快"
            )
            
            return {
                "success": True,
                "message": success_msg,
                "card_key": card_key,
                "time_taken": time_taken
            }
            
        else:
            return {
                "success": False,
                "message": "未知的响应类型"
            }
    
    def _is_success_response(self, response_text: str) -> bool:
        """判断是否为成功响应"""
        if '"status":"success"' in response_text.lower() or "'status':'success'" in response_text.lower():
            return True
        
        key_match = re.search(r'"key"\s*:\s*"([^"]+)"', response_text, re.IGNORECASE)
        time_match = re.search(r'"time"\s*:\s*"([^"]+)"', response_text, re.IGNORECASE)
        
        return bool(key_match and time_match)
    
    def _extract_value(self, text: str, key: str, display_name: str = "") -> str:
        """从响应中提取值"""
        try:
            data = json.loads(text)
            return str(data.get(key, "未知"))
        except json.JSONDecodeError:
            patterns = [
                f'"{key}"\\s*:\\s*"([^"]+)"',
                f"'{key}'\\s*:\\s*'([^']+)'",
                f'{key}\\s*=\\s*"([^"]+)"',
                f'{key}\\s*:\\s*"([^"]+)"',
            ]
            
            for pattern in patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    return match.group(1)
            
            return "未知"
    
    def _calculate_wait_time(self, result: dict, task: ParseTask) -> int:
        """根据失败类型计算等待时间"""
        message = result.get("message", "")
        
        if "API服务暂时不可用" in message:
            return 60
        elif "请勿频繁请求" in message:
            return 120
        elif "请求超时" in message:
            return 45
        elif "网络请求错误" in message:  # 修正匹配条件
            return 30
        else:
            base_wait = self.task_interval
            if task.total_attempts > 5:
                return base_wait * 2
            return base_wait
    
    def _format_retry_message(self, task: ParseTask, result: dict, wait_time: int) -> str:
        """格式化重试消息"""
        message = result.get("message", "未知错误")
        
        msg = f"🔄 第{task.total_attempts}次尝试失败\n"
        msg += f"❌ 原因：{message}\n"
        msg += f"⏱️ {wait_time}秒后将进行第{task.total_attempts + 1}次尝试\n"
        msg += f"📊 已尝试{task.total_attempts}/{task.max_attempts}次"
        
        return msg
    
    async def _send_result_to_user(self, task: ParseTask, result: dict):
        """发送结果给用户"""
        try:
            message = result["message"]
            
            # 构建消息链
            chain = []
            
            # 添加引用
            if task.message_id:
                chain.append(Reply(id=task.message_id))
            
            # 添加@用户（确保user_id是字符串）
            chain.append(At(qq=str(task.user_id)))
            
            # 添加内容
            chain.append(Plain("\n" + message))
            
            message_chain = MessageChain(chain)
            await self.context.send_message(task.event_origin, message_chain)
            
            if self.debug_mode:
                logger.info(f"已发送结果给用户 {task.user_name}")
                
        except Exception as e:
            logger.error(f"发送结果给用户失败: {str(e)}")
    
    async def _send_progress_to_user(self, task: ParseTask, message: str):
        """发送进度通知给用户"""
        try:
            chain = [
                At(qq=str(task.user_id)),
                Plain("\n" + message)
            ]
            
            message_chain = MessageChain(chain)
            await self.context.send_message(task.event_origin, message_chain)
        except Exception as e:
            logger.error(f"发送进度通知失败: {str(e)}")
    
    async def _send_timeout_message(self, task: ParseTask):
        """发送超时消息"""
        timeout_msg = (
            f"⏰ 任务已超时（超过{self.task_timeout//60}分钟）\n"
            f"❌ 链接解析失败\n"
            f"🔗 链接：{task.url}\n"
            f"📊 已尝试次数：{task.total_attempts}\n"
            f"💡 建议：请重新获取新链接后再试"
        )
        await self._send_result_to_user(task, {"success": False, "message": timeout_msg})
    
    async def _send_final_failure_message(self, task: ParseTask, result: dict):
        """发送最终失败消息"""
        error_history = "\n".join(task.error_history[-3:]) if task.error_history else "无"
        
        final_msg = (
            f"❌ 经过{task.max_attempts}次尝试，解析失败\n"
            f"🔗 链接：{task.url}\n"
            f"📊 最后一次错误：{result.get('message', '未知错误')}\n"
            f"📝 最近错误：\n{error_history}\n"
            f"💡 建议：\n"
            f"1. 确认链接是否有效\n"
            f"2. 重新获取新链接再试\n"
            f"3. 如果问题持续，请联系管理员"
        )
        await self._send_result_to_user(task, {"success": False, "message": final_msg})
    
    @filter.command("队列状态")
    async def queue_status(self, event: AstrMessageEvent):
        """查看队列状态"""
        queue_size = self.task_queue.qsize()
        
        status_msg = (
            f"📊 当前队列状态\n"
            f"等待任务数：{queue_size}\n"
            f"正在处理：{'是' if self.current_task else '否'}\n"
            f"任务间隔：{self.task_interval}秒\n"
            f"最大尝试次数：{self.max_attempts}次\n"
            f"任务超时：{self.task_timeout//60}分钟"
        )
        
        if self.current_task and self.current_task.status == TaskStatus.PROCESSING:
            status_msg += f"\n当前处理：{self.current_task.url[:50]}..."
            status_msg += f"\n已尝试：{self.current_task.total_attempts}次"
        
        yield event.plain_result(status_msg)
    
    @filter.command("取消任务")
    async def cancel_task(self, event: AstrMessageEvent):
        """取消用户的任务"""
        user_id = event.get_sender_id()
        
        if user_id not in self.user_tasks or not self.user_tasks[user_id]:
            yield event.plain_result("❌ 你当前没有正在进行的任务")
            return
        
        # 找到用户所有待处理的任务
        active_tasks = [t for t in self.user_tasks[user_id] if t.is_active()]
        
        if not active_tasks:
            yield event.plain_result("❌ 你当前没有正在等待或处理的任务")
            return
        
        # 取消任务
        cancelled_count = 0
        for task in active_tasks:
            if task.status == TaskStatus.PENDING:
                task.status = TaskStatus.CANCELLED
                cancelled_count += 1
            elif task.status == TaskStatus.PROCESSING and task == self.current_task:
                # 正在处理的任务不能立即取消，但标记为取消
                task.status = TaskStatus.CANCELLED
                cancelled_count += 1
                # 注意：正在执行的任务会在下一次重试前检查状态并退出
        
        yield event.plain_result(f"✅ 已取消{cancelled_count}个待处理任务")
    
    async def terminate(self):
        """插件卸载时调用"""
        logger.info("正在卸载链接解析插件...")
        self._running = False
        
        # 取消后台任务
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
        
        # 关闭aiohttp session
        await self.session.close()
        
        # 清空任务队列
        while not self.task_queue.empty():
            try:
                self.task_queue.get_nowait()
                self.task_queue.task_done()
            except asyncio.QueueEmpty:
                break
        
        logger.info("链接解析插件已卸载")    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.api_key = config.get("api_key", "User_LATstudio_3890860058_tbIqbza5C7")
        self.api_url = config.get("api_url", "https://api.bypass.ceo/bypass/qq-bot/delta/q-bot")
        self.debug_mode = config.get("debug_mode", 0)
        self.max_retries = config.get("max_retries", 3)
        self.task_interval = config.get("task_interval", 30)
        self.max_queue_size = config.get("max_queue_size", 10)
        self.max_total_attempts = config.get("max_total_attempts", 10)
        self.task_timeout = config.get("task_timeout", 1800)
        
        # 允许的域名列表
        self.allowed_domains = config.get("allowed_domains", [
            "https://auth.platoboost.com",
            "https://auth.platorelay.com", 
            "https://auth.platoboost.net",
            "https://auth.platoboost.click",
            "https://auth.platoboost.app",
            "https://auth.platoboost.me",
            "https://deltaios-executor.com"
        ])
        
        # 任务队列相关
        self.task_queue: asyncio.Queue = asyncio.Queue()
        self.current_task: Optional[ParseTask] = None
        self.processing_lock = asyncio.Lock()
        self.user_tasks: Dict[str, List[ParseTask]] = {}
        self.last_process_time = 0
        
        # 启动任务处理器
        asyncio.create_task(self._process_task_queue())
        
        if self.debug_mode:
            logger.info(f"链接解析插件初始化完成")
    
    @filter.command("解卡")
    async def parse_link(self, event: AstrMessageEvent, url: str):
        """
        解析链接并解卡（支持任务排队）
        """
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        
        # 验证URL格式
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        # 验证是否为允许的域名
        if not self._is_allowed_domain(url):
            domains_list = "\n".join(self.allowed_domains)
            yield event.plain_result(f"❌ 你的链接不是忍者链接，请重新尝试\n支持的域名：\n{domains_list}")
            return
        
        # 检查队列是否已满
        current_queue_size = self.task_queue.qsize()
        if current_queue_size >= self.max_queue_size:
            yield event.plain_result(f"⚠️ 当前排队人数较多（{current_queue_size}人），请稍后再试")
            return
        
        # 检查用户是否已有任务在排队
        if user_id in self.user_tasks:
            pending_tasks = [t for t in self.user_tasks[user_id] if t.status in ["pending", "processing"]]
            if len(pending_tasks) >= 2:
                yield event.plain_result("⚠️ 你已有任务在排队中，请等待当前任务完成")
                return
        
        # 创建任务
        task = ParseTask(
            user_id=user_id,
            user_name=user_name,
            url=url,
            event_origin=event.unified_msg_origin,
            message_id=event.message_obj.message_id,
            max_retries=self.max_retries,
            max_total_attempts=self.max_total_attempts
        )
        
        # 添加到队列前先获取当前队列大小作为排队位置
        queue_position = self.task_queue.qsize() + 1
        
        # 添加到队列
        await self.task_queue.put(task)
        
        # 记录用户任务
        if user_id not in self.user_tasks:
            self.user_tasks[user_id] = []
        self.user_tasks[user_id].append(task)
        
        # 预估等待时间
        estimated_wait = queue_position * self.task_interval
        
        yield event.plain_result(
            f"✅ 链接已加入解析队列\n"
            f"📊 当前排队位置：第{queue_position}位\n"
            f"⏱️ 预计等待时间：约{estimated_wait}秒\n"
            f"🔄 任务将自动重试{self.max_retries}次，总尝试{self.max_total_attempts}次\n"
            f"⏰ 任务超时时间：{self.task_timeout//60}分钟"
        )
        
        if self.debug_mode:
            logger.info(f"用户 {user_name}({user_id}) 添加任务到队列，位置：{queue_position}")
    
    def _is_allowed_domain(self, url: str) -> bool:
        """验证是否为允许的域名"""
        for domain in self.allowed_domains:
            if url.startswith(domain):
                return True
        return False
    
    async def _process_task_queue(self):
        """处理任务队列的后台任务"""
        while True:
            try:
                # 获取下一个任务
                task: ParseTask = await self.task_queue.get()
                
                # 确保任务间隔
                current_time = time.time()
                time_since_last = current_time - self.last_process_time
                if time_since_last < self.task_interval and self.last_process_time > 0:
                    wait_time = self.task_interval - time_since_last
                    if self.debug_mode:
                        logger.info(f"等待任务间隔 {wait_time:.1f}秒")
                    await asyncio.sleep(wait_time)
                
                # 处理任务
                async with self.processing_lock:
                    self.current_task = task
                    task.status = "processing"
                    
                    if self.debug_mode:
                        logger.info(f"开始处理任务: {task.url}, 用户: {task.user_name}")
                    
                    # 执行解析，带重试
                    success = await self._execute_parse_with_retry(task)
                    
                    # 更新任务状态
                    if success:
                        task.status = "success"
                        if self.debug_mode:
                            logger.info(f"任务处理成功: {task.url}")
                    else:
                        task.status = "failed"
                        if self.debug_mode:
                            logger.warning(f"任务处理失败: {task.url}")
                    
                    self.last_process_time = time.time()
                    self.current_task = None
                    
            except Exception as e:
                logger.error(f"处理任务队列时出错: {str(e)}", exc_info=True)
                await asyncio.sleep(5)
    
    async def _execute_parse_with_retry(self, task: ParseTask) -> bool:
        """执行解析任务（带重试）"""
        consecutive_failures = 0
        
        while task.total_attempts < task.max_total_attempts:
            try:
                task.total_attempts += 1
                task.retry_count += 1
                task.last_attempt_time = time.time()
                
                if self.debug_mode:
                    logger.info(f"第{task.total_attempts}次尝试解析: {task.url}")
                
                # 执行解析
                result = await self._execute_single_parse(task.url)
                
                # 记录错误
                if not result["success"]:
                    task.error_history.append(f"尝试{task.total_attempts}: {result['message']}")
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0
                
                # 解析成功
                if result["success"]:
                    await self._send_result_to_user(task, result)
                    return True
                
                # 检查是否超时
                if time.time() - task.create_time > self.task_timeout:
                    await self._send_timeout_message(task)
                    return False
                
                # 检查连续失败
                if consecutive_failures >= 3 and task.total_attempts < task.max_total_attempts:
                    await self._send_progress_to_user(
                        task,
                        f"⚠️ 检测到连续{consecutive_failures}次失败，可能是链接已失效或服务器问题"
                    )
                
                # 计算等待时间
                wait_time = self._calculate_wait_time(result, task)
                
                # 发送重试通知
                if task.total_attempts < task.max_total_attempts:
                    await self._send_progress_to_user(
                        task,
                        self._format_retry_message(task, result, wait_time)
                    )
                    
                    if self.debug_mode:
                        logger.info(f"等待{wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)
                else:
                    # 达到最大尝试次数
                    await self._send_final_failure_message(task, result)
                    return False
                    
            except Exception as e:
                logger.error(f"解析任务执行出错: {str(e)}", exc_info=True)
                task.error_history.append(f"异常错误: {str(e)}")
                
                if task.total_attempts < task.max_total_attempts:
                    await self._send_progress_to_user(
                        task,
                        f"❌ 解析过程出现错误: {str(e)}\n⏱️ {self.task_interval}秒后将自动重试"
                    )
                    await asyncio.sleep(self.task_interval)
                else:
                    await self._send_final_failure_message(task, {"message": f"系统错误: {str(e)}"})
                    return False
        
        return False
    
    async def _execute_single_parse(self, url: str) -> dict:
        """执行单次解析"""
        try:
            request_url = f"{self.api_url}?url={url}&api_key={self.api_key}"
            
            if self.debug_mode:
                logger.info(f"请求URL: {request_url}")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(request_url, timeout=30) as response:
                    response_status = response.status
                    response_text = await response.text()
                    
                    if self.debug_mode:
                        logger.info(f"API响应状态码: {response_status}")
                        if len(response_text) < 500:
                            logger.info(f"API响应内容: {response_text}")
                    
                    if response_status != 200:
                        return {
                            "success": False,
                            "message": f"API请求失败，状态码: {response_status}"
                        }
                    
                    return self._parse_api_response(response_text)
                    
        except asyncio.TimeoutError:
            return {
                "success": False,
                "message": "请求超时"
            }
        except aiohttp.ClientError as e:
            return {
                "success": False,
                "message": f"网络请求错误: {str(e)}"
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"解析过程出错: {str(e)}"
            }
    
    def _parse_api_response(self, response_text: str) -> dict:
        """解析API响应"""
        # 判断响应类型
        if "API Offline" in response_text:
            return {
                "success": False,
                "message": "API服务暂时不可用"
            }
            
        elif "你在短时间内已经请求过同一链接了" in response_text:
            return {
                "success": False,
                "message": "请勿频繁请求同一链接"
            }
            
        elif "Invalid Delta Link" in response_text:
            return {
                "success": False,
                "message": "无效的忍者链接，请重新获取"
            }
            
        elif "该链接为过期链接，请重新获取新链接" in response_text:
            return {
                "success": False,
                "message": "链接已过期，请重新获取"
            }
            
        elif self._is_success_response(response_text):
            card_key = self._extract_value(response_text, "key", "卡密")
            time_taken = self._extract_value(response_text, "time", "耗时")
            
            success_msg = (
                f"✅ 解卡成功！\n"
                f"🔑 卡密：{card_key}\n"
                f"⏱️ 耗时：{time_taken}\n"
                f"🎮 祝你游玩愉快"
            )
            
            return {
                "success": True,
                "message": success_msg,
                "card_key": card_key,
                "time_taken": time_taken
            }
            
        else:
            return {
                "success": False,
                "message": "未知的响应类型"
            }
    
    def _is_success_response(self, response_text: str) -> bool:
        """判断是否为成功响应"""
        if '"status":"success"' in response_text.lower() or "'status':'success'" in response_text.lower():
            return True
        
        key_match = re.search(r'"key"\s*:\s*"([^"]+)"', response_text, re.IGNORECASE)
        time_match = re.search(r'"time"\s*:\s*"([^"]+)"', response_text, re.IGNORECASE)
        
        return bool(key_match and time_match)
    
    def _extract_value(self, text: str, key: str, display_name: str = "") -> str:
        """从响应中提取值"""
        try:
            data = json.loads(text)
            return str(data.get(key, "未知"))
        except json.JSONDecodeError:
            patterns = [
                f'"{key}"\\s*:\\s*"([^"]+)"',
                f"'{key}'\\s*:\\s*'([^']+)'",
                f'{key}\\s*=\\s*"([^"]+)"',
                f'{key}\\s*:\\s*"([^"]+)"',
            ]
            
            for pattern in patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    return match.group(1)
            
            return "未知"
    
    def _calculate_wait_time(self, result: dict, task: ParseTask) -> int:
        """根据失败类型计算等待时间"""
        message = result.get("message", "")
        
        if "API服务暂时不可用" in message:
            return 60
        elif "请勿频繁请求" in message:
            return 120
        elif "请求超时" in message:
            return 45
        elif "网络连接失败" in message:
            return 30
        else:
            base_wait = self.task_interval
            if task.total_attempts > 5:
                return base_wait * 2
            return base_wait
    
    def _format_retry_message(self, task: ParseTask, result: dict, wait_time: int) -> str:
        """格式化重试消息"""
        message = result.get("message", "未知错误")
        
        msg = f"🔄 第{task.total_attempts}次尝试失败\n"
        msg += f"❌ 原因：{message}\n"
        msg += f"⏱️ {wait_time}秒后将进行第{task.total_attempts + 1}次尝试\n"
        msg += f"📊 已尝试{task.total_attempts}/{task.max_total_attempts}次"
        
        return msg
    
    async def _send_result_to_user(self, task: ParseTask, result: dict):
        """发送结果给用户 - 添加@和引用"""
        try:
            message = result["message"]
            
            if not result["success"] and task.retry_count >= task.max_retries:
                message = f"❌ 经过{task.max_retries}次尝试，解析失败\n{message}"
            
            # 构建消息链 - 使用列表形式
            chain = []
            
            # 添加引用（如果有消息ID）
            if task.message_id:
                chain.append(Reply(id=task.message_id))
            
            # 添加@用户
            chain.append(At(qq=task.user_id))
            
            # 添加内容
            chain.append(Plain("\n" + message))
            
            # 使用MessageChain构建
            message_chain = MessageChain(chain)
            
            await self.context.send_message(task.event_origin, message_chain)
            
            if self.debug_mode:
                logger.info(f"已发送结果给用户 {task.user_name}")
                
        except Exception as e:
            logger.error(f"发送结果给用户失败: {str(e)}")
    
    async def _send_progress_to_user(self, task: ParseTask, message: str):
        """发送进度通知给用户 - 只添加@"""
        try:
            chain = [
                At(qq=task.user_id),
                Plain("\n" + message)
            ]
            
            message_chain = MessageChain(chain)
            await self.context.send_message(task.event_origin, message_chain)
        except Exception as e:
            logger.error(f"发送进度通知失败: {str(e)}")
    
    async def _send_timeout_message(self, task: ParseTask):
        """发送超时消息"""
        timeout_msg = (
            f"⏰ 任务已超时（超过{self.task_timeout//60}分钟）\n"
            f"❌ 链接解析失败\n"
            f"🔗 链接：{task.url}\n"
            f"📊 已尝试次数：{task.total_attempts}\n"
            f"💡 建议：请重新获取新链接后再试"
        )
        await self._send_result_to_user(task, {"success": False, "message": timeout_msg})
    
    async def _send_final_failure_message(self, task: ParseTask, result: dict):
        """发送最终失败消息"""
        error_history = "\n".join(task.error_history[-3:]) if task.error_history else "无"
        
        final_msg = (
            f"❌ 经过{task.max_total_attempts}次尝试，解析失败\n"
            f"🔗 链接：{task.url}\n"
            f"📊 最后一次错误：{result.get('message', '未知错误')}\n"
            f"📝 最近错误：\n{error_history}\n"
            f"💡 建议：\n"
            f"1. 确认链接是否有效\n"
            f"2. 重新获取新链接再试\n"
            f"3. 如果问题持续，请联系管理员"
        )
        await self._send_result_to_user(task, {"success": False, "message": final_msg})
    
    @filter.command("队列状态")
    async def queue_status(self, event: AstrMessageEvent):
        """查看队列状态"""
        queue_size = self.task_queue.qsize()
        
        status_msg = (
            f"📊 当前队列状态\n"
            f"等待任务数：{queue_size}\n"
            f"正在处理：{'是' if self.current_task else '否'}\n"
            f"任务间隔：{self.task_interval}秒\n"
            f"最大重试：{self.max_retries}次\n"
            f"总尝试次数：{self.max_total_attempts}次\n"
            f"任务超时：{self.task_timeout//60}分钟"
        )
        
        if self.current_task:
            status_msg += f"\n当前处理：{self.current_task.url[:50]}..."
            status_msg += f"\n已尝试：{self.current_task.total_attempts}次"
        
        yield event.plain_result(status_msg)
    
    @filter.command("取消任务")
    async def cancel_task(self, event: AstrMessageEvent):
        """取消用户的任务"""
        user_id = event.get_sender_id()
        
        if user_id not in self.user_tasks or not self.user_tasks[user_id]:
            yield event.plain_result("❌ 你当前没有正在进行的任务")
            return
        
        pending_tasks = [t for t in self.user_tasks[user_id] if t.status in ["pending", "processing"]]
        
        if not pending_tasks:
            yield event.plain_result("❌ 你当前没有正在等待或处理的任务")
            return
        
        cancelled_count = 0
        for task in pending_tasks:
            if task.status == "pending":
                task.status = "cancelled"
                cancelled_count += 1
        
        yield event.plain_result(f"✅ 已取消{cancelled_count}个待处理任务")
    
    async def terminate(self):
        """插件卸载时调用"""
        logger.info("链接解析插件已卸载")
        while not self.task_queue.empty():
            try:
                self.task_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
