import asyncio
import os
import tempfile

TEST_RUNTIME_CONFIG_FILE = os.path.join(
    tempfile.gettempdir(), f"datahub_test_task_replay_runtime_{os.getpid()}.json"
)
os.environ["RUNTIME_CONFIG_FILE"] = TEST_RUNTIME_CONFIG_FILE
os.environ.setdefault("SETTLE_TIME", "0.1")

from config import config
from main import DataHubService
import tasks_config


class FakeOPCUA:
    connected = True

    def __init__(self):
        self.writes = []

    async def read_values(self, node_ids):
        values = {
            "YEAR_AGC01": 2026,
            "MON_AGC01": 6,
            "DAY_AGC01": 28,
            "HOUR_AGC01": 13,
            "MIN_AGC01": 50,
            "SEC_AGC01": 0,
            "YEAR_AGC11": 2026,
            "MON_AGC11": 6,
            "DAY_AGC11": 28,
            "HOUR_AGC11": 13,
            "MIN_AGC11": 55,
            "SEC_AGC11": 0,
        }
        return {node_id: values.get(node_id.split("=")[-1]) for node_id in node_ids}

    async def write_value(self, node_id, value):
        self.writes.append((node_id, value))
        return True


class FakeHistory:
    def __init__(self):
        self.calls = []

    def get_history_data(self, start_time, end_time, node_ids):
        self.calls.append((start_time, end_time, list(node_ids)))
        return [
            {
                "nodeId": "AMI_JZ1_FHGD",
                "data": [
                    {"t": 30, "v": 103.0},
                    {"t": 10, "v": 101.0},
                    {"t": 20, "v": 102.0},
                ],
            },
            {
                "nodeId": "JZ1_AI49",
                "data": [
                    {"t": 15, "v": 201.0},
                    {"t": 25, "v": 202.0},
                ],
            },
        ]


class FakeRTDB:
    def __init__(self):
        self.payloads = []

    def write_realtime_data(self, data_list, strict_mode=False):
        self.payloads.append((list(data_list), strict_mode))
        return {"success": True, "code": 0, "message": "success", "data": [0] * len(data_list)}


async def run_agc01_replay(batch_size=None):
    old_batch_size = getattr(config, "DEFAULT_RTDB_REPLAY_BATCH_SIZE", None)
    if batch_size is not None:
        config.DEFAULT_RTDB_REPLAY_BATCH_SIZE = batch_size
    service = DataHubService()
    service.opcua = FakeOPCUA()
    service.history = FakeHistory()
    service.rtdb = FakeRTDB()

    async def token_ok():
        return True

    service._ensure_token = token_ok
    try:
        await service._dispatch_task("AGC01")
    finally:
        if old_batch_size is None:
            try:
                delattr(config, "DEFAULT_RTDB_REPLAY_BATCH_SIZE")
            except AttributeError:
                pass
        else:
            config.DEFAULT_RTDB_REPLAY_BATCH_SIZE = old_batch_size
    return service


def test_task_mode_defaults_to_rtdb_writeback():
    assert config.WRITE_BACK_VIA == "rtdb"


def test_agc01_replays_all_history_values_to_rtdb_then_sets_fc():
    service = asyncio.run(run_agc01_replay())

    assert service.history.calls == [
        (
            "2026-06-28 13:50:00",
            "2026-06-28 13:55:00",
            ["AMI_JZ1_FHGD", "JZ1_AI49"],
        )
    ]
    assert service.rtdb.payloads == [
        (
            [
                {"nodeId": "AGC17", "value": 101.0},
                {"nodeId": "AGC18", "value": 201.0},
                {"nodeId": "AGC17", "value": 102.0},
                {"nodeId": "AGC18", "value": 202.0},
                {"nodeId": "AGC17", "value": 103.0},
            ],
            False,
        )
    ]
    assert service.opcua.writes == [("ns=2;s=FC_AGC01", True)]
    task = next(t for t in service.get_tasks_status()["tasks"] if t["id"] == "AGC01")
    assert task["current_stage"] == "idle"
    assert task["last_result"]["status"] == "completed"
    assert task["last_result"]["stage"] == "completed"
    assert task["last_result"]["detail"] == "ok"


def test_agc01_replay_batches_rtdb_writes():
    service = asyncio.run(run_agc01_replay(batch_size=2))

    assert service.rtdb.payloads == [
        (
            [
                {"nodeId": "AGC17", "value": 101.0},
                {"nodeId": "AGC18", "value": 201.0},
            ],
            False,
        ),
        (
            [
                {"nodeId": "AGC17", "value": 102.0},
                {"nodeId": "AGC18", "value": 202.0},
            ],
            False,
        ),
        (
            [
                {"nodeId": "AGC17", "value": 103.0},
            ],
            False,
        ),
    ]
    assert service.opcua.writes == [("ns=2;s=FC_AGC01", True)]


if __name__ == "__main__":
    test_task_mode_defaults_to_rtdb_writeback()
    test_agc01_replays_all_history_values_to_rtdb_then_sets_fc()
    test_agc01_replay_batches_rtdb_writes()
    print("task replay tests passed")
