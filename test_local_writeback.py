"""
纯本地验证：10001(DCS历史) -> 10011(HICS实时) 数据回写逻辑。

完全不依赖平台（无 token、无网络、无 OPC UA）：
  - MockOPCUA        : 返回 start/end 时间分量，接受 FC 写入
  - MockHistoryClient: 返回下方显式构造的 10001 域模拟历史数据
  - MockRTDBClient   : 记录所有 10011 回写请求，始终返回成功

复用 main.py 真实 _handle_task 业务逻辑，验证 6 个王快任务：
  时间拼装 -> 历史拉取 -> 重放排序 -> 批量回写(10011) -> FC 置位 -> completed
"""
import asyncio
import datetime
import logging
import os

os.environ["HEALTH_ENDPOINT_ENABLED"] = "false"

from main import DataHubService
from config import config
import tasks_config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(threadName)s] %(name)s - %(message)s'
)
logger = logging.getLogger("local_wb")

# ========== 测试时间区间（今天 09:00:00 ~ 09:01:00） ==========
NOW = datetime.datetime.now()
START_DT = NOW.replace(hour=9, minute=0, second=0, microsecond=0)
END_DT = START_DT + datetime.timedelta(minutes=1)
START_STR = START_DT.strftime("%Y-%m-%d %H:%M:%S")
END_STR = END_DT.strftime("%Y-%m-%d %H:%M:%S")


# ========== 模拟 DCS 历史数据（10001 域） ==========
# 每个点 5 个采样点，间隔 12 秒，值按等差递增便于追踪。
# key = history_id（含 10001 域前缀），value = [偏移秒, 数值] 列表
def _ts(offset_sec):
    """由 START_DT + 偏移秒生成毫秒时间戳。"""
    return int((START_DT + datetime.timedelta(seconds=offset_sec)).timestamp() * 1000)


MOCK_DCS_HISTORY = {
    # AGC01
    "10001:AMI_JZ1_FHGD.AV": [(0, 300.0), (12, 302.5), (24, 305.0), (36, 307.5), (48, 310.0)],
    "10001:JZ1_AI49.AV":     [(0, 100.0), (12, 101.0), (24, 102.0), (36, 103.0), (48, 104.0)],
    # AGC02
    "10001:AMI_JZ2_FHGD.AV": [(0, 280.0), (12, 282.5), (24, 285.0), (36, 287.5), (48, 290.0)],
    "10001:JZ2_AI49.AV":     [(0, 110.0), (12, 111.0), (24, 112.0), (36, 113.0), (48, 114.0)],
    # AVC01
    "10001:AMI_JZ1_WGGD.AV": [(0, 45.0),  (12, 46.0),  (24, 47.0),  (36, 48.0),  (48, 49.0)],
    "10001:JZ1_AI50.AV":     [(0, 50.0),  (12, 50.5),  (24, 51.0),  (36, 51.5),  (48, 52.0)],
    # AVC02
    "10001:AMI_JZ2_WGGD.AV": [(0, 55.0),  (12, 56.0),  (24, 57.0),  (36, 58.0),  (48, 59.0)],
    "10001:JZ2_AI50.AV":     [(0, 60.0),  (12, 60.5),  (24, 61.0),  (36, 61.5),  (48, 62.0)],
    # PFC01 / PFC02
    "10001:T1CKDLQ_F.AV":    [(0, 5.0),   (12, 5.5),   (24, 6.0),   (36, 6.5),   (48, 7.0)],
    "10001:T2CKDLQ_F.AV":    [(0, 8.0),   (12, 8.5),   (24, 9.0),   (36, 9.5),   (48, 10.0)],
}


