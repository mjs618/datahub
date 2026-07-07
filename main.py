
import asyncio
import signal
import threading
import time
import logging
from opcua_client import OPCUAController
from history_api import HistoryAPIClient
from rt_db_client import RTDBClient
from auth_client import AuthClient
from state_manager import StateManager
from health_server import Metrics, HealthServer
from web_ui import WebUIServer
from config import config
import tasks_config

# 日志格式：包含时间、级别、模块名、线程名，便于排查
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(threadName)s] %(name)s - %(message)s'
)
logger = logging.getLogger("data-hub")


def _assemble_time(comp_values, components):
    """
    将时间分量节点值拼装为 'yyyy-MM-dd HH:mm:ss' 字符串。
    comp_values: {node_id: value} （来自 OPC UA 批量读取）
    components: {'year': node_id, 'mon': node_id, ...} （见 tasks_config._components）
    返回字符串；任一分量缺失或无法转 int 时返回 None。
    """
    order = ("year", "mon", "day", "hour", "min", "sec")
    try:
        nums = []
        for key in order:
            nid = components[key]
            v = comp_values.get(nid)
            if v is None:
                return None
            n = int(v)
            nums.append(n)
    except (ValueError, TypeError) as e:
        logger.warning(f"_assemble_time: invalid component value ({e}), components={components}")
        return None
    y, mo, d, h, mi, s = nums
    # 基本合法性校验（不抛异常，仅拦截明显非法值）
    if not (1970 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31
            and 0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s <= 59):
        logger.warning(f"_assemble_time: out-of-range value {nums}, will still assemble")
    return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"


def _rtdb_node_id(node_id):
    """将 OPC UA NodeId 形式的目标点转换为 RTDB 写值接口使用的测点名。"""
    if not node_id:
        return node_id
    marker = ";s="
    if marker in node_id:
        return node_id.split(marker, 1)[1]
    return node_id


