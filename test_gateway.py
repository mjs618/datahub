
import requests
import logging
from auth_client import AuthClient

# Configuration
BASE_IP = "http://192.168.1.35"
APP_CODE = "data"
APP_SECRET = "123456"

# Node for test
NODES = ["10001:ICSSYS0001.AVGV"]
START = "2026-03-16 11:18:00"
END = "2026-03-16 12:18:00"

logging.basicConfig(level=logging.INFO)

def test_gateway_path():
    auth = AuthClient(BASE_IP, APP_CODE, APP_SECRET)
    token = auth.get_token()
    if not token:
        print("Login failed")
        return

    # Try different prefixes
    prefixes = [
        "/api/timing-svc/v1/history/findAll",
        "/api/gateway/timing-svc/v1/history/findAll",
        "/api/gateway/hsm-timing-svc/v1/history/findAll"
    ]
    
    headers = {
        "Authorization": token,
        "Content-Type": "application/json"
    }
    payload = {
        "startTime": START,
        "endTime": END,
        "nodeIds": NODES,
        "exactTime": True
    }

    for p in prefixes:
        url = BASE_IP + p
        try:
            print(f"\nTesting {url}...")
            r = requests.post(url, json=payload, headers=headers)
            print(f"Status: {r.status_code}")
            print(f"Body: {r.text[:200]}")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    test_gateway_path()
