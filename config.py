
import os
import json
import logging


# 运行时配置文件路径（由 Web UI 写入，主程序读取）
# 优先级：runtime 文件 > 环境变量 > 默认值
RUNTIME_CONFIG_FILE = os.getenv("RUNTIME_CONFIG_FILE", "config_runtime.json")


def _load_runtime_config():
    """加载运行时配置文件（如果存在）"""
    try:
        with open(RUNTIME_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.warning(f"Failed to load runtime config: {e}")
    return {}


# 启动时加载一次（Web UI 修改后会调用 Config.reload_runtime() 重新加载）
_RUNTIME_CACHE = _load_runtime_config()


class Config:
    # API & Connection Settings
    BASE_IP = os.getenv("BASE_IP", "http://192.168.1.35:6543")
    OPCUA_URL = os.getenv("OPCUA_URL", "opc.tcp://192.168.1.35:6810")

    # Auth Settings
    APP_CODE = os.getenv("APP_CODE", "data")
    APP_SECRET = os.getenv("APP_SECRET", "123456")
    # Token 提前刷新阈值（秒）：到期前 N 秒即视为过期
    TOKEN_REFRESH_MARGIN = int(os.getenv("TOKEN_REFRESH_MARGIN", "60"))
    # Token 默认有效期（秒），当服务端未返回过期时间时使用
    TOKEN_DEFAULT_TTL = int(os.getenv("TOKEN_DEFAULT_TTL", "7200"))

    # Trigger Logic Settings - 这些由 Web UI 可修改，使用 property 从 runtime 读取
    DEFAULT_TRIG_NODE_ID = os.getenv("TRIG_NODE_ID", "ns=2;s=Trigger")
    DEFAULT_TRIG_HISTORY_ID = os.getenv("TRIG_HISTORY_ID", "10001:ICSSYS.Trigger")

    @property
    def TRIG_NODE_ID(self):
        # runtime 文件优先，其次环境变量
        v = _RUNTIME_CACHE.get("TRIG_NODE_ID")
        return v if v is not None else self.DEFAULT_TRIG_NODE_ID

    @property
    def TRIG_HISTORY_ID(self):
        v = _RUNTIME_CACHE.get("TRIG_HISTORY_ID")
        return v if v is not None else self.DEFAULT_TRIG_HISTORY_ID

    def reload_runtime(self):
        """重新加载 runtime 配置文件（Web UI 修改后调用）"""
        global _RUNTIME_CACHE
        _RUNTIME_CACHE = _load_runtime_config()
        logging.info("Runtime config reloaded")
        # 重置 WATCH_LIST / NODE_MAPPING 缓存，使新值生效
        self._watch_list_env_signature = None
        self._node_mapping_env_signature = None

    # Monitoring Settings - 这些可由 Web UI 修改，runtime 文件优先
    DEFAULT_POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "1"))
    DEFAULT_LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "10"))
    # 触发下降沿后的沉淀时间（秒），等待历史数据落库
    DEFAULT_SETTLE_TIME = float(os.getenv("SETTLE_TIME", "2"))

    @property
    def POLL_INTERVAL(self):
        v = _RUNTIME_CACHE.get("POLL_INTERVAL")
        return v if v is not None else self.DEFAULT_POLL_INTERVAL

    @property
    def LOOKBACK_MINUTES(self):
        v = _RUNTIME_CACHE.get("LOOKBACK_MINUTES")
        return v if v is not None else self.DEFAULT_LOOKBACK_MINUTES

    @property
    def SETTLE_TIME(self):
        v = _RUNTIME_CACHE.get("SETTLE_TIME")
        return v if v is not None else self.DEFAULT_SETTLE_TIME

    # HTTP 请求设置
    HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "10"))
    HTTP_MAX_RETRIES = int(os.getenv("HTTP_MAX_RETRIES", "3"))
    HTTP_RETRY_BACKOFF = float(os.getenv("HTTP_RETRY_BACKOFF", "1.5"))

    # OPC UA 重连设置
    OPCUA_RECONNECT_INTERVAL = float(os.getenv("OPCUA_RECONNECT_INTERVAL", "5"))
    OPCUA_MAX_RECONNECT_ATTEMPTS = int(os.getenv("OPCUA_MAX_RECONNECT_ATTEMPTS", "0"))  # 0 = 无限

    # 历史数据分页大小（0 = 不分页）
    HISTORY_PAGE_SIZE = int(os.getenv("HISTORY_PAGE_SIZE", "0"))

    # RTDB 写入失败缓存与重试
    ENABLE_WRITE_CACHE = os.getenv("ENABLE_WRITE_CACHE", "true").lower() == "true"
    WRITE_CACHE_FILE = os.getenv("WRITE_CACHE_FILE", "/tmp/data_hub_write_cache.json")
    WRITE_CACHE_MAX_AGE_HOURS = int(os.getenv("WRITE_CACHE_MAX_AGE_HOURS", "24"))
    WRITE_CACHE_MAX_ENTRIES = int(os.getenv("WRITE_CACHE_MAX_ENTRIES", "1000"))
    RETRY_FAILED_CACHE_ON_CYCLE = os.getenv("RETRY_FAILED_CACHE_ON_CYCLE", "true").lower() == "true"

    # 状态持久化（去重 + 重启恢复）
    STATE_FILE = os.getenv("STATE_FILE", "/tmp/data_hub_state.json")
    ENABLE_DEDUP = os.getenv("ENABLE_DEDUP", "true").lower() == "true"

    # 健康端点
    HEALTH_ENDPOINT_ENABLED = os.getenv("HEALTH_ENDPOINT_ENABLED", "true").lower() == "true"
    HEALTH_ENDPOINT_PORT = int(os.getenv("HEALTH_ENDPOINT_PORT", "8088"))
    HEALTH_STALE_THRESHOLD = int(os.getenv("HEALTH_STALE_THRESHOLD", "60"))  # 主循环超过 N 秒无活动视为不健康

    # 历史时间戳单位：'ms'（毫秒，默认）或 's'（秒）
    HISTORY_TIMESTAMP_UNIT = os.getenv("HISTORY_TIMESTAMP_UNIT", "ms")

    # Nodes to Watch (JSON string in env)
    DEFAULT_WATCH_LIST = [
        "10001:ICSSYS0001.AVGV",
        "10002:ICSSYS0001.AVGV"
    ]

    # 缓存解析后的 WATCH_LIST，避免每次访问都解析 JSON
    _watch_list_cache = None
    _watch_list_env_signature = None

    @property
    def WATCH_LIST(self):
        # 优先 runtime 文件
        v = _RUNTIME_CACHE.get("WATCH_LIST")
        if isinstance(v, list):
            return v
        env_list = os.getenv("WATCH_LIST")
        # 仅在环境变量变化时重新解析
        if env_list != self._watch_list_env_signature:
            self._watch_list_env_signature = env_list
            self._watch_list_cache = None
        if self._watch_list_cache is not None:
            return self._watch_list_cache
        if env_list:
            try:
                self._watch_list_cache = json.loads(env_list)
                return self._watch_list_cache
            except Exception as e:
                logging.error(f"Failed to parse WATCH_LIST from environment: {e}")
        self._watch_list_cache = self.DEFAULT_WATCH_LIST
        return self._watch_list_cache

    # Node Mappings (JSON dict in env: {"history_id": "rtdb_id"})
    DEFAULT_NODE_MAPPING = {}

    _node_mapping_cache = None
    _node_mapping_env_signature = None

    @property
    def NODE_MAPPING(self):
        # 优先 runtime 文件
        v = _RUNTIME_CACHE.get("NODE_MAPPING")
        if isinstance(v, dict):
            return v
        env_map = os.getenv("NODE_MAPPING")
        if env_map != self._node_mapping_env_signature:
            self._node_mapping_env_signature = env_map
            self._node_mapping_cache = None
        if self._node_mapping_cache is not None:
            return self._node_mapping_cache
        if env_map:
            try:
                self._node_mapping_cache = json.loads(env_map)
                return self._node_mapping_cache
            except Exception as e:
                logging.error(f"Failed to parse NODE_MAPPING from environment: {e}")
        self._node_mapping_cache = self.DEFAULT_NODE_MAPPING
        return self._node_mapping_cache


