import asyncio
import logging
from asyncua import Client, ua


class OPCUAController:
    """
    OPC UA 客户端控制器，支持自动重连。
    read_trigger 在读取失败时返回 None（而不是 False），
    以避免被上层误判为触发下降沿。
    """

    def __init__(self, url,
                 reconnect_interval=5.0,
                 max_reconnect_attempts=0):
        self.url = url
        self.reconnect_interval = reconnect_interval
        # 0 表示无限重连
        self.max_reconnect_attempts = max_reconnect_attempts
        self.client = Client(url=self.url)
        self.logger = logging.getLogger(__name__)
        self._connected = False
        self._reconnect_attempts = 0

    @property
    def connected(self):
        return self._connected

    async def connect(self):
        """首次连接"""
        try:
            await self.client.connect()
            self._connected = True
            self._reconnect_attempts = 0
            self.logger.info(f"Connected to OPCUA server at {self.url}")
        except Exception as e:
            self._connected = False
            self.logger.error(f"Failed to connect to OPCUA server: {e}")
            raise

    async def ensure_connected(self):
        """
        确保连接可用，断开时尝试重连。
        返回 True 表示当前已连接，False 表示无法连接。
        """
        if self._connected:
            return True
        return await self._reconnect()

    async def _reconnect(self):
        """按配置的重试策略重连"""
        while True:
            self._reconnect_attempts += 1
            if self.max_reconnect_attempts and self._reconnect_attempts > self.max_reconnect_attempts:
                self.logger.error(
                    f"OPCUA reconnect failed after {self.max_reconnect_attempts} attempts, giving up"
                )
                return False
            try:
                self.logger.info(
                    f"OPCUA reconnect attempt {self._reconnect_attempts}..."
                )
                # 重建 Client 以避免旧 session 状态
                self.client = Client(url=self.url)
                await self.client.connect()
                self._connected = True
                self._reconnect_attempts = 0
                self.logger.info(f"OPCUA reconnected to {self.url}")
                return True
            except Exception as e:
                self._connected = False
                self.logger.warning(f"OPCUA reconnect failed: {e}, retry in {self.reconnect_interval}s")
                await asyncio.sleep(self.reconnect_interval)

    async def disconnect(self):
        try:
            if self._connected:
                await self.client.disconnect()
        except Exception as e:
            self.logger.warning(f"Disconnect error (ignored): {e}")
        finally:
            self._connected = False
            self.logger.info("Disconnected from OPCUA server")

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
            value = await node.read_value()
            return value
        except ua.UaStatusCodeError as e:
            self.logger.error(f"Error reading OPCUA node {node_id}: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Error reading OPCUA node {node_id}: {e}")
            self._connected = False
            return None

    async def read_values(self, node_ids):
        """
        批量读取多个节点。
        返回 dict: {node_id: value}。
        读取失败的节点 value 为 None（不抛异常，不中断其余节点）。
        空列表返回空 dict。
        内部未使用 OPC UA 的批量 Read 服务，而是顺序读取以保证兼容性
        与稳定的失败隔离；12 个节点量级下开销可忽略。
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
            await node.write_value(value)
            self.logger.info(f"OPCUA write ok: {node_id} <- {value!r}")
            return True
        except ua.UaStatusCodeError as e:
            self.logger.error(f"Error writing OPCUA node {node_id}: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Error writing OPCUA node {node_id}: {e}")
            self._connected = False
            return False
