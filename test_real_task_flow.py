import asyncio
import logging
import time
from auth_client import AuthClient
from history_api import HistoryAPIClient
from rt_db_client import RTDBClient
from config import config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("test_real_flow")


class FakeOPCUA:
    connected = True

    async def read_values(self, node_ids):
        # 模拟时间分量：2026-03-16 11:18:00 ~ 11:19:00
        return {
            "ns=10011;s=YEAR_AGC01.AV": 2026,
            "ns=10011;s=MON_AGC01.AV": 3,
            "ns=10011;s=DAY_AGC01.AV": 16,
            "ns=10011;s=HOUR_AGC01.AV": 11,
            "ns=10011;s=MIN_AGC01.AV": 18,
            "ns=10011;s=SEC_AGC01.AV": 0,
            "ns=10011;s=YEAR_AGC11.AV": 2026,
            "ns=10011;s=MON_AGC11.AV": 3,
            "ns=10011;s=DAY_AGC11.AV": 16,
            "ns=10011;s=HOUR_AGC11.AV": 11,
            "ns=10011;s=MIN_AGC11.AV": 19,
            "ns=10011;s=SEC_AGC11.AV": 0,
        }

    async def write_value(self, node_id, value):
        logger.info(f"[FakeOPCUA] write {node_id} = {value}")
        return True


async def run():
    logger.info(">>> 启动真实数据完整业务流程测试（绕过 OPC UA）<<<")

    # 1. 获取 token
    auth = AuthClient(config.BASE_IP, config.APP_CODE, config.APP_SECRET)
    token = auth.get_token()
    if not token:
        logger.error("Token 获取失败")
        return

    # 2. 构造一个使用真实历史点的任务
    # 使用已知有数据的点 10001:ICSSYS0001.AVGV 作为历史源
    task = {
        "id": "REAL01",
        "ac_node": "ns=10011;s=AC_AGC01.DV",
        "fc_node": "ns=10011;s=FC_AGC01.DV",
        "start_components": {
            "year": "ns=10011;s=YEAR_AGC01.AV",
            "mon": "ns=10011;s=MON_AGC01.AV",
            "day": "ns=10011;s=DAY_AGC01.AV",
            "hour": "ns=10011;s=HOUR_AGC01.AV",
            "min": "ns=10011;s=MIN_AGC01.AV",
            "sec": "ns=10011;s=SEC_AGC01.AV",
        },
        "end_components": {
            "year": "ns=10011;s=YEAR_AGC11.AV",
            "mon": "ns=10011;s=MON_AGC11.AV",
            "day": "ns=10011;s=DAY_AGC11.AV",
            "hour": "ns=10011;s=HOUR_AGC11.AV",
            "min": "ns=10011;s=MIN_AGC11.AV",
            "sec": "ns=10011;s=SEC_AGC11.AV",
        },
        "points": [
            {
                "history_id": "10001:ICSSYS0001.AVGV",
                "target_id": "10001:REAL_TARGET_1.AVGV",
                "target_node": "ns=10011;s=REAL_TARGET_1.AV"
            }
        ],
    }

    # 3. 模拟 main.py _handle_task 的核心逻辑
    history = HistoryAPIClient(config.BASE_IP, token, page_size=0)
    rtdb = RTDBClient(config.BASE_IP, token)
    opcua = FakeOPCUA()

    # 读取时间分量
    start_comp = task["start_components"]
    end_comp = task["end_components"]
    all_nodes = list(start_comp.values()) + list(end_comp.values())
    comp_values = await opcua.read_values(all_nodes)

    def assemble_time(components):
        try:
            return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
                int(comp_values[components["year"]]),
                int(comp_values[components["mon"]]),
                int(comp_values[components["day"]]),
                int(comp_values[components["hour"]]),
                int(comp_values[components["min"]]),
                int(comp_values[components["sec"]]),
            )
        except Exception as e:
            logger.error(f"时间拼装失败: {e}")
            return None

    start_str = assemble_time(start_comp)
    end_str = assemble_time(end_comp)
    logger.info(f"时间范围: {start_str} ~ {end_str}")

    # 拉历史数据
    history_ids = [p["history_id"] for p in task["points"]]
    logger.info(f"拉取历史数据: {history_ids}")
    history_data = history.get_history_data(start_str, end_str, history_ids)

    if not history_data:
        logger.error("未获取到历史数据")
        return

    # 构造回写数据
    point_by_id = {p["history_id"]: p for p in task["points"]}
    write_payload = []
    for entry in history_data:
        nid = entry.get("nodeId")
        cfg = point_by_id.get(nid)
        if not cfg:
            continue
        target = cfg.get("target_id")
        for sample in entry.get("data", []) or []:
            val = sample.get("v")
            if val is None:
                continue
            write_payload.append({"nodeId": target, "value": val})

    logger.info(f"准备回写 {len(write_payload)} 个点到 RTDB")
    for item in write_payload[:5]:
        logger.info(f"  {item}")

    # RTDB 回写
    result = rtdb.write_realtime_data(write_payload)
    logger.info(f"RTDB 写入结果: code={result.get('code')} success={result.get('success')} results={result.get('results')}")

    # FC 置位
    fc_ok = await opcua.write_value(task["fc_node"], True)
    logger.info(f"FC 置位结果: {fc_ok}")


if __name__ == "__main__":
    asyncio.run(run())
