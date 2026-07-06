
import logging
from history_api import HistoryAPIClient

# 配置信息（根据你的 main.py）
BASE_IP = "http://192.168.1.35:6543"
TOKEN = "jZae727K08KaOmKSgOaGzww/XVqGr/PKEgIMkjrcbJI="

# 假设的时间和点位
START_TIME = "2026-03-16 11:18:00"
END_TIME = "2026-03-16 12:18:00"
NODES = [
    "10001:ICSSYS0001.AVGV",
    "10002:ICSSYS0001.AVGV"
]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def test_query():
    client = HistoryAPIClient(BASE_IP, TOKEN)
    logging.info(f"开始测试查询: {START_TIME} 至 {END_TIME}")
    
    data = client.get_history_data(START_TIME, END_TIME, NODES)
    
    if data:
        logging.info(f"查询成功！获取到 {len(data)} 条数据。")
        # 打印前5条示例
        for i, item in enumerate(data[:5]):
            print(f"[{i+1}] Node: {item.get('nodeId')} | Time: {item.get('t')} | Value: {item.get('v')}")
    else:
        logging.warning("查询返回为空，请检查：")
        logging.warning("1. 网络是否连通 (192.168.1.35)")
        logging.warning("2. Token 是否有效")
        logging.warning("3. 点位名称是否完全正确")

if __name__ == "__main__":
    test_query()
