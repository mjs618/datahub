import asyncio
import logging
import time
from asyncua import Client, ua


class OPCUAController:
    """
    OPC UA 客户端控制器。

    设计要点：
    - 非阻塞自动重连：ensure_connected() 在断开时立即返回 False，由后台任务持续重连，
      不会阻塞主循环（避免历史“重试 19 次主循环卡死”问题）。
    - 指数退避：重连间隔按 backoff_factor 递增，封顶于 max_reconnect_interval，
      网络长时间异常时降低无效重试频率。
    - 连接超时：connect / read / write 均包 asyncio.wait_for，服务端无响应不会卡死。
    - 后台健康检查：定期读取标准节点 Server_ServerStatus (i=2256)，
      主动发现“TCP 存活但 session 失效”的半开连接。
    - 状态统计：通过 stats 属性暴露连接 uptime / downtime / 重连次数等，供 Web UI 与健康端点展示。
    - 手动重连：reconnect_now() 供 Web UI 立即触发一次重连。
    """

    # 标准 OPC UA 节点：Server.ServerStatus（OPC UA 规范必选）
    _SERVER_STATUS_NODE_ID = "i=2256"

    def __init__(self, url,
                 reconnect_interval=5.0,
                 max_reconnect_attempts=0,
                 max_reconnect_interval=300.0,
                 backoff_factor=2.0,
                 connect_timeout=10.0,
                 request_timeout=10.0,
                 health_check_interval=30.0):
        self.url = url
        self.reconnect_interval = reconnect_interval
        self.max_reconnect_interval = max_reconnect_interval
        self.backoff_factor = backoff_factor
        # 0 表示无限重连
        self.max_reconnect_attempts = max_reconnect_attempts
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.health_check_interval = health_check_interval

        self.client = None
        self.logger = logging.getLogger(__name__)

        self._connected = False
        self._reconnect_attempts = 0
        # 保护连接/断开/重连的临界区，避免后台重连与手动重连/读写并发冲突
        self._lock = asyncio.Lock()

        # 后台任务
        self._reconnect_task = None
        self._health_check_task = None
        self._stop_event = asyncio.Event()

        # 连接状态指标
        self._connected_since = None        # 当前连接建立时间戳
        self._disconnected_since = None     # 当前断开时间戳
        self._last_disconnect_time = None   # 最近一次断开时间戳
        self._total_reconnect_attempts = 0  # 累计重连尝试次数
        self._successful_reconnects = 0     # 成功重连次数
        self._unexpected_disconnects = 0    # 非预期断开次数（读写/健康检查失败导致的掉线）

    # ------------------------------------------------------------------
    # 公共属性
    # ------------------------------------------------------------------

    @property
    def connected(self):
        return self._connected

    @property
    def stats(self):
        """返回连接状态统计快照（供 Web UI / health 展示）。"""
        now = time.time()
        uptime = (now - self._connected_since) if self._connected and self._connected_since else 0
        downtime = (now - self._disconnected_since) if (not self._connected) and self._disconnected_since else 0
        return {
            "connected": self._connected,
            "url": self.url,
            "connected_since": self._connected_since,
            "disconnected_since": self._disconnected_since,
            "last_disconnect_time": self._last_disconnect_time,
            "uptime_seconds": round(uptime, 1),
            "downtime_seconds": round(downtime, 1),
            "reconnect_attempts_current": self._reconnect_attempts,
            "total_reconnect_attempts": self._total_reconnect_attempts,
            "successful_reconnects": self._successful_reconnects,
            "unexpected_disconnects": self._unexpected_disconnects,
            "reconnect_task_running": self._reconnect_task is not None and not self._reconnect_task.done(),
            "health_check_task_running": (
                self._health_check_task is not None and not self._health_check_task.done()
            ),
        }

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------

    async def connect(self):
        """首次连接（带超时）。失败抛异常。成功后启动后台健康检查。"""
        async with self._lock:
            await self._do_connect()
        self._start_health_check()

    async def ensure_connected(self):
        """
        确保连接可用。
        - 已连接：立即返回 True
        - 未连接：触发后台重连任务（如未运行）并立即返回 False，不阻塞调用方

        主循环可据此跳过本周期读写，下个周期再尝试。
        """
        if self._connected:
            return True
        self._ensure_reconnect_task()
        return False

    async def reconnect_now(self):
        """
        立即重连（Web UI 调用）。
        取消当前后台重连任务，立即尝试一次；失败则重启后台重连。
        返回 True 表示成功，False 表示失败。
        """
        self.logger.info("OPC UA manual reconnect requested")
        await self._cancel_reconnect_task()
        async with self._lock:
            try:
                await self._do_connect()
                self._start_health_check()
                return True
            except Exception as e:
                self.logger.warning(f"Manual reconnect failed: {e}")
                self._ensure_reconnect_task()
                return False

    async def disconnect(self):
        """停止后台任务并断开连接（优雅关闭）。"""
        self._stop_event.set()
        await self._cancel_reconnect_task()
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
        self._health_check_task = None
        # 统一通过 _cleanup_client 释放底层连接
        await self._cleanup_client()
        # was_connected=False：关闭流程不算非预期断开
        self._set_disconnected(was_connected=False)
        self.logger.info("Disconnected from OPC UA server")

    # ------------------------------------------------------------------
    # 读写操作
    # ------------------------------------------------------------------

    async def read_trigger(self, trigger_node_id):
        """
        读取触发信号。
        返回：
            True/False - 成功读取到的布尔值
            None       - 读取失败（连接断开或异常）
        失败时返回 None 而非 False，避免被误判为下降沿。
        """
        value = await self.read_value(trigger_node_id)
        if value is None:
            return None
        return bool(value)

    async def read_value(self, node_id):
        """
        通用读单点。
        返回：
            value - 成功读取到的原始值（bool/int/float/str 等）
            None  - 读取失败（连接断开或异常）
        """
        if not await self.ensure_connected():
            return None
        try:
            node = self.client.get_node(node_id)
            value = await asyncio.wait_for(node.read_value(), timeout=self.request_timeout)
            return value
        except ua.UaStatusCodeError as e:
            # 节点级错误（BadNodeIdUnknown 等）通常不代表连接断开，不触发重连
            self.logger.error(f"Error reading OPCUA node {node_id}: {e}")
            return None
        except asyncio.TimeoutError:
            self.logger.error(f"Timeout reading OPCUA node {node_id} ({self.request_timeout}s)")
            await self._mark_disconnected_unexpected()
            return None
        except Exception as e:
            self.logger.error(f"Error reading OPCUA node {node_id}: {e}")
            await self._mark_disconnected_unexpected()
            return None

    async def read_values(self, node_ids):
        """
        批量读取多个节点。
        返回 dict: {node_id: value}。
        读取失败的节点 value 为 None（不抛异常，不中断其余节点）。
        空列表返回空 dict。
        内部顺序读取以保证兼容性与稳定的失败隔离；12 个节点量级下开销可忽略。
        """
        result = {}
        if not node_ids:
            return result
        for nid in node_ids:
            result[nid] = await self.read_value(nid)
        return result

    async def write_value(self, node_id, value):
        """
        通用写单点。
        返回 True 表示写入成功，False 表示失败（连接断开或异常）。
        """
        if not await self.ensure_connected():
            return False
        try:
            node = self.client.get_node(node_id)
            await asyncio.wait_for(node.write_value(value), timeout=self.request_timeout)
            self.logger.info(f"OPCUA write ok: {node_id} <- {value!r}")
            return True
        except ua.UaStatusCodeError as e:
            self.logger.error(f"Error writing OPCUA node {node_id}: {e}")
            return False
        except asyncio.TimeoutError:
            self.logger.error(f"Timeout writing OPCUA node {node_id} ({self.request_timeout}s)")
            await self._mark_disconnected_unexpected()
            return False
        except Exception as e:
            self.logger.error(f"Error writing OPCUA node {node_id}: {e}")
            await self._mark_disconnected_unexpected()
            return False

    # ------------------------------------------------------------------
    # 内部：连接/断开原语
    # ------------------------------------------------------------------

    async def _cleanup_client(self):
        """
        安全断开并清理底层 client 连接（带超时，忽略异常）。
        用于所有异常/重连路径，主动释放服务端连接数，避免占用导致服务端资源泄漏。
        清理后置 client=None，防止后续误用半开 session。
        """
        if self.client is None:
            return
        try:
            await asyncio.wait_for(self.client.disconnect(), timeout=self.connect_timeout)
        except Exception as e:
            # disconnect 失败不阻塞流程，服务端最终会因 keepalive 超时回收
            self.logger.warning(f"Client cleanup error (ignored): {e}")
        finally:
            self.client = None

    async def _do_connect(self):
        """实际建立连接（调用方持锁）。失败抛异常。"""
        # 先清理旧 client（如有），释放服务端连接，避免重叠会话占用连接数
        if self.client is not None:
            await self._cleanup_client()
        # 重建 Client 以避免旧 session 状态
        self.client = Client(url=self.url)
        try:
            await asyncio.wait_for(self.client.connect(), timeout=self.connect_timeout)
        except Exception as e:
            self._set_disconnected(was_connected=False)
            # 连接失败也要清理 client 对象，防止半建连 session 残留
            await self._cleanup_client()
            self.logger.error(f"Failed to connect to OPCUA server at {self.url}: {e}")
            raise
        self._set_connected()
        self._reconnect_attempts = 0
        self.logger.info(f"Connected to OPCUA server at {self.url}")

    def _set_connected(self):
        self._connected = True
        self._connected_since = time.time()
        self._disconnected_since = None

    def _set_disconnected(self, was_connected=True):
        if was_connected and self._connected:
            # 之前处于已连接状态，属于非预期掉线
            self._unexpected_disconnects += 1
        self._connected = False
        now = time.time()
        self._last_disconnect_time = now
        self._disconnected_since = now
        self._connected_since = None

    async def _mark_disconnected_unexpected(self):
        """
        读写/健康检查异常时调用（不持锁路径）：
        1. 主动 disconnect 触发异常的 client 实例，释放服务端连接数
        2. 标记为非预期断开
        3. 触发后台重连任务

        使用本地引用 + CAS 检查，避免与并发手动重连新建的 client 冲突：
        只断开本次异常对应的旧 client，不误清新 client，也不误置空被替换的引用。
        """
        if self._connected:
            stale = self.client  # 本地引用，防止并发替换
            self._set_disconnected(was_connected=True)
            if stale is not None:
                try:
                    await asyncio.wait_for(stale.disconnect(), timeout=self.connect_timeout)
                except Exception as e:
                    self.logger.warning(f"Stale client cleanup error (ignored): {e}")
                # CAS：仅当 self.client 仍是这个旧实例时才置空，避免覆盖并发新建的 client
                if self.client is stale:
                    self.client = None
        self._ensure_reconnect_task()

    # ------------------------------------------------------------------
    # 内部：后台重连任务（指数退避，非阻塞）
    # ------------------------------------------------------------------

    def _ensure_reconnect_task(self):
        """确保后台重连任务在运行（如已运行或已停止则不重启）。"""
        if self._stop_event.is_set():
            return
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _cancel_reconnect_task(self):
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        self._reconnect_task = None

    async def _reconnect_loop(self):
        """后台重连循环：指数退避，直到连接成功或达到最大次数或被取消。"""
        self.logger.info("OPC UA background reconnect loop started")
        try:
            while not self._stop_event.is_set():
                if self._connected:
                    # 已连上（可能是手动重连成功），退出本循环
                    return
                self._reconnect_attempts += 1
                self._total_reconnect_attempts += 1
                if self.max_reconnect_attempts and self._reconnect_attempts > self.max_reconnect_attempts:
                    self.logger.error(
                        f"OPC UA reconnect failed after {self.max_reconnect_attempts} attempts, giving up"
                    )
                    return
                # 指数退避：base * factor^(attempts-1)，封顶 max_reconnect_interval
                delay = min(
                    self.reconnect_interval * (self.backoff_factor ** (self._reconnect_attempts - 1)),
                    self.max_reconnect_interval,
                )
                self.logger.info(
                    f"OPC UA reconnect attempt {self._reconnect_attempts} "
                    f"(delay={delay:.1f}s, backoff={self.backoff_factor}, max={self.max_reconnect_interval}s)"
                )
                try:
                    async with self._lock:
                        if self._connected:
                            return
                        await self._do_connect()
                        self._successful_reconnects += 1
                        self._reconnect_attempts = 0
                        self.logger.info(f"OPC UA reconnected to {self.url}")
                        return
                except Exception as e:
                    self.logger.warning(f"OPC UA reconnect failed: {e}, retry in {delay:.1f}s")
                    # 可被停止事件唤醒的等待
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
        except asyncio.CancelledError:
            self.logger.info("OPC UA background reconnect loop cancelled")
            raise
        finally:
            self.logger.info("OPC UA background reconnect loop exited")

    # ------------------------------------------------------------------
    # 内部：后台健康检查
    # ------------------------------------------------------------------

    def _start_health_check(self):
        if self._stop_event.is_set():
            return
        if self._health_check_task is None or self._health_check_task.done():
            self._health_check_task = asyncio.create_task(self._health_check_loop())

    async def _health_check_loop(self):
        """定期检查连接是否健康（读取服务端 ServerStatus 标准节点）。"""
        self.logger.info(f"OPC UA health check loop started (interval={self.health_check_interval}s)")
        try:
            while not self._stop_event.is_set():
                # 间隔等待（可被停止事件唤醒）
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.health_check_interval)
                except asyncio.TimeoutError:
                    pass
                if self._stop_event.is_set():
                    break
                if self._connected:
                    await self._check_health()
        except asyncio.CancelledError:
            self.logger.info("OPC UA health check loop cancelled")
            raise
        finally:
            self.logger.info("OPC UA health check loop exited")

    async def _check_health(self):
        """检查连接健康：读取 Server.ServerStatus（i=2256），失败则标记断开并触发重连。"""
        try:
            node = self.client.get_node(self._SERVER_STATUS_NODE_ID)
            await asyncio.wait_for(node.read_value(), timeout=self.request_timeout)
            self.logger.debug("OPC UA health check ok")
        except Exception as e:
            self.logger.warning(f"OPC UA health check failed: {e}, marking disconnected")
            await self._mark_disconnected_unexpected()
