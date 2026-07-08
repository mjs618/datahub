import logging
from auth_client import AuthClient
from history_api import HistoryAPIClient
from config import config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("probe_formats")

START_TIME = "2026-03-16 11:18:00"
END_TIME = "2026-03-16 12:18:00"

BASE_POINTS = [
    "AMI_JZ1_FHGD",
    "JZ1_AI49",
    "AMI_JZ2_FHGD",
    "JZ2_AI49",
    "AMI_JZ1_WGGD",
    "JZ1_AI50",
    "AMI_JZ2_WGGD",
    "JZ2_AI50",
    "T1CKDLQ_F",
    "T2CKDLQ_F",
]

FORMATS = [
    "{unit}:{point}.AV",
    "{unit}:{point}.AVGV",
    "DBUNIT_001:{point}.AV",
    "DBUNIT_001:{point}.AVGV",
]


def probe():
    base_ip = config.BASE_IP
    auth = AuthClient(base_ip, config.APP_CODE, config.APP_SECRET)
    token = auth.get_token()
    if not token:
        logger.error("获取 Token 失败")
        return

    history = HistoryAPIClient(base_ip, token, page_size=0)

    for point in BASE_POINTS:
        logger.info(f"=== 探测点: {point} ===")
        for fmt in FORMATS:
            node_id = fmt.format(unit="10001", point=point)
            data = history.get_history_data(START_TIME, END_TIME, [node_id])
            if data:
                entry = data[0]
                points = entry.get("data", []) or []
                logger.info(f"  命中 [{fmt}]: {len(points)} 个点")
            else:
                logger.info(f"  未命中 [{fmt}]")


if __name__ == "__main__":
    probe()
