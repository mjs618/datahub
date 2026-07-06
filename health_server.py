"""
健康检查端点 + 监控指标。

通过轻量 HTTP 服务暴露 /health 端点，供 Docker / Kubernetes 健康检查使用。
同时维护一组运行时指标，可通过 /metrics 查看。

设计为线程内运行，不依赖第三方库，使用标准库 http.server。
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Metrics:
    """线程安全的运行时指标收集"""

    def __init__(self):
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.last_loop_at = 0.0
        self.trigger_count = 0
        self.sync_success_count = 0
        self.sync_failure_count = 0
        self.last_sync_status = "idle"
        self.last_sync_at = 0.0
        self.last_sync_message = ""
        self.opcua_connected = False
        self.token_valid = False
        self.failed_cache_size = 0

    def mark_loop(self):
        with self._lock:
            self.last_loop_at = time.time()

    def mark_trigger(self):
        with self._lock:
            self.trigger_count += 1

    def mark_sync(self, success, message=""):
        with self._lock:
            self.last_sync_at = time.time()
            if success:
                self.sync_success_count += 1
                self.last_sync_status = "success"
            else:
                self.sync_failure_count += 1
                self.last_sync_status = "failure"
            self.last_sync_message = message

    def set_opcua_connected(self, v):
        with self._lock:
            self.opcua_connected = bool(v)

    def set_token_valid(self, v):
        with self._lock:
            self.token_valid = bool(v)

    def set_failed_cache_size(self, n):
        with self._lock:
            self.failed_cache_size = int(n)

    def snapshot(self):
        with self._lock:
            return {
                "started_at": self.started_at,
                "last_loop_at": self.last_loop_at,
                "trigger_count": self.trigger_count,
                "sync_success_count": self.sync_success_count,
                "sync_failure_count": self.sync_failure_count,
                "last_sync_status": self.last_sync_status,
                "last_sync_at": self.last_sync_at,
                "last_sync_message": self.last_sync_message,
                "opcua_connected": self.opcua_connected,
                "token_valid": self.token_valid,
                "failed_cache_size": self.failed_cache_size,
                "uptime_seconds": time.time() - self.started_at,
            }


class HealthServer:
    """HTTP 健康检查服务，后台线程运行"""

    def __init__(self, metrics: Metrics, port=8088, stale_threshold=60):
        self.metrics = metrics
        self.port = port
        self.stale_threshold = stale_threshold
        self._server = None
        self._thread = None

    def _is_healthy(self):
        snap = self.metrics.snapshot()
        if not snap["opcua_connected"]:
            return False, "opcua disconnected"
        if not snap["token_valid"]:
            return False, "token invalid"
        if snap["last_loop_at"] == 0:
            # 启动初期允许
            if snap["uptime_seconds"] < 30:
                return True, "starting up"
            return False, "main loop never ran"
        if (time.time() - snap["last_loop_at"]) > self.stale_threshold:
            return False, "main loop stale"
        return True, "ok"

    def start(self):
        metrics = self.metrics
        is_healthy_fn = self._is_healthy

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                # 静默默认访问日志，避免刷屏
                pass

            def _send(self, code, body):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)

            def do_GET(self):
                if self.path in ("/health", "/healthz", "/"):
                    healthy, reason = is_healthy_fn()
                    payload = {
                        "status": "healthy" if healthy else "unhealthy",
                        "reason": reason,
                        **metrics.snapshot()
                    }
                    self._send(200 if healthy else 503, payload)
                elif self.path in ("/metrics",):
                    self._send(200, metrics.snapshot())
                else:
                    self._send(404, {"error": "not found"})

        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="HealthServer", daemon=True
        )
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