class MockOPCUA:
    """模拟 OPC UA：按节点名后缀返回 start/end 时间分量，接受 FC 写入。"""
    connected = True
    stats = {"connected": True}

    def __init__(self):
        self.fc_writes = []

    async def read_values(self, node_ids):
        import re
        out = {}
        for nid in node_ids:
            name = nid.split(";s=", 1)[-1] if ";s=" in nid else nid
            nums = re.findall(r'\d+', name)
            suffix = nums[-1] if nums else None
            use_end = suffix in ("11", "12", "13", "14")
            dt = END_DT if use_end else START_DT
            if name.startswith("YEAR"):
                out[nid] = dt.year
            elif name.startswith("MON"):
                out[nid] = dt.month
            elif name.startswith("DAY"):
                out[nid] = dt.day
            elif name.startswith("HOUR"):
                out[nid] = dt.hour
            elif name.startswith("MIN"):
                out[nid] = dt.minute
            elif name.startswith("SEC"):
                out[nid] = dt.second
            else:
                out[nid] = 0
        return out

    async def write_value(self, node_id, value):
        self.fc_writes.append((node_id, value))
        logger.info(f"[MockOPCUA] FC 置位 {node_id} = {value}")
        return True


class MockHistoryClient:
    """历史客户端：从 MOCK_DCS_HISTORY 返回 10001 域模拟数据。"""
    def __init__(self):
        self.token = "mock-token"
        self.queries = []

    def get_history_data(self, start_time, end_time, node_ids):
        self.queries.append({"start": start_time, "end": end_time, "nodes": list(node_ids)})
        out = []
        for nid in node_ids:
            samples = MOCK_DCS_HISTORY.get(nid, [])
            pts = [{"t": _ts(off), "v": val, "q": 0} for off, val in samples]
            out.append({"nodeId": nid, "data": pts})
        logger.info(f"[MockHistory] 返回 {len(node_ids)} 节点, 每节点 {len(samples)} 采样")
        return out


class MockRTDBClient:
    """RTDB 客户端：记录所有 10011 回写请求，始终返回成功。"""
    def __init__(self):
        self.token = "mock-token"
        self.writes = []  # 每次 batch 调用的 [{nodeId, value}, ...]

    def write_realtime_data(self, items, strict_mode=False):
        self.writes.append(list(items))
        result = {
            "success": True,
            "code": 0,
            "message": "ok(local-mock)",
            "results": [0] * len(items),
            "attempt": 1,
        }
        logger.info(f"[MockRTDB] write 接收 {len(items)} 点 (10011 目标)")
        return result


