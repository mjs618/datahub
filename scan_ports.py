
import socket

target = "192.168.1.35"
ports = [80, 81, 443, 8080, 8081, 8082, 8083, 8084, 8085, 8088, 8888, 9000, 9001, 9999, 6810]

for port in ports:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    result = s.connect_ex((target, port))
    if result == 0:
        print(f"Port {port} is OPEN")
    s.close()
