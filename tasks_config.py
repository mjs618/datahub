"""
12 任务定义表（数据来源：通讯点(1).xlsx）。

每个任务由 Excel 中"指令行 + 反馈行"两行组成，统一结构：
  - 触发点 AC_<mod><n>      ：用户点"计算"按钮后置 1
  - 结束反馈 FC_<mod><n>    ：任务完成后置 1
  - 开始时间分量 YEAR/MON/DAY/HOUR/MIN/SEC_<mod><sn>  （sn = 01..04）
  - 结束时间分量 YEAR/MON/DAY/HOUR/MIN/SEC_<mod><en>  （en = 11..14）
  - 数据源（历史库 DCS 点）→ 回写目标（设计器点 u11）

OPC UA 节点 ID 现为占位形式 `ns=2;s=<点名>`。
待用户提供真实节点表后，仅需替换 _node(...) 的命名规则即可。
"""


# OPC UA 节点 ID 命名规则（占位）。提供真实节点表后修改此处一处即可生效。
OPCUA_NS = 2


def _node(point_name: str) -> str:
    """由点名构造 OPC UA nodeid（占位规则）。"""
    return f"ns={OPCUA_NS};s={point_name}"


def _components(mod: str, suffix: str) -> dict:
    """
    构造一组时间分量节点。
    mod: 模块前缀，如 "AGC"
    suffix: 两两位，如 "01"（开始）/ "11"（结束）
    返回: {year/mon/day/hour/min/sec -> nodeid}
    """
    return {
        "year": _node(f"YEAR_{mod}{suffix}"),
        "mon":  _node(f"MON_{mod}{suffix}"),
        "day":  _node(f"DAY_{mod}{suffix}"),
        "hour": _node(f"HOUR_{mod}{suffix}"),
        "min":  _node(f"MIN_{mod}{suffix}"),
        "sec":  _node(f"SEC_{mod}{suffix}"),
    }


# 时间分量的固定顺序（拼装 yyyy-MM-dd HH:mm:ss 用）
COMPONENT_ORDER = ("year", "mon", "day", "hour", "min", "sec")


def _task(mod, n, sn, en, source, desc,
          h_instr, tgt_instr, h_fb, tgt_fb):
    """
    组装单个任务定义。
    n : 触发/反馈后缀（如 "01"）
    sn: 开始时间分量后缀（如 "01"）
    en: 结束时间分量后缀（如 "11"）
    """
    return {
        "id": f"{mod}{n}",                 # 任务唯一 ID，如 "AGC01"
        "module": mod,
        "source": source,
        "desc": desc,
        "ac_node": _node(f"AC_{mod}{n}"),
        "fc_node": _node(f"FC_{mod}{n}"),
        "start_components": _components(mod, sn),
        "end_components": _components(mod, en),
        "points": [
            {"history_id": h_instr, "target_node": _node(tgt_instr)},
            {"history_id": h_fb,    "target_node": _node(tgt_fb)},
        ],
    }


# ============================ 12 个任务（与 Excel 严格对应） ============================
TASKS = [
    # AGC 模块
    _task("AGC", "01", "01", "11", "王快", "#1机组负荷指令",
          "AMI_JZ1_FHGD", "AGC17", "JZ1_AI49", "AGC18"),
    _task("AGC", "02", "02", "12", "王快", "#2机组负荷指令",
          "AMI_JZ2_FHGD", "AGC37", "JZ2_AI49", "AGC38"),
    _task("AGC", "03", "03", "13", "仿真", "仿真#1机组负荷指令",
          "AMI_JZ1_FHGD", "AGC57", "JZ1_AI49", "AGC58"),
    _task("AGC", "04", "04", "14", "仿真", "仿真#2机组负荷指令",
          "AMI_JZ2_FHGD", "AGC77", "JZ2_AI49", "AGC78"),

    # AVC 模块
    _task("AVC", "01", "01", "11", "王快", "#1AVC指令",
          "AMI_JZ1_WGGD", "AVC10", "JZ1_AI50", "AVC11"),
    _task("AVC", "02", "02", "12", "王快", "#2AVC指令",
          "AMI_JZ2_WGGD", "AVC29", "JZ2_AI50", "AVC30"),
    _task("AVC", "03", "03", "13", "仿真", "仿真#1AVC指令",
          "AMI_JZ1_WGGD", "AVC50", "JZ1_AI50", "AVC51"),
    _task("AVC", "04", "04", "14", "仿真", "仿真#2AVC指令",
          "AMI_JZ2_WGGD", "AVC69", "JZ2_AI50", "AVC70"),

    # 一次调频 PFC 模块
    _task("PFC", "01", "01", "11", "王快", "#1机组PFC负荷",
          "JZ1_AI49", "PFC01", "T1CKDLQ_F", "PFC02"),
    _task("PFC", "02", "02", "12", "王快", "#2机组PFC负荷",
          "JZ1_AI49", "PFC11", "T2CKDLQ_F", "PFC12"),
    _task("PFC", "03", "03", "13", "仿真", "仿真#1机组PFC负荷",
          "JZ1_AI49", "PFC21", "T1CKDLQ_F", "PFC22"),
    _task("PFC", "04", "04", "14", "仿真", "仿真#2机组PFC负荷",
          "JZ1_AI49", "PFC31", "T2CKDLQ_F", "PFC32"),
]


def task_by_id(task_id: str):
    """按任务 ID 查找任务定义，未找到返回 None。"""
    for t in TASKS:
        if t["id"] == task_id:
            return t
    return None


def all_ac_nodes() -> list:
    """返回所有任务的 AC 触发节点列表（供主循环批量读取）。"""
    return [t["ac_node"] for t in TASKS]


if __name__ == "__main__":
    # 自检：打印 12 个任务摘要
    print(f"共 {len(TASKS)} 个任务")
    for t in TASKS:
        print(f"  [{t['id']}] {t['source']}/{t['desc']}")
        print(f"      AC={t['ac_node']}  FC={t['fc_node']}")
        print(f"      开始分量示例: {t['start_components']['year']}")
        print(f"      结束分量示例: {t['end_components']['year']}")
        for p in t["points"]:
            print(f"      {p['history_id']} -> {p['target_node']}")
