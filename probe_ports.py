
import requests

ports = [8080, 8081, 8088, 9000, 9090]
path = "/api/timing-svc/v1/history/findAll"

for p in ports:
    url = f"http://192.168.1.35:{p}{path}"
    try:
        r = requests.post(url, timeout=2)
        print(f"Port {p} -> Status: {r.status_code}")
    except Exception as e:
        print(f"Port {p} -> Connection failed")
