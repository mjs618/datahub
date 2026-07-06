
import requests
import logging
from auth_client import AuthClient

# Configuration
BASE_IP = "http://192.168.1.35"
AUTH_URL = "http://192.168.1.35/api/gateway/appSignIn" # Auth still on 80
SERVICE_PORT = "8081"
APP_CODE = "data"
APP_SECRET = "123456"

NODES = ["10001:ICSSYS0001.AVGV"]
START = "2026-03-16 11:18:00"
END = "2026-03-16 12:18:00"

logging.basicConfig(level=logging.INFO)

def test_8081():
    # Get token from 80
    auth = AuthClient(BASE_IP, APP_CODE, APP_SECRET)
    token = auth.get_token()
    if not token:
        print("Login failed")
        return

    # Try history on 8081
    url = f"http://192.168.1.35:8081/api/timing-svc/v1/history/findAll"
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

    try:
        print(f"Testing {url}...")
        r = requests.post(url, json=payload, headers=headers)
        print(f"Status: {r.status_code}")
        print(f"Body: {r.text[:500]}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_8081()
