
import requests

url = "http://192.168.1.35/api/timing-svc/v1/history/findAll"
try:
    # Try GET
    print("Testing GET...")
    r_get = requests.get(url)
    print(f"GET status: {r_get.status_code}")
    
    # Try POST with empty body
    print("\nTesting POST (empty body)...")
    r_post = requests.post(url)
    print(f"POST empty status: {r_post.status_code}")
    
    # Try POST with JSON
    print("\nTesting POST (JSON body)...")
    r_json = requests.post(url, json={})
    print(f"POST JSON status: {r_json.status_code}")

except Exception as e:
    print(f"Error: {e}")
