import time
import requests
import logging


class RTDBClient:
    """
    实时库写入客户端。
    - 支持 HTTP 超时
    - 支持失败重试（指数退避）
    - 返回详细结果，便于上层决定是否缓存重试
    """

    def __init__(self, base_url, token=None,
                 timeout=10, max_retries=3, retry_backoff=1.5):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.logger = logging.getLogger(__name__)

    def _headers(self):
        headers = {}
        if self.token:
            headers["Authorization"] = self.token
        headers["Content-Type"] = "application/json"
        return headers

    def write_realtime_data(self, data_list, strict_mode=False):
        """
        批量写入实时库。
        返回 dict：
            {
                "success": bool,         # 整体是否成功
                "code": int,             # 服务端返回码
                "message": str,          # 服务端消息
                "results": list[int],    # 每个测点写入状态码（可能为 None）
                "attempt": int           # 实际尝试次数
            }
        """
        result = {
            "success": False, "code": -1, "message": "",
            "results": None, "attempt": 0
        }

        if not data_list:
            result["success"] = True
            result["code"] = 0
            result["message"] = "empty payload"
            return result

        url = f"{self.base_url}/api/hsm-db-rtserver/v1/rtdata/node/write"
        payload = {
            "data": data_list,
            "strictMode": strict_mode
        }

        last_err = None
        for attempt in range(1, self.max_retries + 1):
            result["attempt"] = attempt
            try:
                self.logger.info(f"Writing {len(data_list)} points to RTDB (attempt {attempt})")
                response = requests.post(
                    url, json=payload, headers=self._headers(), timeout=self.timeout
                )
                if response.status_code != 200:
                    self.logger.error(
                        f"RTDB Write failed with status {response.status_code}: {response.text}"
                    )
                response.raise_for_status()

                body = response.json()
                code = body.get("code")
                result["code"] = code if code is not None else -1
                result["message"] = body.get("message", "")
                result["results"] = body.get("data")

                item_results = result["results"]
                item_success = (
                    item_results is None
                    or all(x == 0 for x in item_results)
                )

                if code == 0 and item_success:
                    result["success"] = True
                    self.logger.info(f"RTDB write successful (attempt {attempt})")
                    return result

                # 业务失败，重试
                self.logger.warning(
                    f"RTDB write business failure "
                    f"(code={code}, results={item_results}): {result['message']}"
                )
                last_err = f"code={code}, results={item_results}, message={result['message']}"

            except requests.exceptions.RequestException as e:
                self.logger.warning(f"RTDB write request error (attempt {attempt}): {e}")
                last_err = str(e)

            # 未成功，退避后重试
            if attempt < self.max_retries:
                backoff = self.retry_backoff ** attempt
                time.sleep(backoff)

        result["message"] = last_err or result["message"]
        self.logger.error(f"RTDB write gave up after {self.max_retries} attempts: {last_err}")
        return result
