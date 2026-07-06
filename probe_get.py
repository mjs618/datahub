
import requests

url = "http://192.168.1.35/api/timing-svc/v1/history/findAll"
try:
    r = requests.get(url)
    print(f"Status: {r.status_code}")
    print(f"Content: {r.text[:500]}")
except Exception as e:
    print(f"Error: {e}")
