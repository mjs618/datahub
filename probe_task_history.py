import logging
from auth_client import AuthClient
from history_api import HistoryAPIClient
from config import config
import tasks_config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("probe_task_history")

# 测试时间范围（有真实数据的时间段）
START_TIME = "2026-03-16 11:18:00"
END_TIME = "2026-03-16 12:18:00"


def probe_tasks():
    base_ip = config.BASE_IP
    auth = AuthClient(base_ip, config.APP_CODE, config.APP_SECRET)
    token = auth.get_token()
    if not token:
        logger.error("获取 Token 失败")
        return

    history = HistoryAPIClient(base_ip, token, page_size=0)

    for task in tasks_config.TASKS:
        tid = task["id"]
        history_ids = [p["history_id"] for p in task["points"]]
        logger.info(f"[{tid}] {task['desc']} 历史点: {history_ids}")

        data = history.get_history_data(START_TIME, END_TIME, history_ids)
        if not data:
            logger.warning(f"[{tid}] 无历史数据")
            continue

        for entry in data:
            node_id = entry.get("nodeId")
            points = entry.get("data", []) or []
            logger.info(f"[{tid}] {node_id}: {len(points)} 个点")
            for p in points[:3]:
                logger.info(f"    t={p.get('t')} v={p.get('v')}")


if __name__ == "__main__":
    logger.info(f"=== 探测 12 个任务的历史数据 ({START_TIME} ~ {END_TIME}) ===")
    probe_tasks()
