import datetime
import requests
import logging


class HistoryAPIClient:
    """
    历史数据查询客户端。
    - 支持 HTTP 超时
    - 支持分页
    - get_pulse_times 返回字符串时间戳，符合 API 规范
    - 对脉冲点按时间排序后再取首尾
    """

    def __init__(self, base_url, token,
                 timeout=10, page_size=0, timestamp_unit="ms"):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.timeout = timeout
        # 0 表示不分页
        self.page_size = page_size
        self.timestamp_unit = timestamp_unit
        self.logger = logging.getLogger(__name__)

    def _headers(self):
        return {
            "Authorization": self.token,
            "Content-Type": "application/json"
        }

    def get_history_data(self, start_time, end_time, node_ids):
        """
        查询历史数据。
        start_time/end_time 可以是 "yyyy-MM-dd HH:mm:ss" 字符串或毫秒值字符串。
        node_ids 为测点列表。
        当 page_size > 0 时自动分页拉取全部数据。
        """
        url = f"{self.base_url}/api/timing-svc/v1/history/findAll"

        # 规范化时间参数为字符串
        st_str = self._normalize_time(start_time)
        et_str = self._normalize_time(end_time)

        base_payload = {
            "startTime": st_str,
            "endTime": et_str,
            "nodeIds": list(node_ids),
            "exactTime": True
        }

        all_data = []
        page = 0
        while True:
            payload = dict(base_payload)
            if self.page_size > 0:
                payload["pageable"] = {
                    "page": page,
                    "pageSize": self.page_size
                }

            try:
                self.logger.info(
                    f"Requesting history for {len(node_ids)} nodes "
                    f"from {st_str} to {et_str} (page={page})"
                )
                response = requests.post(
                    url, json=payload, headers=self._headers(), timeout=self.timeout
                )
                if response.status_code != 200:
                    self.logger.error(
                        f"History API failed with status {response.status_code}: {response.text}"
                    )
                response.raise_for_status()

                result = response.json()
                if result.get("code") != 0:
                    self.logger.error(f"History API Error: {result.get('message')}")
                    return []

                page_data = result.get("data", []) or []
                if not page_data:
                    # 首页就没数据
                    if page == 0:
                        self.logger.info("History API returned empty data")
                    break

                all_data.extend(page_data)

                # 不分页或无分页信息时只取一次
                if self.page_size <= 0:
                    break

                # 分页：判断是否还有更多
                total = result.get("total", 0)
                if len(all_data) >= total or len(page_data) < self.page_size:
                    break
                page += 1
            except Exception as e:
                self.logger.error(f"History API Request failed: {e}")
                return all_data  # 返回已获取的部分

        if all_data:
            self.logger.info(f"History fetched: {len(all_data)} node-entries")
        return all_data

    @staticmethod
    def _normalize_time(t):
        """
        规范化时间参数为字符串。
        - 字符串原样返回
        - int/float 视为毫秒时间戳，转为字符串
        - datetime 对象转为 yyyy-MM-dd HH:mm:ss
        """
        if t is None:
            return None
        if isinstance(t, str):
            return t
        if isinstance(t, (int, float)):
            # API 接受毫秒值字符串
            return str(int(t))
        if isinstance(t, datetime.datetime):
            return t.strftime("%Y-%m-%d %H:%M:%S")
        return str(t)

    def get_pulse_times(self, trigger_node_id, lookback_minutes=10):
        """
        查询触发点的历史，找到最近的 [start, end] 脉冲区间。
        返回 (start_str, end_str)，均为字符串（毫秒值字符串）；
        未找到返回 (None, None)。
        """
        now = datetime.datetime.now()
        start_search = (now - datetime.timedelta(minutes=lookback_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        end_search = now.strftime("%Y-%m-%d %H:%M:%S")

        history = self.get_history_data(start_search, end_search, [trigger_node_id])
        if not history:
            return None, None

        node_entry = next(
            (item for item in history if item.get('nodeId') == trigger_node_id), None
        )
        if not node_entry or not node_entry.get('data'):
            return None, None

        points = node_entry['data']
        # 值为 1 的点视为脉冲激活点
        active_points = [p for p in points if str(p.get('v')) == '1' or p.get('v') == 1]
        if not active_points:
            return None, None

        # 按时间升序排序后再取首尾，避免乱序导致错误
        active_points.sort(key=lambda x: x.get('t', 0))

        start_ts = active_points[0]['t']
        end_ts = active_points[-1]['t']

        # 转为字符串以符合 API 规范（毫秒值字符串）
        return str(start_ts), str(end_ts)