config = Config()


def save_runtime_config(updates: dict):
    """
    将配置更新合并写入 runtime 文件，并重载内存缓存。
    Web UI 调用此函数保存配置。
    """
    global _RUNTIME_CACHE
    # 先读取现有内容
    current = _load_runtime_config()
    current.update(updates)
    # 原子写入
    tmp = f"{RUNTIME_CONFIG_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    os.replace(tmp, RUNTIME_CONFIG_FILE)
    # 重载缓存
    _RUNTIME_CACHE = current
    config.reload_runtime()
    logging.info(f"Runtime config saved: {list(updates.keys())}")
    return current


def get_runtime_config_snapshot():
    """返回当前生效的完整配置快照（供 Web UI 展示）"""
    return {
        "TRIG_NODE_ID": config.TRIG_NODE_ID,
        "TRIG_HISTORY_ID": config.TRIG_HISTORY_ID,
        "WATCH_LIST": config.WATCH_LIST,
        "NODE_MAPPING": config.NODE_MAPPING,
        "BASE_IP": config.BASE_IP,
        "OPCUA_URL": config.OPCUA_URL,
        "POLL_INTERVAL": config.POLL_INTERVAL,
        "LOOKBACK_MINUTES": config.LOOKBACK_MINUTES,
        "SETTLE_TIME": config.SETTLE_TIME,
    }
