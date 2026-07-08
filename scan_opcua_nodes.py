"""
OPC UA 服务器节点扫描工具。

使用 asyncua 库浏览服务器地址空间，列出所有可用节点的 NodeID，
包括命名空间索引、标识符类型和标识符值。

用法：
    python scan_opcua_nodes.py [--url OPCUA_URL] [--output OUTPUT_FILE]

示例：
    python scan_opcua_nodes.py
    python scan_opcua_nodes.py --url opc.tcp://192.168.1.35:6810
    python scan_opcua_nodes.py --output nodes_list.txt
"""

import asyncio
import argparse
import logging
from datetime import datetime
from asyncua import Client

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def browse_server(url: str, output_file: str = None):
    """
    连接到 OPC UA 服务器并浏览其地址空间。

    Args:
        url: OPC UA 服务器 URL
        output_file: 输出文件路径（可选）
    """
    logger.info(f"Connecting to OPC UA server: {url}")

    nodes_info = []

    try:
        client = Client(url=url)
        await client.connect()

        logger.info("Connected successfully!")

        # 获取服务器信息
        server_node = client.get_server_node()
        nodes_info.append(f"\n{'='*80}")
        nodes_info.append(f"OPC UA Server Information")
        nodes_info.append(f"{'='*80}")
        nodes_info.append(f"Server URL: {url}")
        nodes_info.append(f"Server Name: {await server_node.read_display_name()}")
        nodes_info.append(f"Server State: {await server_node.read_browse_name()}")

        # 获取命名空间表
        ns_array = await client.get_namespace_array()
        nodes_info.append(f"\n{'='*80}")
        nodes_info.append(f"Namespace Table (索引 -> URI)")
        nodes_info.append(f"{'='*80}")
        for idx, uri in enumerate(ns_array):
            nodes_info.append(f"  ns={idx} -> {uri}")

        # 浏览地址空间
        nodes_info.append(f"\n{'='*80}")
        nodes_info.append(f"Address Space Browse")
        nodes_info.append(f"{'='*80}")

        # 从 Objects 节点开始浏览（通常是用户自定义节点的根节点）
        objects_node = client.get_objects_node()
        logger.info(f"Starting browse from Objects node (NodeID: {objects_node.nodeid})")

        # 递归浏览所有节点
        await browse_node_recursive(client, objects_node, nodes_info, depth=0, max_depth=5)

        # 输出结果
        result_text = "\n".join(nodes_info)

        if output_file:
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(result_text)
            logger.info(f"Results saved to: {output_file}")
        else:
            print(result_text)

        # 统计信息
        logger.info(f"\n{'='*80}")
        logger.info(f"Browse completed!")
        logger.info(f"Total nodes found: {len([l for l in nodes_info if 'NodeID:' in l])}")
        logger.info(f"{'='*80}")

    except Exception as e:
        logger.error(f"Error browsing server: {e}")
        raise
    finally:
        try:
            await client.disconnect()
            logger.info("Disconnected from server")
        except:
            pass


async def browse_node_recursive(client, node, nodes_info, depth, max_depth):
    """
    递归浏览节点及其子节点。

    Args:
        client: OPC UA 客户端
        node: 当前节点
        nodes_info: 节点信息列表
        depth: 当前深度
        max_depth: 最大浏览深度
    """
    if depth > max_depth:
        nodes_info.append(f"  {'  '*depth}[Max depth reached, skipping children]")
        return

    try:
        # 读取节点基本信息
        nodeid = node.nodeid
        browse_name = await node.read_browse_name()
        display_name = await node.read_display_name()

        # NodeID 格式化
        nodeid_str = format_nodeid(nodeid)

        # 记录节点信息
        indent = "  " * depth
        nodes_info.append(f"{indent}[{browse_name.Name}] NodeID: {nodeid_str} | Display: '{display_name.Text}'")

        # 特别标注我们关心的节点（AC_AGC、AC_AVC、AC_PFC等）
        if browse_name.Name.startswith(("AC_", "FC_", "YEAR_", "MON_", "DAY_", "HOUR_", "MIN_", "SEC_", "AGC", "AVC", "PFC")):
            nodes_info.append(f"{indent}  ★★ This is a task-related node!")

        # 浏览子节点
        try:
            children = await node.get_children()
            if children:
                for child in children:
                    await browse_node_recursive(client, child, nodes_info, depth + 1, max_depth)
        except Exception as e:
            nodes_info.append(f"{indent}  [Error reading children: {e}]")

    except Exception as e:
        nodes_info.append(f"{'  '*depth}[Error reading node: {e}]")


def format_nodeid(nodeid):
    """
    格式化 NodeID 为标准 OPC UA 格式。

    Args:
        nodeid: NodeID 对象

    Returns:
        格式化的 NodeID 字符串
    """
    ns = nodeid.NamespaceIndex
    identifier = nodeid.Identifier

    # 根据标识符类型格式化
    if nodeid.is_string():
        return f"ns={ns};s={identifier}"
    elif nodeid.is_numeric():
        return f"ns={ns};i={identifier}"
    elif nodeid.is_guid():
        return f"ns={ns};g={identifier}"
    elif nodeid.is_bytestring():
        return f"ns={ns};b={identifier}"
    else:
        return f"ns={ns};{identifier}"


def main():
    parser = argparse.ArgumentParser(
        description="Scan OPC UA server and list all node IDs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        "--url",
        default="opc.tcp://192.168.1.35:6810",
        help="OPC UA server URL (default: opc.tcp://192.168.1.35:6810)"
    )
    parser.add_argument(
        "--output",
        help="Output file path (optional, prints to console if not specified)"
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=5,
        help="Maximum browse depth (default: 5)"
    )

    args = parser.parse_args()

    # 运行扫描
    asyncio.run(browse_server(args.url, args.output))


if __name__ == "__main__":
    main()