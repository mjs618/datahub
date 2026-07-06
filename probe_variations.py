
import requests

base = "http://192.168.1.35"
variations = [
    "/api/timing-svc/v1/history/findAll",
    "/api/hsm-timing-svc/v1/history/findAll",
    "/api/timing-svc/v1/history/findAll/",
    "/api/hsm-timing-svc/v1/history/findAll/",
    "/api/gateway/timing-svc/v1/history/findAll"
]

for v in variations:
    url = base + v
    try:
        r = requests.post(url, json={})
        print(f"Path: {v} -> Status: {r.status_code}")
    except Exception as e:
        print(f"Path: {v} -> Error: {e}")
