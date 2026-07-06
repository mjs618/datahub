
import logging
from history_api import HistoryAPIClient
from rt_db_client import RTDBClient
from auth_client import AuthClient

# 配置信息
BASE_IP = "http://192.168.1.35:6543"
APP_CODE = "data"
APP_SECRET = "123456"

# 测试参数
START_TIME = "2026-03-16 11:18:00"
END_TIME = "2026-03-16 12:18:00"
NODES = [
    "10001:ICSSYS0001.AVGV",
    "10002:ICSSYS0001.AVGV"
]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def run_integration_test():
    # 获取 Token
    auth = AuthClient(BASE_IP, APP_CODE, APP_SECRET)
    token = auth.get_token()
    if not token:
        logging.error("无法获取 Token，测试中止。")
        return

    history = HistoryAPIClient(BASE_IP, token)
    rtdb = RTDBClient(BASE_IP, token)
    
    # 1. 读取历史数据
    logging.info(f"--- 步骤 1: 读取历史数据 ({START_TIME} -> {END_TIME}) ---")
    history_data = history.get_history_data(START_TIME, END_TIME, NODES)
    
    if not history_data:
        logging.error("未能获取到历史数据，测试终止。")
        return

    logging.info(f"成功获取到 {len(history_data)} 条历史数据点。")

    # 2. 转换数据格式
    logging.info("--- 步骤 2: 准备回写数据 ---")
    write_payload = []
    for node_entry in history_data:
        node_id = node_entry.get("nodeId")
        points = node_entry.get("data", [])
        if points:
            # 取最后一条数据（脉冲截止时的最新值）
            last_point = points[-1]
            write_payload.append({
                "nodeId": node_id,
                "value": last_point.get("v")
            })

    # 3. 批量回写到实时库
    logging.info(f"--- 步骤 3: 正在回写 {len(write_payload)} 条数据到实时库 ---")
    success = rtdb.write_realtime_data(write_payload)
    
    if success:
        logging.info(">>> 集成测试成功！数据已成功读取并回写。")
    else:
        logging.error(">>> 集成测试失败！回写环节出现错误。")

if __name__ == "__main__":
    run_integration_test()
