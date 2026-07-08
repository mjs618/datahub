"""
probe_opcua.py — OPC UA 连接诊断脚本

排查 "UAExpert 能连、asyncua 连不上" 的常见原因:
  1. 服务端要求安全策略 (SecurityPolicy != None) / 客户端证书
  2. 服务端要求用户名密码认证 (无 Anonymous token)
  3. Endpoint URL host 不匹配 (服务端返回的 EndpointUrl 与请求的 host 不一致)
  4. 握手超时 / 协议不兼容
  5. TCP 网络层不通

用法:
    python probe_opcua.py [opc.tcp://host:port]

默认连接 config_runtime.json 里的 OPCUA_URL。
"""
import asyncio
import json
import logging
import os
import socket
import sys
from urllib.parse import urlparse


def _default_url():
    cfg_path = os.path.join(os.path.dirname(__file__), "config_runtime.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f).get("OPCUA_URL", "opc.tcp://192.168.1.35:6810")
    except Exception:
        return "opc.tcp://192.168.1.35:6810"


URL = sys.argv[1] if len(sys.argv) > 1 else _default_url()

# 打开 DEBUG 日志看握手细节，但降低部分噪音
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
for noisy in (
    "asyncua.common.connection",
    "asyncua.client.client",
):
    logging.getLogger(noisy).setLevel(logging.INFO)

SEP = "=" * 70
print(SEP)
print("OPC UA 连接诊断")
print(f"目标 URL: {URL}")
print(SEP)

# ---------- [1] 环境信息 ----------
print("\n[1] 环境信息")
print(f"  Python : {sys.version.split()[0]}")
try:
    import asyncua
    print(f"  asyncua: {getattr(asyncua, '__version__', '未知(无 __version__)')}")
except ImportError as e:
    print(f"  asyncua 未安装: {e}")
    print("  请先安装: pip install 'asyncua>=1.0,<2.0'")
    sys.exit(1)

# ---------- [2] TCP 端口探测 ----------
print("\n[2] TCP 端口探测")
parsed = urlparse(URL)
host = parsed.hostname
port = parsed.port or 4840
print(f"  host={host}, port={port}")
try:
    s = socket.create_connection((host, port), timeout=5)
    s.close()
    print(f"  TCP 连通: OK ({host}:{port} 可达)")
except Exception as e:
    print(f"  TCP 连通: 失败 — {type(e).__name__}: {e}")
    print("  -> 网络层不通。asyncua 和 UAExpert 都会失败; 先查 IP/端口/防火墙/VPN/网段。")
    sys.exit(1)

# ---------- [3]/[4] asyncua 握手 ----------
from asyncua import Client


async def probe():
    # --- [3] GetEndpoints (不建会话) ---
    print("\n[3] asyncua GetEndpoints (仅查询, 不建会话)")
    client = Client(URL)
    endpoints = None
    try:
        endpoints = await asyncio.wait_for(client.get_endpoints(), timeout=20)
        print(f"  GetEndpoints 成功, 返回 {len(endpoints)} 个 endpoint:")
        for i, ep in enumerate(endpoints):
            print(f"  [{i}] EndpointUrl      : {ep.EndpointUrl}")
            print(f"      SecurityPolicyUri: {ep.SecurityPolicyUri}")
            print(f"      SecurityMode     : {ep.SecurityMode}")
            tds = ep.UserIdentityTokens or []
            for td in tds:
                pol = td.SecurityPolicyUri or "(继承 endpoint)"
                print(f"      UserToken        : {td.TokenType} (policy={pol})")
    except Exception as e:
        print(f"  GetEndpoints 失败: {type(e).__name__}: {e}")
        print("  -> 连 GetEndpoints 都失败, 多半是协议层/安全层不兼容, 见下方连接测试。")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    # --- [4] 默认连接 (匿名 + None) ---
    print("\n[4] asyncua 默认连接 (匿名 + SecurityPolicy=None)  <-- 与 opcua_client.py 现状一致")
    client2 = Client(URL)
    connected = False
    try:
        await asyncio.wait_for(client2.connect(), timeout=20)
        connected = True
        print("  [OK] 默认连接成功! 说明匿名 + None 可用, 问题不在这里。")
        try:
            ns = await client2.get_namespace_array()
            print(f"  Namespaces: {ns}")
        except Exception as e:
            print(f"  读取命名空间失败(连接是通的): {e}")
        try:
            node = client2.get_node("ns=2;s=Trigger")
            v = await node.read_value()
            print(f"  读取 ns=2;s=Trigger = {v!r}")
        except Exception as e:
            print(f"  读取 Trigger 失败(连接是通的, 节点层面问题): {e}")
    except Exception as e:
        print(f"  [FAIL] 默认连接失败: {type(e).__name__}: {e}")
        msg = str(e).lower()
        print("\n  ---- 诊断建议 ----")
        gave_advice = False
        if endpoints:
            has_none = any(
                str(getattr(ep, "SecurityPolicyUri", "")).endswith("None")
                for ep in endpoints
            )
            if not has_none:
                print("  * 服务端所有 endpoint 均不含 SecurityPolicy=None,")
                print("    而 asyncua 默认用 None —— 这是连不上的根因。")
                print("    UAExpert 能连是因为它选了带安全的策略并用了自己的证书。")
                print("    修复: opcua_client.py 里调用 client.set_security(...) 配置证书。")
                gave_advice = True
            tokens = []
            for ep in endpoints:
                for td in (ep.UserIdentityTokens or []):
                    tokens.append(str(td.TokenType))
            if tokens and not any("anonymous" in str(t).lower() for t in tokens):
                print("  * 服务端不支持匿名(无 Anonymous token), 需要用户名/密码。")
                print("    修复: client.set_user(...); client.set_password(...)")
                gave_advice = True
        if "timeout" in msg or "timed out" in msg:
            print("  * 握手超时。常见于国产 OPC UA 服务端响应慢, 或 endpoint host 不匹配。")
            print("    可尝试: client.set_timeout(30) 增大超时。")
            gave_advice = True
        if "certificate" in msg or "security" in msg or "policy" in msg:
            print("  * 证书/安全策略相关错误, 需要配置客户端证书。")
            gave_advice = True
        if "host" in msg or "endpoint" in msg:
            print("  * endpoint URL host 不匹配。服务端返回的 EndpointUrl 里的 host")
            print("    (可能是机器名/localhost) 与请求的 host 不一致。")
            gave_advice = True
        if not gave_advice:
            print("  * 未能自动归类。请把上方 DEBUG 日志(特别是 [3] 的 endpoint 列表)")
            print("    和本条完整异常贴出来, 进一步分析。")
    finally:
        if connected:
            try:
                await client2.disconnect()
            except Exception:
                pass

    print("\n" + SEP)
    print("诊断完成")
    print(SEP)


asyncio.run(probe())