async def run():
    logger.info("=" * 70)
    logger.info("本地验证：10001(DCS) -> 10011(HICS) 数据回写逻辑")
    logger.info(f"测试时间区间: {START_STR} ~ {END_STR}")
    logger.info("全部本地 mock，无网络/token/OPC UA 依赖")
    logger.info("=" * 70)

    # 构造 service 但不调用 start()（不连 OPC UA、不启 Web UI）
    service = DataHubService()

    # 替换依赖为本地 mock
    mock_opcua = MockOPCUA()
    mock_history = MockHistoryClient()
    mock_rtdb = MockRTDBClient()
    service.opcua = mock_opcua
    service.history = mock_history
    service.rtdb = mock_rtdb

    # 跳过 token 检查与 settle 等待
    async def fake_ensure_token():
        return True
    service._ensure_token = fake_ensure_token

    async def fake_sleep(seconds):
        pass
    service._sleep_or_stop = fake_sleep

    # 只跑 DCS（王快）任务
    dcs_tasks = [t for t in tasks_config.TASKS if t["source"] == "王快"]
    logger.info(f"DCS(王快)任务数: {len(dcs_tasks)}\n")

    results = []
    for orig in dcs_tasks:
        tid = orig["id"]
        logger.info(f"{'=' * 60}\n>>> 任务 {tid}: {orig['desc']}\n{'=' * 60}")
        service._set_task_stage(tid, "idle")
        try:
            success, detail, stage = await service._handle_task(orig)
            results.append((orig, success, detail, stage))
            logger.info(f"<<< 任务 {tid}: success={success} detail={detail} stage={stage}\n")
        except Exception as e:
            logger.exception(f"任务 {tid} 异常: {e}")
            results.append((orig, False, str(e), "exception"))

    # ========== 汇总与回写映射验证 ==========
    logger.info("=" * 70)
    logger.info("10001 -> 10011 回写映射验证")
    logger.info("=" * 70)

    ok = sum(1 for r in results if r[1])
    for orig, success, detail, stage in results:
        tid = orig["id"]
        logger.info(f"[{tid}] {'PASS' if success else 'FAIL'}  stage={stage}  detail={detail}")

    logger.info(f"\n总计: {len(results)} 任务, {ok} 成功")
    logger.info(f"FC 置位次数: {len(mock_opcua.fc_writes)}")
    logger.info(f"RTDB write 调用次数: {len(mock_rtdb.writes)}")

    # 详细映射表：每个任务的 10001 源 -> 10011 目标 -> 回写值序列
    logger.info("\n" + "-" * 70)
    logger.info("详细回写映射（10001 源 -> 10011 目标）")
    logger.info("-" * 70)
    for orig, success, _, _ in results:
        tid = orig["id"]
        logger.info(f"\n[{tid}] {orig['desc']}")
        for p in orig["points"]:
            hid = p["history_id"]
            tid_target = p["target_id"]
            samples = MOCK_DCS_HISTORY.get(hid, [])
            vals = [str(v) for _, v in samples]
            logger.info(f"  {hid:<28} -> {tid_target:<20}  vals=[{', '.join(vals)}]")

    # 校验：RTDB 实际收到的 10011 目标点与配置一致
    logger.info("\n" + "-" * 70)
    logger.info("RTDB 实际收到的回写点校验")
    logger.info("-" * 70)
    expected_targets = set()
    for orig, _, _, _ in results:
        for p in orig["points"]:
            expected_targets.add(p["target_id"])

    actual_targets = set()
    for batch in mock_rtdb.writes:
        for item in batch:
            actual_targets.add(item["nodeId"])

    missing = expected_targets - actual_targets
    extra = actual_targets - expected_targets
    logger.info(f"期望 10011 目标点数: {len(expected_targets)}")
    logger.info(f"实际回写目标点数: {len(actual_targets)}")
    if not missing and not extra:
        logger.info("校验通过: 10001 -> 10011 映射完整一致")
    else:
        if missing:
            logger.warning(f"缺失目标点: {sorted(missing)}")
        if extra:
            logger.warning(f"多余目标点: {sorted(extra)}")

    # 回写值数量校验：每任务 2 源点 × 5 采样 = 10 条
    logger.info("\n" + "-" * 70)
    logger.info("回写条数校验")
    logger.info("-" * 70)
    all_ok = True
    for orig, success, _, _ in results:
        tid = orig["id"]
        expected_count = sum(len(MOCK_DCS_HISTORY.get(p["history_id"], [])) for p in orig["points"])
        actual_count = 0
        for batch in mock_rtdb.writes:
            # 简单按条数累计（每任务一次 batch）
            pass
        logger.info(f"[{tid}] 期望回写 {expected_count} 条, success={success}")
        if not success or expected_count == 0:
            all_ok = False

    total_written = sum(len(b) for b in mock_rtdb.writes)
    expected_total = sum(
        sum(len(MOCK_DCS_HISTORY.get(p["history_id"], [])) for p in orig["points"])
        for orig, _, _, _ in results
    )
    logger.info(f"\n总回写条数: {total_written} (期望 {expected_total})")
    if total_written == expected_total and ok == len(results):
        logger.info(">>> 本地回写逻辑验证通过: 10001 -> 10011 完整跑通 <<<")
    else:
        logger.warning(">>> 本地回写逻辑验证存在问题，请检查上方日志 <<<")


if __name__ == "__main__":
    asyncio.run(run())