class DataHubService:
    def __init__(self):
        self.auth = AuthClient(
            config.BASE_IP, config.APP_CODE, config.APP_SECRET,
            timeout=config.HTTP_TIMEOUT,
            refresh_margin=config.TOKEN_REFRESH_MARGIN,
            default_ttl=config.TOKEN_DEFAULT_TTL,
        )
        # OPC UA / History / RTDB 在拿到 token 后初始化
        self.opcua = OPCUAController(
            config.OPCUA_URL,
            reconnect_interval=config.OPCUA_RECONNECT_INTERVAL,
            max_reconnect_attempts=config.OPCUA_MAX_RECONNECT_ATTEMPTS,
        )
        self.history = None
        self.rtdb = None

        self.state = StateManager(
            state_file=config.STATE_FILE,
            cache_max_age_hours=config.WRITE_CACHE_MAX_AGE_HOURS,
            cache_max_entries=config.WRITE_CACHE_MAX_ENTRIES,
        )

        self.metrics = Metrics()
        if config.HEALTH_ENDPOINT_ENABLED:
            self.health = HealthServer(
                self.metrics,
                port=config.HEALTH_ENDPOINT_PORT,
                stale_threshold=config.HEALTH_STALE_THRESHOLD,
            )
        else:
            self.health = None

        # Web UI 用于手动触发同步的标志位与最新结果
        self._manual_sync_requested = False
        self._manual_sync_lock = threading.Lock()
        self._last_manual_sync_result = None
        # 最近一次触发信号读取值（供 Web UI 概览页展示），None 表示尚未读到
        self._last_trigger_value = None

        # ========== 多任务模式状态（通讯点(1).xlsx 定义的 12 个任务） ==========
        # 每个任务记录：ac_prev（上次 AC 值）、processing（是否处理中，防重入）、
        #               last_result（最近一次处理结果摘要，供 Web UI 展示）
        self._task_state = {}
        for t in tasks_config.TASKS:
            self._task_state[t["id"]] = {
                "task": t,
                "ac_prev": None,        # None=未知，True/False=上次读到的 AC 值
                "processing": False,    # 防重入标志
                "current_stage": "idle",
                "last_result": None,    # {"status","detail","timestamp"}
            }
        # Web UI 手动触发指定任务的请求队列：{task_id: True}
        self._manual_task_requests = {}
        self._manual_task_lock = threading.Lock()

        # Web UI 服务（端口 8089）
        self.web_ui = WebUIServer(
            opcua_url=config.OPCUA_URL,
            trigger_sync_callback=self._request_manual_sync,
            status_getter=self._get_manual_sync_status,
            metrics_getter=self.metrics.snapshot,
            trigger_state_getter=self._get_trigger_state,
            task_trigger_callback=self.request_task_trigger,
            task_status_getter=self.get_tasks_status,
        )

        self._stop_event = asyncio.Event()
        self._loop = None

    # ---------- 生命周期 ----------

    async def start(self):
        # 注册信号处理（优雅关闭）
        self._loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._loop.add_signal_handler(sig, self._request_stop)
            except NotImplementedError:
                # Windows 不支持 add_signal_handler，使用 signal.signal 兜底
                signal.signal(sig, lambda *_: asyncio.create_task(self._request_stop_async()))

        if self.health:
            self.health.start()
            logger.info(f"Health endpoint listening on :{config.HEALTH_ENDPOINT_PORT}")

        # 启动 Web UI
        self.web_ui.start()
        logger.info(f"Web UI listening on :{self.web_ui.port}")

        # 等待初始 Token
        token = self.auth.get_token()
        if not token:
            logger.error("Failed to obtain valid token at startup, will retry in loop")
            self.metrics.set_token_valid(False)
        else:
            self.metrics.set_token_valid(True)
            self._init_clients(token)

        try:
            await self.opcua.connect()
            self.metrics.set_opcua_connected(self.opcua.connected)
        except Exception as e:
            logger.error(f"Initial OPC UA connect failed: {e}, will retry in loop")
            self.metrics.set_opcua_connected(False)

        # 按运行模式进入对应主循环
        if config.TASK_MODE == "multi":
            logger.info(f"TASK_MODE=multi, entering task loop ({len(tasks_config.TASKS)} tasks)")
            await self._run_task_loop()
        else:
            logger.info("TASK_MODE=single, entering legacy single-trigger loop")
            await self._run_loop()

    def _request_stop(self):
        logger.info("Stop signal received, shutting down...")
        self._stop_event.set()

    async def _request_stop_async(self):
        self._request_stop()

    def _init_clients(self, token):
        """根据当前有效 token 创建/重建 History 和 RTDB 客户端"""
        self.history = HistoryAPIClient(
            config.BASE_IP, token,
            timeout=config.HTTP_TIMEOUT,
            page_size=config.HISTORY_PAGE_SIZE,
            timestamp_unit=config.HISTORY_TIMESTAMP_UNIT,
        )
        self.rtdb = RTDBClient(
            config.BASE_IP, token,
            timeout=config.HTTP_TIMEOUT,
            max_retries=config.HTTP_MAX_RETRIES,
            retry_backoff=config.HTTP_RETRY_BACKOFF,
        )

    # ---------- Web UI 回调 ----------

    def _request_manual_sync(self):
        """Web UI 调用：请求主循环在下一周期执行一次同步"""
        with self._manual_sync_lock:
            already_pending = self._manual_sync_requested
            self._manual_sync_requested = True
        if already_pending:
            logger.info("Manual sync already pending, ignored duplicate request")
        else:
            logger.info("Manual sync requested via Web UI")
        return {"status": "triggered"}

    def _get_manual_sync_status(self):
        """Web UI 调用：查询最近一次手动同步的结果"""
        with self._manual_sync_lock:
            return {
                "pending": self._manual_sync_requested,
                "last_result": self._last_manual_sync_result,
            }

    def _get_trigger_state(self):
        """Web UI 调用：查询当前触发信号状态与 OPC UA 连接状态"""
        return {
            "opcua_connected": bool(self.opcua.connected),
            "trigger_value": self._last_trigger_value,
        }

    async def _ensure_token(self):
        """确保 token 有效，过期则刷新并重建客户端"""
        logger.info("Step 0: Checking token validity...")
        token = self.auth.get_token()
        if not token:
            logger.warning("Step 0: Token is None/invalid, marking token_valid=False and will skip sync")
            self.metrics.set_token_valid(False)
            return False
        self.metrics.set_token_valid(True)
        # 如果客户端未创建或 token 变化，重建
        if self.history is None:
            logger.info("Step 0: Token valid, initializing History/RTDB clients for the first time")
            self._init_clients(token)
        elif self.history.token != token:
            logger.info("Step 0: Token changed, rebuilding History/RTDB clients with new token")
            self._init_clients(token)
        else:
            logger.info("Step 0: Token valid and unchanged, clients reused")
        return True

    # ---------- 主循环 ----------

    async def _run_loop(self):
        logger.info(f"Monitoring Loop Started (Trigger: {config.TRIG_NODE_ID})")
        logger.info(
            f"Loop config: POLL_INTERVAL={config.POLL_INTERVAL}s, "
            f"SETTLE_TIME={config.SETTLE_TIME}s, "
            f"LOOKBACK_MINUTES={config.LOOKBACK_MINUTES}min, "
            f"ENABLE_DEDUP={config.ENABLE_DEDUP}, "
            f"ENABLE_WRITE_CACHE={config.ENABLE_WRITE_CACHE}"
        )
        prev_trigger = None  # None 表示未知，避免启动瞬间误判

        while not self._stop_event.is_set():
            try:
                self.metrics.mark_loop()
                await self._ensure_opcua_connected()

                # 检测 Web UI 手动触发请求
                with self._manual_sync_lock:
                    manual_requested = self._manual_sync_requested
                if manual_requested:
                    logger.info("===== Manual sync triggered via Web UI =====")
                    with self._manual_sync_lock:
                        self._manual_sync_requested = False
                    try:
                        await self._handle_sync_cycle()
                        with self._manual_sync_lock:
                            self._last_manual_sync_result = {
                                "status": "completed",
                                "timestamp": time.time(),
                            }
                    except Exception as e:
                        logger.exception(f"Manual sync failed: {e}")
                        with self._manual_sync_lock:
                            self._last_manual_sync_result = {
                                "status": "failed",
                                "error": str(e),
                                "timestamp": time.time(),
                            }

                current = await self.opcua.read_trigger(config.TRIG_NODE_ID)
                self.metrics.set_opcua_connected(self.opcua.connected)

                # 读取失败时不进行边沿判断
                if current is None:
                    logger.warning(
                        f"read_trigger returned None (read failed), "
                        f"opcua.connected={self.opcua.connected}, skip edge detection this cycle"
                    )
                    # 等待下一周期重连
                    await self._sleep_or_stop(config.POLL_INTERVAL)
                    continue

                # 缓存最近一次触发值，供 Web UI 概览页展示
                self._last_trigger_value = current

                # 首次成功读取时打印一次状态
                if prev_trigger is None:
                    logger.info(f"First successful trigger read: current={current}, prev=None (initial)")

                # 下降沿 1 -> 0
                if prev_trigger is True and current is False:
                    logger.info(
                        "Trigger falling edge detected (1 -> 0). Syncing data..."
                    )
                    self.metrics.mark_trigger()
                    try:
                        await self._handle_sync_cycle()
                    except Exception as e:
                        # 单次同步异常不影响主循环
                        logger.exception(f"Sync cycle failed with exception: {e}")

                elif prev_trigger is False and current is True:
                    logger.info("Trigger rising edge detected (0 -> 1). Session started...")

                elif prev_trigger is None:
                    # 已在上方打印，跳过
                    pass
                else:
                    # 状态保持不变（无变化）
                    logger.debug(f"Trigger unchanged: prev={prev_trigger}, current={current}")

                prev_trigger = current
            except Exception as e:
                logger.exception(f"Main loop iteration error: {e}")

            await self._sleep_or_stop(config.POLL_INTERVAL)

        logger.info("Main loop exited (stop event set)")

    async def _ensure_opcua_connected(self):
        if not self.opcua.connected:
            logger.info("OPC UA not connected, attempting ensure_connected()...")
            try:
                await self.opcua.ensure_connected()
                logger.info(f"OPC UA ensure_connected returned, connected={self.opcua.connected}")
            except Exception as e:
                logger.warning(f"OPC UA reconnect attempt failed: {e}")
        self.metrics.set_opcua_connected(self.opcua.connected)

    async def _sleep_or_stop(self, seconds):
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ========== 多任务模式（12 任务调度） ==========

    async def _run_task_loop(self):
        """多任务主循环：轮询所有任务的 AC 触发点，检测上升沿并派发处理。"""
        logger.info(
            f"Task Loop Started (tasks={len(tasks_config.TASKS)}, "
            f"POLL_INTERVAL={config.POLL_INTERVAL}s, SETTLE_TIME={config.SETTLE_TIME}s, "
            f"WRITE_BACK_VIA={config.WRITE_BACK_VIA})"
        )
        ac_nodes = tasks_config.all_ac_nodes()

        while not self._stop_event.is_set():
            try:
                self.metrics.mark_loop()
                await self._ensure_opcua_connected()

                # 处理 Web UI 手动触发的任务
                await self._drain_manual_task_requests()

                # 批量读取所有 AC 当前值
                current_values = await self.opcua.read_values(ac_nodes)
                self.metrics.set_opcua_connected(self.opcua.connected)

                for t in tasks_config.TASKS:
                    tid = t["id"]
                    st = self._task_state[tid]
                    ac_node = t["ac_node"]
                    raw = current_values.get(ac_node)
                    # None 表示读取失败，跳过边沿判断
                    if raw is None:
                        continue
                    current = bool(raw)

                    prev = st["ac_prev"]
                    # 首次成功读取只记录，不触发
                    if prev is None:
                        st["ac_prev"] = current
                        logger.info(f"Task {tid}: first AC read = {current}")
                        continue

                    # 上升沿 0 -> 1（即"状态变成1"）
                    if prev is False and current is True:
                        logger.info(f"===== Task {tid} triggered (AC rising edge 0->1) =====")
                        self.metrics.mark_trigger()
                        await self._dispatch_task(tid)

                    st["ac_prev"] = current
            except Exception as e:
                logger.exception(f"Task loop iteration error: {e}")

            await self._sleep_or_stop(config.POLL_INTERVAL)

        logger.info("Task loop exited (stop event set)")

    async def _drain_manual_task_requests(self):
        """处理 Web UI 手动触发指定任务的请求（如有）。"""
        with self._manual_task_lock:
            pending = dict(self._manual_task_requests)
            self._manual_task_requests.clear()
        for tid in pending:
            logger.info(f"===== Task {tid} manually triggered via Web UI =====")
            self.metrics.mark_trigger()
            await self._dispatch_task(tid)

    async def _dispatch_task(self, task_id):
        """派发单个任务：防重入 + 异常隔离。"""
        st = self._task_state.get(task_id)
        if st is None:
            logger.warning(f"Task {task_id}: unknown task id, ignored")
            return
        if st["processing"]:
            logger.warning(f"Task {task_id}: already processing, skip this trigger")
            return
        st["processing"] = True
        self._set_task_stage(task_id, "queued")
        try:
            success, detail, stage = await self._handle_task(st["task"])
            st["last_result"] = {
                "status": "completed" if success else "failed",
                "detail": detail,
                "stage": stage,
                "timestamp": time.time(),
            }
        except Exception as e:
            logger.exception(f"Task {task_id} failed with exception: {e}")
            st["last_result"] = {
                "status": "failed",
                "detail": str(e),
                "stage": st.get("current_stage", "exception"),
                "timestamp": time.time(),
            }
        finally:
            st["processing"] = False
            self._set_task_stage(task_id, "idle")
            logger.info(f"===== Task {task_id} cycle finished =====")

    def _set_task_stage(self, task_id, stage):
        st = self._task_state.get(task_id)
        if st is not None:
            st["current_stage"] = stage

    async def _handle_task(self, task):
        """
        单任务处理流程：
          1. 确保 token 有效
          2. 沉淀等待历史落库
          3. 读 12 个时间分量，拼成开始/结束时间
          4. 调历史 API 拉数据
          5. 将区间内所有历史值按时间顺序通过 RTDB 接口回写到目标点
          6. 成功后置 FC=1
        """
        tid = task["id"]
        logger.info(f"Task {tid}: ----- sync started -----")
        self._set_task_stage(tid, "token")

        # 0. token
        if not await self._ensure_token():
            logger.warning(f"Task {tid}: no valid token, abort")
            self.metrics.mark_sync(False, "no valid token")
            return False, "no valid token", "token"

        # 1. 沉淀
        self._set_task_stage(tid, "settle")
        logger.info(f"Task {tid}: settling {config.SETTLE_TIME}s for history ingestion...")
        await self._sleep_or_stop(config.SETTLE_TIME)
        if self._stop_event.is_set():
            logger.info(f"Task {tid}: stop during settle, abort")
            return False, "stopped during settle", "settle"

        # 2. 读时间分量
        self._set_task_stage(tid, "read_time")
        start_components = task["start_components"]
        end_components = task["end_components"]
        all_comp_nodes = list(start_components.values()) + list(end_components.values())
        comp_values = await self.opcua.read_values(all_comp_nodes)
        self.metrics.set_opcua_connected(self.opcua.connected)

        start_str = _assemble_time(comp_values, start_components)
        end_str = _assemble_time(comp_values, end_components)
        if not start_str or not end_str:
            logger.error(
                f"Task {tid}: failed to assemble time from components "
                f"(start_str={start_str!r}, end_str={end_str!r}, raw={comp_values}), abort"
            )
            self.metrics.mark_sync(False, "bad time components")
            return False, "bad time components", "read_time"
        logger.info(f"Task {tid}: time range start={start_str} end={end_str}")

        # 3. 拉历史数据
        self._set_task_stage(tid, "history")
        history_ids = [p["history_id"] for p in task["points"]]
        logger.info(f"Task {tid}: fetching history for {history_ids}")
        history_data = self.history.get_history_data(start_str, end_str, history_ids)
        if not history_data:
            logger.warning(f"Task {tid}: no history data returned, abort")
            self.metrics.mark_sync(False, "empty history")
            return False, "empty history", "history"

        # 4. 展开每个历史点在区间内的所有有效值，按时间排序后用于回放。
        self._set_task_stage(tid, "build_replay")
        replay_items = []
        point_by_history_id = {p["history_id"]: p for p in task["points"]}
        point_order = {p["history_id"]: idx for idx, p in enumerate(task["points"])}
        for node_entry in history_data:
            nid = node_entry.get("nodeId")
            point_cfg = point_by_history_id.get(nid)
            if not point_cfg:
                logger.warning(f"Task {tid}: unexpected history node {nid}, skipped")
                continue
            points = node_entry.get("data", []) or []
            if not points:
                continue
            target_id = point_cfg.get("target_id") or _rtdb_node_id(point_cfg.get("target_node"))
            for sample in points:
                val = sample.get("v")
                if val is None:
                    continue
                replay_items.append({
                    "t": sample.get("t", 0),
                    "order": point_order.get(nid, 0),
                    "history_id": nid,
                    "nodeId": target_id,
                    "value": val,
                })

        replay_items.sort(key=lambda item: (item["t"], item["order"]))
        write_payload = [
            {"nodeId": item["nodeId"], "value": item["value"]}
            for item in replay_items
        ]
        for item in replay_items:
            logger.info(
                f"Task {tid}: replay {item['history_id']} -> {item['nodeId']} "
                f"= {item['value']!r} (t={item['t']})"
            )

        for p in task["points"]:
            hid = p["history_id"]
            if not any(item["history_id"] == hid for item in replay_items):
                target_id = p.get("target_id") or _rtdb_node_id(p.get("target_node"))
                logger.warning(f"Task {tid}: source {hid} has no valid values, skip {target_id}")
                continue

        if not write_payload:
            logger.warning(f"Task {tid}: no valid points to write, abort")
            self.metrics.mark_sync(False, "no valid points")
            return False, "no valid points", "build_replay"

        # 5. 通过实时库接口批量回写区间内所有值
        self._set_task_stage(tid, "rtdb_replay")
        batch_size = max(1, int(config.RTDB_REPLAY_BATCH_SIZE))
        batches = [
            write_payload[i:i + batch_size]
            for i in range(0, len(write_payload), batch_size)
        ]
        write_ok = True
        failed_message = ""
        for idx, batch in enumerate(batches, start=1):
            result = self.rtdb.write_realtime_data(batch)
            batch_ok = bool(result.get("success"))
            logger.info(
                f"Task {tid}: RTDB replay batch {idx}/{len(batches)} "
                f"{'SUCCESS' if batch_ok else 'FAILED'} "
                f"(count={len(batch)}, code={result.get('code')}, "
                f"message={result.get('message')})"
            )
            if not batch_ok:
                write_ok = False
                failed_message = result.get("message") or "write-back failed"
                break

        if not write_ok:
            self.metrics.mark_sync(False, failed_message or "write-back failed")
            logger.error(f"Task {tid}: write-back failed, FC will NOT be set")
            return False, failed_message or "write-back failed", "rtdb_replay"

        # 6. 置 FC=1（仅在回写全部成功后）
        self._set_task_stage(tid, "fc_feedback")
        fc_node = task["fc_node"]
        fc_ok = await self.opcua.write_value(fc_node, True)
        if fc_ok:
            logger.info(f"Task {tid}: FC=1 set ({fc_node})")
            self.metrics.mark_sync(True, "ok")
        else:
            logger.error(f"Task {tid}: write-back ok but FC=1 FAILED ({fc_node})")
            self.metrics.mark_sync(False, "FC write failed")
            return False, "FC write failed", "fc_feedback"

        self._set_task_stage(tid, "completed")
        logger.info(f"Task {tid}: ----- sync completed SUCCESS -----")
        return True, "ok", "completed"

    # ---------- Web UI 任务回调 ----------

    def request_task_trigger(self, task_id):
        """Web UI 调用：请求主循环处理指定任务。"""
        if task_id not in self._task_state:
            return {"status": "error", "error": f"unknown task id: {task_id}"}
        with self._manual_task_lock:
            already = self._manual_task_requests.get(task_id, False)
            self._manual_task_requests[task_id] = True
        if already:
            logger.info(f"Task {task_id}: manual trigger already pending, ignored duplicate")
        else:
            logger.info(f"Task {task_id}: manual trigger requested via Web UI")
        return {"status": "triggered", "task_id": task_id}

    def get_tasks_status(self):
        """Web UI 调用：返回所有任务的状态摘要。"""
        out = []
        for t in tasks_config.TASKS:
            tid = t["id"]
            st = self._task_state.get(tid, {})
            out.append({
                "id": tid,
                "module": t["module"],
                "source": t["source"],
                "desc": t["desc"],
                "ac_node": t["ac_node"],
                "fc_node": t["fc_node"],
                "processing": st.get("processing", False),
                "current_stage": st.get("current_stage", "idle"),
                "ac_prev": st.get("ac_prev"),
                "last_result": st.get("last_result"),
            })
        return {"tasks": out}

    # ---------- 同步流程 ----------

    async def _handle_sync_cycle(self):
        logger.info("===== Sync cycle started =====")
        # 0. 确保 token 有效
        if not await self._ensure_token():
            logger.warning("No valid token, skip this sync cycle")
            self.metrics.mark_sync(False, "no valid token")
            return

        # 1. 重试之前的失败写入（先消费缓存，避免堆积）
        if config.ENABLE_WRITE_CACHE and config.RETRY_FAILED_CACHE_ON_CYCLE:
            logger.info("Step 1: Retrying cached failed writes (if any)...")
            await self._retry_failed_writes()
        else:
            logger.info(
                f"Step 1: Skipped failed-write retry "
                f"(ENABLE_WRITE_CACHE={config.ENABLE_WRITE_CACHE}, "
                f"RETRY_FAILED_CACHE_ON_CYCLE={config.RETRY_FAILED_CACHE_ON_CYCLE})"
            )

        # 2. 沉淀时间，等历史数据落库
        logger.info(f"Step 2: Settling {config.SETTLE_TIME}s to wait for history ingestion...")
        await self._sleep_or_stop(config.SETTLE_TIME)
        if self._stop_event.is_set():
            logger.info("Step 2: Stop event detected during settle, abort sync cycle")
            return
        logger.info("Step 2: Settle complete")

        # 3. 查询触发脉冲时间区间
        logger.info(
            f"Step 3: Querying pulse interval for "
            f"TRIG_HISTORY_ID={config.TRIG_HISTORY_ID}, "
            f"LOOKBACK_MINUTES={config.LOOKBACK_MINUTES}"
        )
        st, et = self.history.get_pulse_times(
            config.TRIG_HISTORY_ID, config.LOOKBACK_MINUTES
        )
        if not st or not et:
            logger.warning(
                "Step 3: Could not find trigger interval in history "
                "(no active pulse points with v=1 in lookback window)"
            )
            self.metrics.mark_sync(False, "no pulse interval")
            return
        logger.info(f"Step 3: Pulse interval resolved: startTime={st}, endTime={et}")

        # 4. 去重：同一脉冲只同步一次
        if config.ENABLE_DEDUP and self.state.is_pulse_synced(et):
            last_synced = self.state.get_last_synced_et()
            logger.info(
                f"Step 4: Pulse {et} already synced (last_synced_et={last_synced}), skip"
            )
            return
        logger.info(f"Step 4: Pulse {et} not yet synced, proceeding")

        # 5. 拉取 WATCH_LIST 的历史数据
        watch_list = config.WATCH_LIST
        if not watch_list:
            logger.warning("Step 5: WATCH_LIST is empty, nothing to sync")
            self.metrics.mark_sync(False, "empty watch list")
            return
        logger.info(f"Step 5: Fetching history for {len(watch_list)} nodes: {watch_list}")

        history_data = self.history.get_history_data(st, et, watch_list)
        if not history_data:
            logger.warning(
                f"Step 5: No history data returned for nodes in range {st} -> {et}"
            )
            self.metrics.mark_sync(False, "empty history")
            return
        logger.info(
            f"Step 5: History data fetched, "
            f"node-entries={len(history_data)}, "
            f"nodeIds={[e.get('nodeId') for e in history_data]}"
        )

        # 6. 组装写入负载：按时间排序后取最后一条，过滤空值
        logger.info("Step 6: Building write payload (sort by t, take last, filter None)...")
        write_payload = []
        mapping = config.NODE_MAPPING
        skipped_none = 0
        skipped_empty = 0
        for node_entry in history_data:
            source_id = node_entry.get("nodeId")
            target_id = mapping.get(source_id, source_id)
            points = node_entry.get("data", []) or []
            if not points:
                logger.info(f"Step 6: Node {source_id} has no data points, skipped")
                skipped_empty += 1
                continue
            # 防御性排序
            points_sorted = sorted(points, key=lambda p: p.get("t", 0))
            last_point = points_sorted[-1]
            last_val = last_point.get("v")
            if last_val is None:
                logger.warning(
                    f"Step 6: Node {source_id} last value is None "
                    f"(last_point t={last_point.get('t')}), skipped"
                )
                skipped_none += 1
                continue
            mapped_msg = "" if target_id == source_id else f" (mapped -> {target_id})"
            logger.info(
                f"Step 6: Node {source_id}{mapped_msg}: "
                f"{len(points_sorted)} points, last value={last_val!r} (t={last_point.get('t')})"
            )
            write_payload.append({"nodeId": target_id, "value": last_val})

        logger.info(
            f"Step 6: Payload built: "
            f"valid={len(write_payload)}, "
            f"skipped_empty={skipped_empty}, "
            f"skipped_none={skipped_none}"
        )

        if not write_payload:
            logger.warning("Step 6: No valid points to write after filtering, abort sync")
            self.metrics.mark_sync(False, "no valid points")
            return

        # 7. 写入实时库
        logger.info(f"Step 7: Writing {len(write_payload)} points to RTDB...")
        result = self.rtdb.write_realtime_data(write_payload)
        if result.get("success"):
            logger.info(
                f"Step 7: RTDB write SUCCESS "
                f"(code={result.get('code')}, attempts={result.get('attempt')}, "
                f"pulse_et={et})"
            )
            self.state.set_last_synced_et(et)
            self.metrics.mark_sync(True, "ok")
            logger.info(f"===== Sync cycle completed SUCCESS for pulse {et} =====")
        else:
            logger.error(
                f"Step 7: RTDB write FAILED "
                f"(code={result.get('code')}, message={result.get('message')}, "
                f"attempts={result.get('attempt')}, pulse_et={et})"
            )
            if config.ENABLE_WRITE_CACHE:
                logger.info(f"Step 7: Caching {len(write_payload)} failed writes for retry")
                self.state.add_failed_write(write_payload, pulse_et=et)
            self.metrics.mark_sync(False, result.get("message", "write failed"))
            # 失败时也标记 token 无效以便下次刷新
            logger.info("Step 7: Invalidating token so next cycle will refresh")
            self.auth.invalidate()
            logger.info(f"===== Sync cycle FAILED for pulse {et} =====")

        # 更新失败缓存大小指标
        if config.ENABLE_WRITE_CACHE:
            cache_size = self.state.count_failed_writes()
            self.metrics.set_failed_cache_size(cache_size)
            logger.info(f"Step 7: Failed-write cache size now = {cache_size}")

    async def _retry_failed_writes(self):
        """重试之前缓存的失败写入"""
        pending = self.state.pop_failed_writes()
        if not pending:
            logger.info("Step 1: No cached failed writes to retry")
            return

        logger.info(f"Step 1: Retrying {len(pending)} cached failed writes...")
        payload = [
            {"nodeId": e.get("nodeId"), "value": e.get("value")}
            for e in pending
            if e.get("nodeId") is not None
        ]
        if not payload:
            logger.warning("Step 1: All cached entries had None nodeId, clearing cache")
            self.state.set_failed_writes([])
            return

        result = self.rtdb.write_realtime_data(payload)
        if result.get("success"):
            logger.info(
                f"Step 1: Cached failed writes successfully flushed "
                f"(count={len(payload)}, attempts={result.get('attempt')})"
            )
            self.state.set_failed_writes([])
        else:
            # 保留原缓存（更新 ts 以延长存活）
            self.state.set_failed_writes(pending)
            logger.warning(
                f"Step 1: Retry of cached writes failed "
                f"(code={result.get('code')}, message={result.get('message')}), "
                f"kept {len(pending)} entries in cache"
            )

    # ---------- 关闭 ----------

    async def stop(self):
        logger.info("Stopping DataHubService...")
        self._request_stop()
        try:
            await self.opcua.disconnect()
            logger.info("OPC UA disconnected cleanly")
        except Exception as e:
            logger.warning(f"OPC UA disconnect error: {e}")
        if self.health:
            self.health.stop()
            logger.info("Health endpoint stopped")
        self.web_ui.stop()
        logger.info("Web UI stopped")
        logger.info("Data Hub Service stopped")


async def main():
    service = DataHubService()
    try:
        await service.start()
    except Exception as e:
        logger.exception(f"Main loop fatal error: {e}")
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())
