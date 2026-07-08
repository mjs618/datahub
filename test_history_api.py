import logging
from auth_client import AuthClient
from history_api import HistoryAPIClient
from config import config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("test_history")

# 测试参数
START_TIME = "2026-03-16 11:18:00"
END_TIME = "2026-03-16 12:18:00"
NODES = [
    "10001:ICSSYS0001.AVGV",
    "10002:ICSSYS0001.AVGV"
]


def test_history():
    """通过 BASE_IP (6543) 获取 token 并查询历史数据"""
    base_ip = config.BASE_IP
    logger.info(f"目标地址: {base_ip}")

    auth = AuthClient(base_ip, config.APP_CODE, config.APP_SECRET)
    token = auth.get_token()
    if not token:
        logger.error("获取 Token 失败")
        return False

    logger.info(f"Token 获取成功: {token[:20]}...")

    client = HistoryAPIClient(base_ip, token, page_size=0)
    data = client.get_history_data(START_TIME, END_TIME, NODES)

    if data:
        logger.info(f"查询成功，返回 {len(data)} 条 node-entries")
        for item in data:
            node_id = item.get('nodeId')
            points = item.get('data', []) or []
            logger.info(f"  Node: {node_id} | 点数: {len(points)}")
            for p in points[:5]:
                logger.info(f"    t={p.get('t')} v={p.get('v')}")
        return True
    else:
        logger.warning("查询返回为空")
        return False


if __name__ == "__main__":
    logger.info("=== 历史数据获取测试开始 ===")
    logger.info(f"时间范围: {START_TIME} ~ {END_TIME}")
    logger.info(f"测点: {NODES}")

    ok = test_history()

    logger.info("=== 历史数据获取测试结束 ===")
    if ok:
        logger.info("结果: 历史数据接口正常")
    else:
        logger.error("结果: 历史数据接口异常")
