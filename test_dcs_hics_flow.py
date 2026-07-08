"""
DCS -> HICS 端到端流程测试（6 个王快任务）。

策略：
  - Token：真实 /api/gateway/appSignIn
  - 历史数据：真实 findAll 查询 10001 域；平台 history/write 假成功（数据不落盘），
    故当真实查询无数据时，在客户端层注入按时间区间的测试数据，使回放逻辑可验证。
  - RTDB 回写：真实调用 HICS(10011) write 接口，记录真实结果；
    若失败（域离线），mock 返回成功以跑通后续 FC 反馈环节（用户要求先不管平台问题）。
  - OPC UA：mock 时间分量 + FC 置位（OPC UA 6810 端口未开放）。

复用 main.py 真实 _handle_task 业务逻辑，验证完整 6 阶段：
  token -> settle -> read_time -> history -> rtdb_replay -> fc_feedback -> completed
"""
import asyncio
import datetime
import logging
import os

os.environ["HEALTH_ENDPOINT_ENABLED"] = "false"

import requests
from main import DataHubService
from config import config
import tasks_config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(threadName)s] %(name)s - %(message)s'
)
logger = logging.getLogger("dcs_hics")

# 测试时间区间（今天的一个区间，1 分钟跨度）
NOW = datetime.datetime.now()
START_DT = NOW.replace(hour=9, minute=0, second=0, microsecond=0)
END_DT = START_DT + datetime.timedelta(minutes=1)
START_STR = START_DT.strftime("%Y-%m-%d %H:%M:%S")
END_STR = END_DT.strftime("%Y-%m-%d %H:%M:%S")

# 真实 HICS write 的 URL
WURL = f"{config.BASE_IP.rstrip('/')}/api/hsm-db-rtserver/v1/rtdata/node/write"


class MockOPCUA:
    """模拟 OPC UA：返回 start/end 时间分量，接受 FC 写入。"""
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


class InjectHistoryClient:
    """历史客户端：先真实查询 10001 域，无数据则注入测试数据。"""
    def __init__(self, real_client):
        self.token = real_client.token
        self._real = real_client
        self.injected = 0
        self.real_hits = 0

    def get_history_data(self, start_time, end_time, node_ids):
        d = self._real.get_history_data(start_time, end_time, node_ids)
        # 检查是否有真实数据
        has_data = any((e.get("data") or []) for e in d)
        if has_data:
            self.real_hits += 1
            logger.info(f"[History] 真实数据命中: {len(d)} 节点")
            return d
        # 无数据，注入测试数据（按时间区间造采样点）
        self.injected += 1
        logger.info(f"[History] 真实查询无数据，注入测试数据: {len(node_ids)} 节点")
        try:
            st_ms = int(START_DT.timestamp() * 1000)
            et_ms = int(END_DT.timestamp() * 1000)
        except Exception:
            st_ms = int(datetime.datetime.now().timestamp() * 1000)
            et_ms = st_ms + 60000
        span = et_ms - st_ms
        out = []
        for nid in node_ids:
            pts = []
            for i in range(6):  # 6 个采样点，验证回放排序
                t = st_ms + int(span * i / 5)
                pts.append({"t": t, "v": round(200 + i * 5.5, 2), "q": 0})
            out.append({"nodeId": nid, "data": pts})
        return out


