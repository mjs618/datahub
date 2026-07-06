
import requests

base = "http://192.168.1.35:8081"
variations = [
    "/api/timing-svc/v1/history/findAll",
    "/timing-svc/v1/history/findAll",
    "/api/hsm-timing-svc/v1/history/findAll",
    "/hsm-timing-svc/v1/history/findAll",
    "/api/hsm-db-rtserver/v1/rtdata/node/write",
    "/hsm-db-rtserver/v1/rtdata/node/write"
]

for v in variations:
    url = base + v
    try:
        r = requests.post(url, json={})
        print(f"Path: {v} -> Status: {r.status_code}")
    except Exception as e:
        print(f"Path: {v} -> Error: {e}")
