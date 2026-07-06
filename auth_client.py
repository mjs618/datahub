
import time
import threading
import requests
import logging


class AuthClient:
    """
    应用 Token 客户端，支持缓存与自动刷新。
    线程安全：内部使用锁保护 Token 状态。
    """

    def __init__(self, base_url, app_code, app_secret,
                 timeout=10, refresh_margin=60, default_ttl=7200):
        self.base_url = base_url.rstrip('/')
        self.app_code = app_code
        self.app_secret = app_secret
        self.timeout = timeout
        # Token 提前刷新阈值（秒）
        self.refresh_margin = refresh_margin
        # 当服务端未返回过期时间时的默认有效期
        self.default_ttl = default_ttl
        self.logger = logging.getLogger(__name__)

        self._token = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def _request_token(self):
        """实际发起 HTTP 请求获取新 Token"""
        url = f"{self.base_url}/api/gateway/appSignIn"
        payload = {
            "code": self.app_code,
            "secret": self.app_secret
        }
        try:
            self.logger.info(f"正在尝试获取应用 Token (AppCode: {self.app_code})...")
            response = requests.post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            result = response.json()

            if str(result.get("code")) == "0" or result.get("message") == "success":
                token = result.get("data")
                # 尝试解析过期时间（部分服务端会返回 expire/expiresIn/expires_in）
                ttl = self.default_ttl
                for key in ("expiresIn", "expires_in", "expire", "ttl"):
                    if isinstance(result.get(key), (int, float)):
                        ttl = int(result[key])
                        break
                return token, ttl
            self.logger.error(f"获取 Token 失败! 响应内容: {result}")
            return None, 0
        except Exception as e:
            self.logger.error(f"Token 请求发生异常: {e}")
            return None, 0

    def get_token(self, force_refresh=False):
        """
        获取有效 Token。若缓存 Token 仍然有效则直接返回，否则自动刷新。
        force_refresh=True 时强制刷新。
        """
        with self._lock:
            now = time.time()
            if not force_refresh and self._token and now < (self._expires_at - self.refresh_margin):
                return self._token

            token, ttl = self._request_token()
            if not token:
                return None
            self._token = token
            self._expires_at = now + ttl
            self.logger.info(f"Token 获取成功，有效期 {ttl}s，将于 {ttl - self.refresh_margin}s 后刷新")
            return self._token

    def invalidate(self):
        """使当前 Token 失效，下次 get_token 时会重新获取"""
        with self._lock:
            self._token = None
            self._expires_at = 0.0