class RealRTDBWrapper:
    """RTDB 包装：真实调用 HICS write，记录结果；失败则 mock 成功以跑通流程。"""
    def __init__(self, token):
        self.token = token
        self.real_results = []  # 记录每次真实 write 的结果
        self.mocked_success = 0

    def write_realtime_data(self, items, strict_mode=False):
        """返回与 RTDBClient 一致的 dict 格式。真实调用 HICS write，失败则 mock 成功。"""
        hdr = {"Authorization": self.token, "Content-Type": "application/json"}
        payload = [{"nodeId": it["nodeId"], "value": it["value"]} for it in items]
        result = {"success": False, "code": -1, "message": "", "results": None, "attempt": 1}
        try:
            r = requests.post(WURL, json={"data": payload, "strictMode": strict_mode}, headers=hdr, timeout=10).json()
            codes = r.get("data") or []
            all_ok = all(c == 0 for c in codes)
            result["code"] = r.get("code", -1)
            result["results"] = codes
            self.real_results.append({"count": len(items), "codes": codes, "ok": all_ok})
            if all_ok:
                logger.info(f"[RTDB] 真实 write 成功: {len(items)} 点")
                result["success"] = True
                result["message"] = "ok"
            else:
                logger.warning(f"[RTDB] 真实 write 失败(平台问题,mock成功): codes={codes}")
                self.mocked_success += 1
                result["success"] = True
                result["message"] = "ok(mock:平台write失败,域离线)"
        except Exception as e:
            self.real_results.append({"count": len(items), "error": str(e)})
            logger.warning(f"[RTDB] 真实 write 异常(平台问题,mock成功): {e}")
            self.mocked_success += 1
            result["success"] = True
            result["message"] = "ok(mock:平台write异常)"
        return result


async def run():
    logger.info("=" * 70)
    logger.info("DCS -> HICS 端到端流程测试（6 个王快任务）")
    logger.info(f"测试时间区间: {START_STR} ~ {END_STR}")
    logger.info("历史: 真实查询+注入 | RTDB: 真实调用HICS(失败则mock) | OPCUA: mock")
    logger.info("=" * 70)

    service = DataHubService()
    token = service.auth.get_token()
    if not token:
        logger.error("Token 获取失败")
        return
    service._init_clients(token)
    logger.info("Token 获取成功，真实 RTDB/History 客户端已初始化")

    # 替换依赖
    mock_opcua = MockOPCUA()
    inj_history = InjectHistoryClient(service.history)
    real_rtdb = RealRTDBWrapper(token)
    service.opcua = mock_opcua
    service.history = inj_history
    service.rtdb = real_rtdb

    async def fake_ensure_token():
        return True
    service._ensure_token = fake_ensure_token

    async def fake_sleep(seconds):
        pass
    service._sleep_or_stop = fake_sleep

    # 只跑 DCS（王快）任务
    dcs_tasks = [t for t in tasks_config.TASKS if t["source"] == "王快"]
    logger.info(f"DCS(王快)任务数: {len(dcs_tasks)}")

    results = []
    for orig in dcs_tasks:
        tid = orig["id"]
        logger.info(f"\n{'='*55}\n>>> 任务 {tid}: {orig['source']}/{orig['desc']}\n{'='*55}")
        service._set_task_stage(tid, "idle")
        try:
            success, detail, stage = await service._handle_task(orig)
            results.append((tid, success, detail, stage))
            logger.info(f"<<< 任务 {tid}: success={success} detail={detail} stage={stage}")
        except Exception as e:
            logger.exception(f"任务 {tid} 异常: {e}")
            results.append((tid, False, str(e), "exception"))

    # 汇总
    logger.info("\n" + "=" * 70)
    logger.info("DCS -> HICS 端到端测试汇总")
    logger.info("=" * 70)
    ok = sum(1 for r in results if r[1])
    for tid, success, detail, stage in results:
        logger.info(f"  [{tid}] {'PASS' if success else 'FAIL'}  stage={stage}  detail={detail}")
    logger.info(f"\n总计: {len(results)} 任务, {ok} 成功")
    logger.info(f"历史注入次数: {inj_history.injected} | 真实命中: {inj_history.real_hits}")
    logger.info(f"FC 置位次数: {len(mock_opcua.fc_writes)}")
    logger.info(f"RTDB 真实 write 调用: {len(real_rtdb.real_results)} 次")
    for i, rr in enumerate(real_rtdb.real_results):
        logger.info(f"  调用{i+1}: {rr}")
    logger.info(f"RTDB mock 成功次数(平台write失败): {real_rtdb.mocked_success}")


if __name__ == "__main__":
    asyncio.run(run())
