"""
浏览 OPC UA 服务器地址空间，打印根节点下的子节点，
以及包含 "AC" / "FC" / "YEAR" 关键字的节点，帮助确认真实节点 ID 命名规则。

用法：
    python explore_opcua.py [server_url]

默认 server_url = opc.tcp://192.168.1.35:6810
"""
import asyncio
import sys
from asyncua import Client


DEFAULT_URL = "opc.tcp://192.168.1.35:6810"
# 关键字过滤（不区分大小写）
KEYWORDS = ["AC", "FC", "YEAR", "MON", "DAY", "HOUR", "MIN", "SEC", "AGC", "AVC", "PFC"]
MAX_DEPTH = 3
MAX_CHILDREN_PER_NODE = 50


async def browse_node(client, node, depth=0, max_depth=MAX_DEPTH, hits=None):
    if hits is None:
        hits = []
    try:
        children = await node.get_children()
    except Exception as e:
        print(f"  {'  ' * depth}<browse error: {e}>")
        return hits

    for idx, child in enumerate(children):
        if idx >= MAX_CHILDREN_PER_NODE:
            print(f"  {'  ' * depth}... (truncated, {len(children) - idx} more children)")
            break
        try:
            bn = await child.read_browse_name()
            dn = await child.read_display_name()
            nid = child.nodeid
            nid_str = str(nid)
            label = f"{bn} | {dn} | {nid_str}"
            print(f"  {'  ' * depth}- {label}")

            # 关键字命中
            for kw in KEYWORDS:
                if kw.lower() in (bn.Name or "").lower() or kw.lower() in (dn.Text or "").lower():
                    hits.append(nid_str)
                    break

            if depth + 1 < max_depth:
                await browse_node(client, child, depth + 1, max_depth, hits)
        except Exception as e:
            print(f"  {'  ' * depth}<read error: {e}>")
    return hits


async def main(url):
    print(f"Connecting to {url} ...")
    client = Client(url=url)
    await client.connect()
    print("Connected. Browsing root folder...\n")

    root = client.get_root_node()
    objects = client.get_objects_node()

    print("=== Root node ===")
    print(f"  NodeId: {root.nodeid}")
    print(f"  BrowseName: {await root.read_browse_name()}")
    print(f"  DisplayName: {await root.read_display_name()}\n")

    print("=== Objects folder (depth=1) ===")
    hits = await browse_node(client, objects, depth=0, max_depth=1)

    print("\n=== Searching entire address space for keywords (depth=3) ===")
    hits = await browse_node(client, root, depth=0, max_depth=3)

    print(f"\n=== Keyword hits ({len(hits)}) ===")
    for h in hits:
        print(f"  {h}")

    await client.disconnect()
    print("\nDisconnected.")


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    asyncio.run(main(url))
