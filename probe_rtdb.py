
import requests

base = "http://192.168.1.35:6543"
paths = [
    "/api/hsm-db-rtserver/v1/rtdata/node/write",
    "/api/gateway/hsm-db-rtserver/v1/rtdata/node/write"
]

for p in paths:
    url = base + p
    try:
        r = requests.post(url, json={})
        print(f"Path: {p} -> Status: {r.status_code}")
    except Exception as e:
        print(f"Path: {p} -> Error: {e}")
