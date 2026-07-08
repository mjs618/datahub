"""
本地 Mock 流程测试：复用 main.py 真实 _handle_task 业务逻辑，跑通 12 任务全流程。

Mock 部分：
  - OPC UA：read_values 返回时间分量；write_value 接受 FC 置位
  - History API：get_history_data 返回按时间区间造的采样数据
真实部分：
  - Token 认证（真实 /api/gateway/appSignIn）
  - RTDB 写入（真实 /api/hsm-db-rtserver/v1/rtdata/node/write）
    回写目标使用历史源点（10001 域，write 返回 success），因为设计器目标点(AGC17 等)在实时库中不可写

验证 _handle_task 的 6 个阶段：
  token -> settle -> read_time -> history -> build_replay -> rtdb_replay -> fc_feedback -> completed
"""
import asyncio
import datetime
import logging
import os

# 禁用 health 端点，避免端口占用
os.environ["HEALTH_ENDPOINT_ENABLED"] = "false"

from main import DataHubService
from config import config
import tasks_config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(threadName)s] %(name)s - %(message)s'
)
logger = logging.getLogger("mock_test")

# 回写目标点：用源点（10001 域，write 返回 data=[0] 成功）
# 设计器目标点(AGC17等)write 返回 2150825984 失败，故用源点代替以验证回写环节
RTDB_WRITE_TARGET = "10001:AMI_JZ1_FHGD.AV"

# 测试时间区间（今天的一个区间）
NOW = datetime.datetime.now()
START_DT = NOW - datetime.timedelta(minutes=2)
END_DT = NOW - datetime.timedelta(minutes=1)
START_STR = START_DT.strftime("%Y-%m-%d %H:%M:%S")
END_STR = END_DT.strftime("%Y-%m-%d %H:%M:%S")


class MockOPCUA:
    """模拟 OPC UA 控制器：按节点名后缀返回 start/end 时间分量，接受 FC 写入。"""
    connected = True
    stats = {"connected": True}

    def __init__(self):
        self.fc_writes = []  # 记录 FC 写入

    async def read_values(self, node_ids):
        import re
        out = {}
        for nid in node_ids:
            # 节点名形如 ns=10011;s=YEAR_AGC01.AV
            name = nid.split(";s=", 1)[-1] if ";s=" in nid else nid
            # 用正则提取节点名中的数字串，判断 start(01-04)/end(11-14)
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
        logger.info(f"[MockOPCUA] FC write {node_id} = {value}")
        return True


class MockHistoryClient:
    """模拟历史 API：返回按时间区间造的采样数据。"""
    def __init__(self):
        self.token = "mock-token"
        self.calls = []

    def get_history_data(self, start_time, end_time, node_ids):
        self.calls.append((start_time, end_time, list(node_ids)))
        logger.info(f"[MockHistory] query {start_time} ~ {end_time} nodes={node_ids}")
        # 基础毫秒时间戳
        st_ms = int(START_DT.timestamp() * 1000)
        et_ms = int(END_DT.timestamp() * 1000)
        span = et_ms - st_ms
        out = []
        for nid in node_ids:
            # 把 mock 的源点 id（10001:XXX.AV）作为 nodeId 返回
            # 每个点造 5 个采样，值递增，验证回放排序
            pts = []
            for i in range(5):
                t = st_ms + int(span * i / 4)
                pts.append({"t": t, "v": round(100 + i * 1.5, 2), "q": 0})
            out.append({"nodeId": nid, "data": pts})
        return out


def build_mock_task(orig_task):
    """基于真实任务定义构造 mock 任务：history_id 已是完整带域格式(10001:xxx.AV)直接复用，
    target_id 统一用可写的源点（真实设计器目标点 AGC17 等在实时库中不可写）。"""
    return {
        "id": orig_task["id"],
        "module": orig_task["module"],
        "source": orig_task["source"],
        "desc": orig_task["desc"],
        "ac_node": orig_task["ac_node"],
        "fc_node": orig_task["fc_node"],
        "start_components": orig_task["start_components"],
        "end_components": orig_task["end_components"],
        "points": [
            {
                "history_id": p["history_id"],
                "target_id": RTDB_WRITE_TARGET,
                "target_node": p["target_node"],
            }
            for p in orig_task["points"]
        ],
    }


async def run():
    logger.info("=" * 70)
    logger.info("本地 Mock 流程测试：跑通 12 任务 _handle_task 业务逻辑")
    logger.info(f"测试时间区间: {START_STR} ~ {END_STR}")
    logger.info(f"RTDB 回写目标: {RTDB_WRITE_TARGET} (源点，write 可成功)")
    logger.info("=" * 70)

    # 实例化服务（不 start，不监听端口）
    service = DataHubService()

    # 先用真实 token 初始化 rtdb 客户端
    token = service.auth.get_token()
    if not token:
        logger.error("Token 获取失败，无法继续")
        return
    service._init_clients(token)
    logger.info("Token 获取成功，真实 RTDB 客户端已初始化")

    # 替换为 mock
    mock_opcua = MockOPCUA()
    mock_history = MockHistoryClient()
    service.opcua = mock_opcua
    service.history = mock_history

    # mock _ensure_token 直接返回 True（避免重建 history 覆盖 mock）
    async def fake_ensure_token():
        logger.info("[mock] _ensure_token -> True (skip client rebuild)")
        return True
    service._ensure_token = fake_ensure_token

    # 跳过 settle 等待加速测试（settle 仅等待历史落库，mock 场景无此需求）
    async def fake_sleep(seconds):
        logger.debug(f"[mock] skip sleep {seconds}s")
    service._sleep_or_stop = fake_sleep

    results = []
    for orig_task in tasks_config.TASKS:
        tid = orig_task["id"]
        task = build_mock_task(orig_task)
        logger.info(f"\n{'='*50}\n>>> 测试任务 {tid}: {orig_task['source']}/{orig_task['desc']}\n{'='*50}")
        # 重置任务阶段状态
        service._set_task_stage(tid, "idle")
        try:
            success, detail, stage = await service._handle_task(task)
            results.append((tid, success, detail, stage))
            logger.info(f"<<< 任务 {tid} 结果: success={success} detail={detail} stage={stage}")
        except Exception as e:
            logger.exception(f"任务 {tid} 异常: {e}")
            results.append((tid, False, str(e), "exception"))

    # 汇总
    logger.info("\n" + "=" * 70)
    logger.info("测试汇总")
    logger.info("=" * 70)
    ok = sum(1 for r in results if r[1])
    fail = len(results) - ok
    for tid, success, detail, stage in results:
        status = "PASS" if success else "FAIL"
        logger.info(f"  [{tid}] {status}  stage={stage}  detail={detail}")
    logger.info(f"\n总计: {len(results)} 个任务, {ok} 成功, {fail} 失败")
    logger.info(f"FC 置位次数: {len(mock_opcua.fc_writes)}")
    logger.info(f"历史查询次数: {len(mock_history.calls)}")


if __name__ == "__main__":
    asyncio.run(run())
