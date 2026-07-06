
import asyncio
import logging
from auth_client import AuthClient
from history_api import HistoryAPIClient
from rt_db_client import RTDBClient

# ================= 配置区 =================
BASE_IP = "http://192.168.1.35:6543"
APP_CODE = "data"
APP_SECRET = "123456"

# 需要在历史库中真实存在的触发点 (用于测试)
# 请确保这个点在过去 10 分钟内有过从 1 变 0 的记录
TRIG_HISTORY_ID = "10001:ICSSYS.Trigger" 

WATCH_LIST = [
    "10001:ICSSYS0001.AVGV",
    "10002:ICSSYS0001.AVGV",
]
# ==========================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

async def run_simulation():
    logging.info(">>> 启动模拟触发测试 (不依赖 OPC UA) <<<")
    
    # 1. 登录
    auth = AuthClient(BASE_IP, APP_CODE, APP_SECRET)
    token = auth.get_token()
    if not token:
        logging.error("登录失败，请检查 AppCode/Secret")
        return

    history = HistoryAPIClient(BASE_IP, token)
    rtdb = RTDBClient(BASE_IP, token)

    logging.info(f"--- 步骤 1: 正在从历史库分析 '{TRIG_HISTORY_ID}' 的脉冲时间 ---")
    
    # 2. 调用历史溯源逻辑
    st, et = history.get_pulse_times(TRIG_HISTORY_ID, lookback_minutes=60)
    
    if st and et:
        logging.info(f"成功模拟触发！检测到历史生产区间: {st} 至 {et}")
        
        # 3. 抓取点位数据
        logging.info(f"--- 步骤 2: 抓取 {len(WATCH_LIST)} 个点位的历史值 ---")
        history_data = history.get_history_data(st, et, WATCH_LIST)
        
        if history_data:
            # 4. 回写实时库
            logging.info(f"--- 步骤 3: 正在回写数据 ---")
            payload = []
            for node_entry in history_data:
                points = node_entry.get("data", [])
                if points:
                    last_val = points[-1].get("v")
                    payload.append({
                        "nodeId": node_entry.get("nodeId"), 
                        "value": last_val
                    })

            if payload:
                rtdb.write_realtime_data(payload)
                logging.info(">>> 模拟测试完成！数据处理逻辑闭环。")
            else:
                logging.warning("没有可写的数据。")
        else:
            logging.warning("该时间段内未抓取到 WATCH_LIST 中的数据点。")
    else:
        logging.warning(f"未能从历史点 '{TRIG_HISTORY_ID}' 中找到值为 1 的脉冲记录。")
        logging.info("提示：请确保该点在历史库中有数据，或者你可以运行 test_full_flow.py 直接使用固定时间测试。")

if __name__ == "__main__":
    asyncio.run(run_simulation())
