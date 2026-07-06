"""
状态持久化管理。

职责：
1. 记录上次同步的脉冲结束时间戳，用于重启后去重，避免同一脉冲被重复同步。
2. 缓存 RTDB 写入失败的数据，后续周期自动重试。

文件格式：JSON
{
    "last_synced_et": "1712345678000",   # 上次成功同步的脉冲结束时间戳（字符串）
    "failed_writes": [                   # 失败写入队列
        {
            "nodeId": "...",
            "value": ...,
            "ts": 1712345678,            # 加入缓存的时间（秒级 unix）
            "pulse_et": "1712345678000"   # 关联的脉冲结束时间戳
        }
    ]
}
"""
import os
import json
import time
import threading
import logging


class StateManager:
    def __init__(self, state_file,
                 cache_max_age_hours=24,
                 cache_max_entries=1000):
        self.state_file = state_file
        self.cache_max_age_seconds = cache_max_age_hours * 3600
        self.cache_max_entries = cache_max_entries
        self._lock = threading.Lock()
        self.logger = logging.getLogger(__name__)

        self._state = {
            "last_synced_et": None,
            "failed_writes": []
        }
        self.load()

    def load(self):
        """从磁盘加载状态"""
        with self._lock:
            if not os.path.exists(self.state_file):
                return
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._state["last_synced_et"] = data.get("last_synced_et")
                    fw = data.get("failed_writes", [])
                    if isinstance(fw, list):
                        # 加载时清理过期条目
                        now = time.time()
                        self._state["failed_writes"] = [
                            e for e in fw
                            if isinstance(e, dict)
                            and (now - float(e.get("ts", 0))) < self.cache_max_age_seconds
                        ]
                    self.logger.info(
                        f"State loaded: last_synced_et={self._state['last_synced_et']}, "
                        f"failed_writes={len(self._state['failed_writes'])}"
                    )
            except Exception as e:
                self.logger.warning(f"Failed to load state file: {e}, starting fresh")
                self._state = {"last_synced_et": None, "failed_writes": []}

    def save(self):
        """持久化状态到磁盘"""
        with self._lock:
            try:
                tmp = f"{self.state_file}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._state, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.state_file)
            except Exception as e:
                self.logger.warning(f"Failed to save state file: {e}")

    def get_last_synced_et(self):
        with self._lock:
            return self._state.get("last_synced_et")

    def set_last_synced_et(self, et):
        with self._lock:
            self._state["last_synced_et"] = et
        self.save()

    def is_pulse_synced(self, et):
        """判断该脉冲结束时间戳是否已同步过"""
        with self._lock:
            return self._state.get("last_synced_et") == et

    def add_failed_write(self, payload, pulse_et=None):
        """添加写入失败的负载到缓存队列"""
        if not payload:
            return
        with self._lock:
            now = time.time()
            for item in payload:
                entry = {
                    "nodeId": item.get("nodeId"),
                    "value": item.get("value"),
                    "ts": now,
                    "pulse_et": pulse_et
                }
                self._state["failed_writes"].append(entry)
            # 超出容量时丢弃最旧的
            if len(self._state["failed_writes"]) > self.cache_max_entries:
                overflow = len(self._state["failed_writes"]) - self.cache_max_entries
                self._state["failed_writes"] = self._state["failed_writes"][overflow:]
                self.logger.warning(
                    f"Failed-write cache overflow, dropped {overflow} oldest entries"
                )
        self.save()

    def pop_failed_writes(self, max_count=None):
        """取出待重试的失败写入（不删除，由 set_failed_writes 确认后替换）"""
        with self._lock:
            now = time.time()
            valid = [
                e for e in self._state["failed_writes"]
                if (now - float(e.get("ts", 0))) < self.cache_max_age_seconds
            ]
            self._state["failed_writes"] = valid
            if max_count is not None:
                return valid[:max_count]
            return list(valid)

    def count_failed_writes(self):
        """返回当前缓存中失败写入的数量（同时清理过期项）"""
        with self._lock:
            now = time.time()
            valid = [
                e for e in self._state["failed_writes"]
                if (now - float(e.get("ts", 0))) < self.cache_max_age_seconds
            ]
            self._state["failed_writes"] = valid
            return len(valid)

    def set_failed_writes(self, remaining):
        """用重试后剩余的列表替换缓存"""
        with self._lock:
            self._state["failed_writes"] = list(remaining)
        self.save()

    def clear(self):
        with self._lock:
            self._state = {"last_synced_et": None, "failed_writes": []}
        self.save()
