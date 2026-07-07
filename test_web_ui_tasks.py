"""
端到端测试：启动 WebUIServer 并验证任务管理 API + 配置端点。

测试覆盖：
1. WebUIServer 可独立启动
2. GET /api/tasks/status 返回 12 个任务
3. POST /api/tasks/trigger 对合法 task_id 返回 triggered
4. POST /api/tasks/trigger 对非法 task_id 返回错误
5. GET /api/config 包含 TASK_MODE / WRITE_BACK_VIA 字段
6. POST /api/config 接受清空 WATCH_LIST/NODE_MAPPING
7. 首页 HTML 包含新增的「任务管理」Tab 标识
"""
import json
import threading
import time
import urllib.request
import urllib.error
import sys

from web_ui import WebUIServer
from tasks_config import TASKS


def fake_task_status():
    return {"tasks": [
        {
            "id": t["id"], "module": t["module"], "source": t["source"],
            "desc": t["desc"], "ac_node": t["ac_node"], "fc_node": t["fc_node"],
            "processing": False, "ac_prev": None, "last_result": None,
        } for t in TASKS
    ]}


triggered = {"calls": []}


def fake_task_trigger(task_id):
    triggered["calls"].append(task_id)
    if task_id in [t["id"] for t in TASKS]:
        return {"status": "triggered", "task_id": task_id}
    return {"status": "error", "error": f"unknown task id: {task_id}"}


def main():
    server = WebUIServer(
        opcua_url="opc.tcp://127.0.0.1:6810",
        trigger_sync_callback=lambda: {"status": "triggered"},
        status_getter=lambda: {"pending": False, "last_result": None},
        metrics_getter=lambda: {"started_at": time.time(), "uptime_seconds": 0},
        trigger_state_getter=lambda: {"opcua_connected": False, "trigger_value": None},
        task_trigger_callback=fake_task_trigger,
        task_status_getter=fake_task_status,
        port=18089,
    )
    server.start()
    try:
        time.sleep(0.5)
        base = "http://127.0.0.1:18089"
        failures = []

        def get(path):
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, r.read().decode("utf-8")

        def post_json(path, body):
            req = urllib.request.Request(
                base + path, data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, r.read().decode("utf-8")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8")

        # 1. 首页 HTML
        s, body = get("/")
        assert s == 200, f"GET / failed: {s}"
        assert "任务管理" in body, "HTML 未包含「任务管理」Tab"
        assert 'data-tab="tasks"' in body, "HTML 未包含 tasks tab 按钮"
        assert "loadTasks()" in body, "HTML 未包含 loadTasks 函数"
        assert "triggerTask(" in body, "HTML 未包含 triggerTask 函数"
        assert "clearWatchList()" in body, "HTML 未包含 clearWatchList 函数"
        assert "resetConfigForm()" in body, "HTML 未包含 resetConfigForm 函数"
        assert "applySyncModeHint" in body, "HTML 未包含 applySyncModeHint 函数"
        print("[OK] 1. 首页 HTML 含任务管理 Tab 与新函数")

        # 2. 任务状态
        s, body = get("/api/tasks/status")
        assert s == 200, f"GET /api/tasks/status failed: {s}"
        data = json.loads(body)
        assert "tasks" in data, "响应缺 tasks 字段"
        assert len(data["tasks"]) == 12, f"任务数 != 12, got {len(data['tasks'])}"
        first = data["tasks"][0]
        for k in ("id", "module", "source", "desc", "ac_node", "fc_node", "processing"):
            assert k in first, f"任务缺字段 {k}"
        print(f"[OK] 2. /api/tasks/status 返回 12 个任务，首任务 id={first['id']}")

        # 3. 触发合法任务
        triggered["calls"].clear()
        s, body = post_json("/api/tasks/trigger", {"task_id": "AGC01"})
        assert s == 200, f"POST /api/tasks/trigger 合法 ID failed: {s}"
        data = json.loads(body)
        assert data.get("status") == "triggered", f"未触发: {data}"
        assert "AGC01" in triggered["calls"], "callback 未被调用"
        print(f"[OK] 3. 触发 AGC01 成功，callback 收到: {triggered['calls']}")

        # 4. 触发非法任务
        s, body = post_json("/api/tasks/trigger", {"task_id": "NOPE99"})
        assert s == 400, f"非法 ID 应返回 400, got {s}"
        data = json.loads(body)
        assert data.get("status") == "error", f"应返回 error: {data}"
        print(f"[OK] 4. 触发非法任务 NOPE99 返回错误 (HTTP {s})")

        # 5. 触发缺 task_id
        s, body = post_json("/api/tasks/trigger", {})
        assert s == 400, f"缺 task_id 应返回 400, got {s}"
        print(f"[OK] 5. 缺 task_id 返回 400")

        # 6. 配置端点含 TASK_MODE / WRITE_BACK_VIA
        s, body = get("/api/config")
        assert s == 200, f"GET /api/config failed: {s}"
        cfg = json.loads(body)
        for k in ("TASK_MODE", "WRITE_BACK_VIA", "BASE_IP", "OPCUA_URL"):
            assert k in cfg, f"配置缺字段 {k}"
        print(f"[OK] 6. /api/config 含 TASK_MODE={cfg['TASK_MODE']}, WRITE_BACK_VIA={cfg['WRITE_BACK_VIA']}")

        # 7. 清空 WATCH_LIST/NODE_MAPPING
        s, body = post_json("/api/config", {"WATCH_LIST": [], "NODE_MAPPING": {}})
        assert s == 200, f"清空配置失败: {s}"
        cfg2 = json.loads(body)
        assert cfg2["WATCH_LIST"] == [], "WATCH_LIST 未清空"
        assert cfg2["NODE_MAPPING"] == {}, "NODE_MAPPING 未清空"
        print(f"[OK] 7. 清空 WATCH_LIST/NODE_MAPPING 成功")

        # 8. 调优参数校验：非法 POLL_INTERVAL
        s, body = post_json("/api/config", {"POLL_INTERVAL": 99999})
        assert s == 400, f"非法 POLL_INTERVAL 应返回 400, got {s}"
        print(f"[OK] 8. 非法 POLL_INTERVAL=99999 被拒绝 (HTTP {s})")

        print("\n========== 所有测试通过 ==========")
    except AssertionError as e:
        print(f"\n[FAIL] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)
    finally:
        server.stop()


if __name__ == "__main__":
    main()
