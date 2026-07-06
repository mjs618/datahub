"""
Data Hub Web UI 服务。

基于 Python 标准库 http.server 实现，无需任何第三方依赖（opcua 浏览按需临时新建 asyncua 客户端）。
提供配置编辑、OPC UA 节点浏览、CSV 批量导入、手动触发同步 4 个功能。

服务在后台 daemon 线程运行，与主程序解耦。
"""
import asyncio
import csv
import io
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from config import config, save_runtime_config, get_runtime_config_snapshot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _json_safe(val):
    """将 asyncua 读到的值转换为 JSON 可序列化形式。"""
    if val is None:
        return None
    if isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, bytes):
        try:
            return val.decode("utf-8")
        except Exception:
            return val.hex()
    try:
        json.dumps(val)
        return val
    except (TypeError, ValueError):
        return str(val)


async def _describe_node(node):
    """读取单个 OPC UA 节点的描述信息。"""
    try:
        # asyncua NodeId 对象有 to_string() 方法，返回标准 "ns=X;s=Y" / "i=X" 格式
        node_id_str = node.nodeid.to_string()
    except Exception:
        node_id_str = str(node.nodeid)
    info = {"node_id": node_id_str}
    try:
        bn = await node.read_browse_name()
        # QualifiedName 对象有 NamespaceIndex 和 Name 属性
        info["browse_name"] = bn.Name if hasattr(bn, "Name") else str(bn)
        info["browse_namespace"] = bn.NamespaceIndex if hasattr(bn, "NamespaceIndex") else 0
    except Exception:
        info["browse_name"] = ""
        info["browse_namespace"] = 0
    try:
        dn = await node.read_display_name()
        # LocalizedText 对象有 Text 属性
        info["display_name"] = dn.Text if hasattr(dn, "Text") else str(dn)
    except Exception:
        info["display_name"] = ""
    try:
        nc = await node.read_node_class()
        info["node_class"] = str(nc).split(".")[-1]
    except Exception:
        info["node_class"] = "?"
    try:
        val = await node.read_value()
        info["value"] = _json_safe(val)
    except Exception:
        info["value"] = None
    return info


async def _browse_nodes(opcua_url, node_id=None):
    """
    新建临时 asyncua 客户端浏览节点，浏览完断开。
    不传 node_id 时返回 Root/Objects 顶层节点；传 node_id 时返回该节点子节点列表。
    """
    from asyncua import Client

    client = Client(url=opcua_url)
    await client.connect()
    try:
        if not node_id:
            root = client.get_root_node()
            objects = client.get_objects_node()
            return [await _describe_node(root), await _describe_node(objects)]
        node = client.get_node(node_id)
        children = await node.get_children()
        result = []
        for child in children:
            result.append(await _describe_node(child))
        return result
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            logger.warning(f"Web UI temp client disconnect error (ignored): {e}")


def _parse_multipart(body: bytes, content_type: str):
    """
    手动解析 multipart/form-data 请求体。
    返回 dict: field_name -> (filename_or_None, content_bytes)。
    """
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("boundary="):
            boundary = part[len("boundary="):].strip().strip('"')
            break
    if not boundary:
        return {}

    delimiter = b"--" + boundary.encode("utf-8")
    segments = body.split(delimiter)
    fields = {}
    # segments[0] 是前导空串，segments[-1] 是 "--\r\n" 闭合
    for seg in segments[1:-1]:
        # 去除首尾 CRLF
        if seg.startswith(b"\r\n"):
            seg = seg[2:]
        if seg.endswith(b"\r\n"):
            seg = seg[:-2]
        if not seg:
            continue
        header_end = seg.find(b"\r\n\r\n")
        if header_end == -1:
            continue
        header_bytes = seg[:header_end].decode("utf-8", errors="replace")
        content = seg[header_end + 4:]
        name = None
        filename = None
        for line in header_bytes.split("\r\n"):
            if line.lower().startswith("content-disposition:"):
                for kv in line.split(";"):
                    kv = kv.strip()
                    if kv.startswith("name="):
                        name = kv[5:].strip().strip('"')
                    elif kv.startswith("filename="):
                        filename = kv[9:].strip().strip('"')
        if name:
            fields[name] = (filename, content)
    return fields


# ---------------------------------------------------------------------------
# 调优参数校验（Web UI 配置保存时调用）
# ---------------------------------------------------------------------------

# 校验失败时返回给前端的提示
_TUNE_VALIDATION_HINT = (
    "调优参数需为数值：POLL_INTERVAL (整数, 1~3600 秒), "
    "SETTLE_TIME (数值, 0~3600 秒), LOOKBACK_MINUTES (整数, 1~10080 分钟)"
)


def _validate_tuning(filtered):
    """
    校验 filtered 中携带的调优参数（就地转换类型），全部合法返回 True，否则 None。
    POLL_INTERVAL/LOOKBACK_MINUTES 必须为正整数；SETTLE_TIME 必须为非负数。
    """
    try:
        if "POLL_INTERVAL" in filtered:
            v = int(filtered["POLL_INTERVAL"])
            if not (1 <= v <= 3600):
                return None
            filtered["POLL_INTERVAL"] = v
        if "SETTLE_TIME" in filtered:
            v = float(filtered["SETTLE_TIME"])
            if not (0 <= v <= 3600):
                return None
            filtered["SETTLE_TIME"] = v
        if "LOOKBACK_MINUTES" in filtered:
            v = int(filtered["LOOKBACK_MINUTES"])
            if not (1 <= v <= 10080):
                return None
            filtered["LOOKBACK_MINUTES"] = v
    except (TypeError, ValueError):
        return None
    return True


# ---------------------------------------------------------------------------
# 前端 HTML（单页应用，全部内嵌）
# ---------------------------------------------------------------------------

HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Data Hub 控制台</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; background: #f4f6f9; color: #333; }
  header { background: #1f2d3d; color: #fff; padding: 12px 24px; display: flex; align-items: center; }
  header h1 { font-size: 18px; font-weight: 600; }
  header .sub { margin-left: 12px; font-size: 12px; color: #8492a6; }
  nav { background: #fff; border-bottom: 1px solid #e0e0e0; display: flex; padding: 0 24px; overflow-x: auto; }
  nav button { background: none; border: none; padding: 12px 20px; cursor: pointer; font-size: 14px; color: #666; border-bottom: 2px solid transparent; white-space: nowrap; }
  nav button:hover { color: #1f2d3d; }
  nav button.active { color: #1f2d3d; border-bottom-color: #409eff; font-weight: 600; }
  main { padding: 24px; }
  .panel { display: none; background: #fff; border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); padding: 24px; margin-bottom: 24px; }
  .panel.active { display: block; }
  .panel-title { font-size: 15px; font-weight: 600; color: #1f2d3d; margin-bottom: 16px; }
  .field { margin-bottom: 16px; }
  .field label { display: block; font-size: 13px; color: #606266; margin-bottom: 6px; font-weight: 600; }
  .field input[type=text], .field input[type=number], .field textarea { width: 100%; padding: 8px 10px; border: 1px solid #dcdfe6; border-radius: 4px; font-size: 13px; font-family: "Consolas", monospace; }
  .field input[type=number] { font-family: inherit; }
  .field textarea { min-height: 120px; resize: vertical; }
  .field .hint { font-size: 12px; color: #909399; margin-top: 4px; }
  .field-row { display: flex; gap: 16px; flex-wrap: wrap; }
  .field-row .field { flex: 1; min-width: 160px; margin-bottom: 16px; }
  .btn { display: inline-block; padding: 8px 20px; background: #409eff; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; transition: background .15s; }
  .btn:hover { background: #66b1ff; }
  .btn:disabled { background: #a0cfff; cursor: not-allowed; }
  .btn-sm { padding: 4px 12px; font-size: 12px; }
  .btn-warn { background: #e6a23c; }
  .btn-warn:hover { background: #ebb563; }
  .btn-danger { background: #f56c6c; }
  .btn-danger:hover { background: #f78989; }
  /* 加载态：按钮禁用 + spinner 点 */
  .btn.is-loading { position: relative; padding-left: 32px; cursor: progress; opacity: .8; }
  .btn.is-loading::before { content: ""; position: absolute; left: 12px; top: 50%; width: 12px; height: 12px; margin-top: -6px; border: 2px solid rgba(255,255,255,.4); border-top-color: #fff; border-radius: 50%; animation: spin .7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  pre { background: #f5f7fa; padding: 12px; border-radius: 4px; font-size: 12px; overflow-x: auto; white-space: pre-wrap; word-break: break-all; }
  #tree-container { width: 45%; float: left; border: 1px solid #e0e0e0; border-radius: 4px; padding: 12px; height: 480px; overflow: auto; }
  #node-detail { width: 50%; float: right; border: 1px solid #e0e0e0; border-radius: 4px; padding: 12px; height: 480px; overflow: auto; }
  .clearfix::after { content: ""; display: table; clear: both; }
  ul.tree { list-style: none; padding-left: 18px; }
  ul.tree.root { padding-left: 0; }
  .tree-node { margin: 2px 0; }
  .toggle { display: inline-block; width: 16px; cursor: pointer; user-select: none; color: #909399; }
  .leaf .toggle { visibility: hidden; }
  .label { cursor: pointer; padding: 2px 4px; border-radius: 3px; }
  .label:hover { background: #ecf5ff; }
  .label.selected { background: #409eff; color: #fff; }
  .detail-row { margin-bottom: 8px; }
  .detail-row .k { color: #909399; font-size: 12px; }
  .detail-row .v { font-family: "Consolas", monospace; word-break: break-all; }
  .status-badge { display: inline-block; padding: 2px 10px; border-radius: 10px; font-size: 12px; }
  .status-idle { background: #e0e0e0; color: #666; }
  .status-success { background: #f0f9eb; color: #67c23a; }
  .status-failure { background: #fef0f0; color: #f56c6c; }
  .file-drop { border: 2px dashed #dcdfe6; border-radius: 6px; padding: 32px; text-align: center; color: #909399; cursor: pointer; transition: border-color .15s, background .15s; }
  .file-drop:hover { border-color: #409eff; color: #409eff; }
  .file-drop.dragover { border-color: #409eff; background: #ecf5ff; color: #409eff; }
  .file-drop input[type=file] { display: none; }
  .file-name { margin-top: 8px; font-size: 12px; color: #606266; word-break: break-all; }
  /* 概览页卡片 */
  .ov-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 16px; }
  .ov-card { border: 1px solid #ebeef5; border-radius: 6px; padding: 16px; background: #fafbfc; }
  .ov-card .k { font-size: 12px; color: #909399; margin-bottom: 6px; }
  .ov-card .v { font-size: 20px; font-weight: 600; color: #1f2d3d; font-family: "Consolas", monospace; }
  .ov-card .v.big { font-size: 32px; }
  .ov-card .sub { font-size: 11px; color: #c0c4cc; margin-top: 4px; }
  .ov-section-title { font-size: 13px; font-weight: 600; color: #606266; margin: 20px 0 10px; }
  .ov-section-title:first-child { margin-top: 0; }
  .kv-list { display: grid; grid-template-columns: 140px 1fr; gap: 6px 12px; font-size: 13px; }
  .kv-list .k { color: #909399; }
  .kv-list .v { font-family: "Consolas", monospace; word-break: break-all; }
  .readonly-input { background: #f5f7fa; color: #909399; cursor: not-allowed; }
  .badge-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
  .badge-dot.ok { background: #67c23a; }
  .badge-dot.bad { background: #f56c6c; }
  .badge-dot.warn { background: #e6a23c; }
  /* Toast 容器 */
  #toast-container { position: fixed; top: 16px; right: 16px; z-index: 9999; display: flex; flex-direction: column; gap: 8px; max-width: 360px; }
  .toast { padding: 10px 16px; border-radius: 4px; color: #fff; font-size: 13px; box-shadow: 0 2px 12px rgba(0,0,0,0.15); opacity: 0; transform: translateX(20px); transition: opacity .25s, transform .25s; }
  .toast.show { opacity: 1; transform: translateX(0); }
  .toast.ok { background: #67c23a; }
  .toast.err { background: #f56c6c; }
  .toast.info { background: #909399; }
  /* 响应式：窄屏节点浏览改单列 */
  @media (max-width: 768px) {
    main { padding: 12px; }
    .panel { padding: 16px; }
    #tree-container, #node-detail { width: 100%; float: none; height: 320px; margin-bottom: 12px; }
    .field-row { flex-direction: column; }
    .kv-list { grid-template-columns: 110px 1fr; }
  }
</style>
</head>
<body>
<header>
  <h1>Data Hub 控制台</h1>
  <span class="sub" id="header-uptime"></span>
</header>
<nav>
  <button class="tab-btn active" data-tab="overview">概览</button>
  <button class="tab-btn" data-tab="config">配置</button>
  <button class="tab-btn" data-tab="nodes">节点浏览</button>
  <button class="tab-btn" data-tab="csv">CSV 导入</button>
  <button class="tab-btn" data-tab="sync">同步</button>
</nav>
<div id="toast-container"></div>
<main>
  <!-- 概览 Tab -->
  <div id="tab-overview" class="panel active">
    <div class="ov-section-title">连接状态</div>
    <div class="ov-grid">
      <div class="ov-card">
        <div class="k">OPC UA 连接</div>
        <div class="v" id="ov-opcua">—</div>
        <div class="sub" id="ov-opcua-sub"></div>
      </div>
      <div class="ov-card">
        <div class="k">Token 状态</div>
        <div class="v" id="ov-token">—</div>
      </div>
      <div class="ov-card">
        <div class="k">主循环</div>
        <div class="v" id="ov-loop">—</div>
        <div class="sub" id="ov-loop-sub"></div>
      </div>
      <div class="ov-card">
        <div class="k">失败写入缓存</div>
        <div class="v" id="ov-cache">—</div>
        <div class="sub">条待重试</div>
      </div>
    </div>

    <div class="ov-section-title">触发信号</div>
    <div class="ov-grid">
      <div class="ov-card">
        <div class="k">当前触发值</div>
        <div class="v big" id="ov-trig-val">—</div>
        <div class="sub">TRIG_NODE_ID 实时读取</div>
      </div>
      <div class="ov-card">
        <div class="k">触发次数（边沿）</div>
        <div class="v" id="ov-trig-count">—</div>
        <div class="sub">本次启动以来</div>
      </div>
    </div>

    <div class="ov-section-title">同步统计</div>
    <div class="ov-grid">
      <div class="ov-card">
        <div class="k">同步成功</div>
        <div class="v" style="color:#67c23a;" id="ov-sync-ok">—</div>
      </div>
      <div class="ov-card">
        <div class="k">同步失败</div>
        <div class="v" style="color:#f56c6c;" id="ov-sync-fail">—</div>
      </div>
      <div class="ov-card">
        <div class="k">最近同步</div>
        <div class="v" id="ov-sync-status">—</div>
        <div class="sub" id="ov-sync-msg"></div>
      </div>
      <div class="ov-card">
        <div class="k">最近同步时间</div>
        <div class="v" id="ov-sync-time">—</div>
        <div class="sub" id="ov-sync-ago"></div>
      </div>
    </div>

    <div class="ov-section-title">运行信息</div>
    <div class="kv-list">
      <span class="k">运行时长</span><span class="v" id="ov-uptime">—</span>
      <span class="k">启动时间</span><span class="v" id="ov-started">—</span>
      <span class="k">手动同步</span><span class="v" id="ov-manual">—</span>
      <span class="k">轮询间隔</span><span class="v" id="ov-poll">—</span>
    </div>
  </div>

  <!-- 配置 Tab -->
  <div id="tab-config" class="panel">
    <div class="panel-title">触发配置</div>
    <div class="field">
      <label>触发节点 ID (TRIG_NODE_ID)</label>
      <input type="text" id="cfg-trig-node-id">
      <div class="hint">OPC UA 节点 ID，如 ns=2;s=Trigger</div>
    </div>
    <div class="field">
      <label>触发历史点 ID (TRIG_HISTORY_ID)</label>
      <input type="text" id="cfg-trig-history-id">
      <div class="hint">历史库点 ID，如 10001:ICSSYS.Trigger</div>
    </div>
    <div class="field">
      <label>监听列表 (WATCH_LIST)</label>
      <textarea id="cfg-watch-list"></textarea>
      <div class="hint">每行一个历史点 ID，如 10001:ICSSYS0001.AVGV</div>
    </div>
    <div class="field">
      <label>节点映射 (NODE_MAPPING)</label>
      <textarea id="cfg-node-mapping"></textarea>
      <div class="hint">每行格式 key|value，从历史点 ID 映射到 RTDB 点 ID</div>
    </div>

    <div class="panel-title" style="margin-top:24px;">调优参数</div>
    <div class="hint" style="margin-bottom:12px;">保存后写入运行时配置；轮询间隔下个周期生效，其余参数下次同步/重启后生效。</div>
    <div class="field-row">
      <div class="field">
        <label>轮询间隔 POLL_INTERVAL (秒)</label>
        <input type="number" id="cfg-poll-interval" min="1" max="3600" step="1">
        <div class="hint">主循环读取触发信号的周期，1~3600</div>
      </div>
      <div class="field">
        <label>沉淀时间 SETTLE_TIME (秒)</label>
        <input type="number" id="cfg-settle-time" min="0" max="3600" step="0.5">
        <div class="hint">触发后等待历史数据落库的时长，0~3600</div>
      </div>
      <div class="field">
        <label>回溯窗口 LOOKBACK_MINUTES (分钟)</label>
        <input type="number" id="cfg-lookback-minutes" min="1" max="10080" step="1">
        <div class="hint">查询脉冲区间时向前回溯的分钟数，1~10080</div>
      </div>
    </div>

    <div class="panel-title" style="margin-top:24px;">连接信息（只读）</div>
    <div class="field-row">
      <div class="field">
        <label>BASE_IP</label>
        <input type="text" id="cfg-base-ip" class="readonly-input" readonly>
        <div class="hint">历史库 / 实时库服务地址，通过环境变量配置</div>
      </div>
      <div class="field">
        <label>OPCUA_URL</label>
        <input type="text" id="cfg-opcua-url" class="readonly-input" readonly>
        <div class="hint">OPC UA 服务地址，通过环境变量配置</div>
      </div>
    </div>

    <button class="btn" id="btn-save-config" onclick="saveConfig()">保存配置</button>
    <span id="cfg-msg" style="margin-left:12px;"></span>
  </div>

  <!-- 节点浏览 Tab -->
  <div id="tab-nodes" class="panel">
    <div class="clearfix">
      <div id="tree-container">
        <ul class="tree root" id="tree-root"></ul>
      </div>
      <div id="node-detail">
        <p style="color:#909399;">点击左侧节点查看详情</p>
      </div>
    </div>
  </div>

  <!-- CSV 导入 Tab -->
  <div id="tab-csv" class="panel">
    <div class="panel-title">CSV 批量导入</div>
    <div class="field">
      <label>CSV 文件</label>
      <label class="file-drop" id="csv-dropzone">
        <input type="file" id="csv-file" accept=".csv">
        <div>点击选择或拖拽 CSV 文件到此处</div>
        <div class="file-name" id="csv-filename"></div>
      </label>
      <div class="hint">CSV 格式：每行两列 history_id,rtdb_id（首行可为表头）。history_id 加到 WATCH_LIST，同时建立 NODE_MAPPING。</div>
    </div>
    <button class="btn" id="btn-import-csv" onclick="importCsv()">导入</button>
    <div style="margin-top:16px;">
      <pre id="csv-result">导入结果将显示在这里</pre>
    </div>
  </div>

  <!-- 同步 Tab -->
  <div id="tab-sync" class="panel">
    <div class="panel-title">手动触发同步</div>
    <div class="field">
      <button class="btn" id="btn-trigger-sync" onclick="triggerSync()">立即同步</button>
      <span id="sync-trigger-msg" style="margin-left:12px;"></span>
    </div>

    <div class="panel-title" style="margin-top:24px;">最近同步状态</div>
    <div class="field">
      <button class="btn btn-sm" onclick="loadSyncStatus()">刷新</button>
      <span style="margin-left:8px;font-size:12px;color:#909399;" id="sync-auto-hint"></span>
    </div>
    <div id="sync-status-cards" class="ov-grid" style="margin-bottom:16px;"></div>
    <pre id="sync-status">点击刷新查看状态</pre>
  </div>
</main>

<script>
// ==================== 工具函数 ====================
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function showMsg(elId, msg, ok) {
  const el = $(elId);
  if (!el) return;
  el.textContent = msg;
  el.style.color = ok ? '#67c23a' : '#f56c6c';
}

// Toast：替代所有 alert
function toast(msg, type = 'info', ms = 3000) {
  const box = $('toast-container');
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  box.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), 300);
  }, ms);
}

// 按钮 loading 态
function setLoading(btnId, loading, loadingText) {
  const btn = typeof btnId === 'string' ? $(btnId) : btnId;
  if (!btn) return;
  if (loading) {
    if (!btn.dataset._origText) btn.dataset._origText = btn.textContent;
    btn.classList.add('is-loading');
    btn.disabled = true;
    if (loadingText) btn.textContent = loadingText;
  } else {
    btn.classList.remove('is-loading');
    btn.disabled = false;
    if (btn.dataset._origText) btn.textContent = btn.dataset._origText;
  }
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  if (isNaN(d.getTime())) return '—';
  return d.toLocaleString('zh-CN', { hour12: false });
}
function fmtUptime(sec) {
  if (!sec || sec < 0) return '—';
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  let out = '';
  if (d) out += d + '天 ';
  if (h || d) out += h + '时 ';
  out += m + '分 ' + s + '秒';
  return out;
}
function badge(ok, okText, badText) {
  return '<span class="badge-dot ' + (ok ? 'ok' : 'bad') + '"></span>' + (ok ? okText : badText);
}

async function apiError(res) {
  const err = await res.json().catch(() => ({}));
  return new Error(err.detail || err.error || ('HTTP ' + res.status));
}

// ==================== Tab 切换 ====================
function switchTab(tab) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  $('tab-' + tab).classList.add('active');
}
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});

// ==================== 配置 Tab ====================
async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    if (!res.ok) throw await apiError(res);
    const cfg = await res.json();
    $('cfg-trig-node-id').value = cfg.TRIG_NODE_ID || '';
    $('cfg-trig-history-id').value = cfg.TRIG_HISTORY_ID || '';
    $('cfg-watch-list').value = (cfg.WATCH_LIST || []).join('\\n');
    const mapping = cfg.NODE_MAPPING || {};
    $('cfg-node-mapping').value = Object.keys(mapping).map(k => k + '|' + mapping[k]).join('\\n');
    $('cfg-poll-interval').value = cfg.POLL_INTERVAL != null ? cfg.POLL_INTERVAL : '';
    $('cfg-settle-time').value = cfg.SETTLE_TIME != null ? cfg.SETTLE_TIME : '';
    $('cfg-lookback-minutes').value = cfg.LOOKBACK_MINUTES != null ? cfg.LOOKBACK_MINUTES : '';
    $('cfg-base-ip').value = cfg.BASE_IP || '';
    $('cfg-opcua-url').value = cfg.OPCUA_URL || '';
  } catch (e) {
    toast('加载配置失败: ' + e.message, 'err');
  }
}

async function saveConfig() {
  const watchList = $('cfg-watch-list').value.split('\\n').map(s => s.trim()).filter(s => s);
  const nodeMapping = {};
  $('cfg-node-mapping').value.split('\\n').forEach(line => {
    line = line.trim();
    if (!line) return;
    const idx = line.indexOf('|');
    if (idx > 0) {
      const k = line.substring(0, idx).trim();
      const v = line.substring(idx + 1).trim();
      if (k) nodeMapping[k] = v;
    }
  });
  const payload = {
    TRIG_NODE_ID: $('cfg-trig-node-id').value.trim(),
    TRIG_HISTORY_ID: $('cfg-trig-history-id').value.trim(),
    WATCH_LIST: watchList,
    NODE_MAPPING: nodeMapping,
    POLL_INTERVAL: parseInt($('cfg-poll-interval').value, 10),
    SETTLE_TIME: parseFloat($('cfg-settle-time').value),
    LOOKBACK_MINUTES: parseInt($('cfg-lookback-minutes').value, 10),
  };
  setLoading('btn-save-config', true, '保存中...');
  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!res.ok) throw await apiError(res);
    await res.json();
    showMsg('cfg-msg', '保存成功', true);
    toast('配置已保存。调优参数下个周期 / 重启后生效', 'ok');
  } catch (e) {
    showMsg('cfg-msg', '保存失败', false);
    toast('保存失败: ' + e.message, 'err');
  } finally {
    setLoading('btn-save-config', false);
  }
}

// ==================== 节点浏览 Tab ====================
let selectedNode = null;

async function browseNodes(nodeId) {
  const url = nodeId ? ('/api/opcua/nodes?node_id=' + encodeURIComponent(nodeId)) : '/api/opcua/nodes';
  const res = await fetch(url);
  if (!res.ok) throw await apiError(res);
  const data = await res.json();
  return data.nodes || [];
}

function renderTree(nodes, container) {
  container.innerHTML = '';
  nodes.forEach(node => {
    const li = document.createElement('li');
    li.className = 'tree-node';
    const toggle = document.createElement('span');
    toggle.className = 'toggle';
    toggle.textContent = '\\u25B6';
    const label = document.createElement('span');
    label.className = 'label';
    const name = node.browse_name || node.display_name || node.node_id;
    label.textContent = name + '  [' + node.node_class + ']';
    label.title = node.node_id;
    label.addEventListener('click', () => selectNode(node, label));
    toggle.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (li.classList.contains('expanded')) {
        li.classList.remove('expanded');
        const ul = li.querySelector(':scope > ul');
        if (ul) ul.style.display = 'none';
        toggle.textContent = '\\u25B6';
      } else {
        if (!li.classList.contains('loaded')) {
          toggle.textContent = '\\u23F3';
          try {
            const children = await browseNodes(node.node_id);
            const ul = document.createElement('ul');
            ul.className = 'tree';
            renderTree(children, ul);
            li.appendChild(ul);
            li.classList.add('loaded');
            if (children.length === 0) li.classList.add('leaf');
          } catch (err) {
            toast('加载子节点失败: ' + err.message, 'err');
            toggle.textContent = '\\u25B6';
            return;
          }
        }
        const ul = li.querySelector(':scope > ul');
        if (ul) ul.style.display = '';
        li.classList.add('expanded');
        toggle.textContent = '\\u25BC';
      }
    });
    li.appendChild(toggle);
    li.appendChild(label);
    container.appendChild(li);
  });
}

async function loadTopNodes() {
  try {
    const nodes = await browseNodes(null);
    renderTree(nodes, $('tree-root'));
  } catch (e) {
    $('tree-root').innerHTML = '<li style="color:#f56c6c;">加载失败: ' + esc(e.message) + '</li>';
  }
}

function selectNode(node, labelEl) {
  selectedNode = node;
  document.querySelectorAll('.label.selected').forEach(el => el.classList.remove('selected'));
  if (labelEl) labelEl.classList.add('selected');
  const detail = $('node-detail');
  let html = '';
  ['node_id', 'browse_name', 'display_name', 'node_class'].forEach(k => {
    html += '<div class="detail-row"><span class="k">' + k + '</span><div class="v">' + esc(node[k]) + '</div></div>';
  });
  html += '<div class="detail-row"><span class="k">value</span><div class="v">' + (node.value != null ? esc(JSON.stringify(node.value)) : '(不可读)') + '</div></div>';
  html += '<div style="margin-top:16px; display:flex; gap:8px; flex-wrap:wrap;">';
  html += '<button class="btn btn-sm btn-warn" onclick="setAsTrigger()">设为触发节点</button>';
  html += '<button class="btn btn-sm" onclick="copyNodeId()">复制节点 ID</button>';
  html += '</div>';
  detail.innerHTML = html;
}

function setAsTrigger() {
  if (!selectedNode) return;
  $('cfg-trig-node-id').value = selectedNode.node_id;
  switchTab('config');
  showMsg('cfg-msg', '已填入节点 ID，请点击保存', true);
  toast('节点 ID 已填入触发配置', 'ok');
}

function copyNodeId() {
  if (!selectedNode) return;
  const text = selectedNode.node_id;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      () => toast('节点 ID 已复制: ' + text, 'ok'),
      () => toast('复制失败，请手动选择文本', 'err')
    );
  } else {
    toast('浏览器不支持剪贴板，节点 ID: ' + text, 'info', 5000);
  }
}

// ==================== CSV 导入 Tab ====================
function initCsvDropzone() {
  const dz = $('csv-dropzone');
  const input = $('csv-file');
  const nameEl = $('csv-filename');
  input.addEventListener('change', () => {
    nameEl.textContent = input.files.length ? input.files[0].name : '';
  });
  ['dragenter', 'dragover'].forEach(ev => {
    dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add('dragover'); });
  });
  ['dragleave', 'drop'].forEach(ev => {
    dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove('dragover'); });
  });
  dz.addEventListener('drop', e => {
    if (e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      nameEl.textContent = e.dataTransfer.files[0].name;
    }
  });
}

async function importCsv() {
  const input = $('csv-file');
  if (!input.files.length) { toast('请先选择 CSV 文件', 'err'); return; }
  const formData = new FormData();
  formData.append('file', input.files[0]);
  $('csv-result').textContent = '导入中...';
  setLoading('btn-import-csv', true, '导入中...');
  try {
    const res = await fetch('/api/import_csv', { method: 'POST', body: formData });
    if (!res.ok) throw await apiError(res);
    const data = await res.json();
    $('csv-result').textContent =
      '导入 ' + data.imported_count + ' 条\\n\\nWATCH_LIST:\\n' +
      JSON.stringify(data.WATCH_LIST, null, 2) + '\\n\\nNODE_MAPPING:\\n' +
      JSON.stringify(data.NODE_MAPPING, null, 2);
    toast('导入成功，共 ' + data.imported_count + ' 条', 'ok');
    loadConfig();
  } catch (e) {
    $('csv-result').textContent = '导入失败: ' + e.message;
    toast('导入失败: ' + e.message, 'err');
  } finally {
    setLoading('btn-import-csv', false);
  }
}

// ==================== 同步 Tab ====================
let syncPollTimer = null;

async function triggerSync() {
  setLoading('btn-trigger-sync', true, '触发中...');
  try {
    const res = await fetch('/api/trigger_sync', { method: 'POST' });
    if (!res.ok) throw await apiError(res);
    showMsg('sync-trigger-msg', '已触发', true);
    toast('同步已触发', 'ok');
    pollSyncStatus();
  } catch (e) {
    showMsg('sync-trigger-msg', '触发失败', false);
    toast('触发失败: ' + e.message, 'err');
  } finally {
    setLoading('btn-trigger-sync', false);
  }
}

// 轮询同步状态直到 pending=false（最长 ~60s）
function pollSyncStatus() {
  if (syncPollTimer) clearInterval(syncPollTimer);
  $('sync-auto-hint').textContent = '自动刷新中...';
  let rounds = 0;
  syncPollTimer = setInterval(async () => {
    rounds++;
    let pending = true;
    try {
      const res = await fetch('/api/sync_status');
      if (res.ok) { const d = await res.json(); pending = !!d.pending; renderSyncStatus(d); }
    } catch (e) { /* 忽略单次失败 */ }
    if (!pending || rounds > 30) {
      clearInterval(syncPollTimer);
      syncPollTimer = null;
      $('sync-auto-hint').textContent = '';
    }
  }, 2000);
}

function renderSyncStatus(data) {
  const cards = $('sync-status-cards');
  const lr = data.last_result || {};
  let statusText = 'idle', statusType = 'warn';
  if (lr.status === 'completed') { statusText = '完成'; statusType = 'ok'; }
  else if (lr.status === 'failed') { statusText = '失败'; statusType = 'bad'; }
  cards.innerHTML =
    '<div class="ov-card"><div class="k">手动同步</div><div class="v">' +
      (data.pending ? '<span class="badge-dot warn"></span>进行中' : '<span class="badge-dot ok"></span>空闲') +
    '</div></div>' +
    '<div class="ov-card"><div class="k">结果</div><div class="v">' +
      '<span class="badge-dot ' + statusType + '"></span>' + statusText +
    '</div></div>' +
    '<div class="ov-card"><div class="k">完成时间</div><div class="v">' +
      (lr.timestamp ? fmtTime(lr.timestamp) : '—') +
    '</div></div>' +
    (lr.error ? '<div class="ov-card"><div class="k">错误</div><div class="v" style="color:#f56c6c;">' + esc(lr.error) + '</div></div>' : '');
  $('sync-status').textContent = JSON.stringify(data, null, 2);
}

async function loadSyncStatus() {
  try {
    const res = await fetch('/api/sync_status');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    renderSyncStatus(await res.json());
  } catch (e) {
    $('sync-status').textContent = '获取状态失败: ' + e.message;
  }
}

// ==================== 概览 Tab（自动刷新） ====================
let overviewTimer = null;

async function refreshOverview() {
  let m = null, ts = null;
  try {
    const [mr, tr] = await Promise.all([
      fetch('/api/metrics').then(r => r.ok ? r.json() : null).catch(() => null),
      fetch('/api/trigger_state').then(r => r.ok ? r.json() : null).catch(() => null)
    ]);
    m = mr; ts = tr;
  } catch (e) { return; }

  if (ts) {
    const v = ts.trigger_value;
    $('ov-trig-val').textContent = (v === true ? '1' : v === false ? '0' : '—');
    $('ov-opcua').innerHTML = badge(ts.opcua_connected, '已连接', '未连接');
    $('ov-opcua-sub').textContent = '';
  }
  if (!m) return;

  const now = Date.now() / 1000;
  $('ov-token').innerHTML = badge(m.token_valid, '有效', '无效');
  $('ov-cache').textContent = m.failed_cache_size != null ? m.failed_cache_size : '—';
  $('ov-trig-count').textContent = m.trigger_count != null ? m.trigger_count : '—';
  $('ov-sync-ok').textContent = m.sync_success_count != null ? m.sync_success_count : '—';
  $('ov-sync-fail').textContent = m.sync_failure_count != null ? m.sync_failure_count : '—';

  // 主循环活跃度：距离上次循环的时间
  if (m.last_loop_at) {
    const ago = Math.max(0, Math.round(now - m.last_loop_at));
    const healthy = ago < 60;
    $('ov-loop').innerHTML = badge(healthy, '活跃', '停滞');
    $('ov-loop-sub').textContent = ago + ' 秒前';
  } else {
    $('ov-loop').innerHTML = badge(false, '—', '未运行');
    $('ov-loop-sub').textContent = '';
  }

  // 最近同步状态
  const statusMap = { success: ['成功', 'ok'], failure: ['失败', 'bad'], idle: ['空闲', 'warn'] };
  const s = statusMap[m.last_sync_status] || ['未知', 'warn'];
  $('ov-sync-status').innerHTML = '<span class="badge-dot ' + s[1] + '"></span>' + s[0];
  $('ov-sync-msg').textContent = m.last_sync_message || '';
  $('ov-sync-time').textContent = fmtTime(m.last_sync_at);
  if (m.last_sync_at) {
    const ago = Math.max(0, Math.round(now - m.last_sync_at));
    $('ov-sync-ago').textContent = fmtUptime(ago) + ' 前';
  } else {
    $('ov-sync-ago').textContent = '';
  }

  $('ov-uptime').textContent = fmtUptime(m.uptime_seconds);
  $('header-uptime').textContent = m.uptime_seconds ? '运行 ' + fmtUptime(m.uptime_seconds) : '';
  $('ov-started').textContent = fmtTime(m.started_at);

  // 手动同步状态合并展示
  try {
    const sr = await fetch('/api/sync_status');
    if (sr.ok) {
      const sd = await sr.json();
      $('ov-manual').innerHTML = sd.pending
        ? '<span class="badge-dot warn"></span>进行中'
        : (sd.last_result ? '<span class="badge-dot ok"></span>' + (sd.last_result.status === 'completed' ? '完成' : '失败') : '空闲');
    }
  } catch (e) { /* 忽略 */ }
}

function startOverviewRefresh() {
  refreshOverview();
  if (overviewTimer) clearInterval(overviewTimer);
  overviewTimer = setInterval(refreshOverview, 2000);
}

// 页面可见性变化：切到后台暂停轮询，回到前台立即刷新
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    if (overviewTimer) { clearInterval(overviewTimer); overviewTimer = null; }
  } else {
    startOverviewRefresh();
  }
});

// ==================== 初始化 ====================
loadConfig();
loadTopNodes();
initCsvDropzone();
startOverviewRefresh();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Web UI 服务器
# ---------------------------------------------------------------------------

class WebUIServer:
    """
    基于 http.server 的 Web UI 服务。

    在后台 daemon 线程运行，提供配置编辑、节点浏览、CSV 导入、同步触发功能。
    """

    def __init__(self, opcua_url, trigger_sync_callback, status_getter,
                 metrics_getter=None, trigger_state_getter=None, port=8089):
        self.opcua_url = opcua_url
        self.trigger_sync_callback = trigger_sync_callback
        self.status_getter = status_getter
        self.metrics_getter = metrics_getter
        self.trigger_state_getter = trigger_state_getter
        self.port = int(os.getenv("WEB_UI_PORT", port))
        self._server = None
        self._thread = None

    def start(self):
        server_self = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                # 静默默认访问日志
                pass

            def _send_json(self, code, body):
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)

            def _send_html(self, html):
                body_bytes = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)

            def _read_body(self):
                length = int(self.headers.get("Content-Length", 0))
                if length > 0:
                    return self.rfile.read(length)
                return b""

            # ---- GET 路由 ----
            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path

                if path == "/" or path == "/index.html":
                    self._send_html(HTML_PAGE)
                    return

                if path == "/api/config":
                    try:
                        self._send_json(200, get_runtime_config_snapshot())
                    except Exception as e:
                        self._send_json(500, {"error": "get config failed", "detail": str(e)})
                    return

                if path == "/api/opcua/nodes":
                    qs = parse_qs(parsed.query)
                    node_id = qs.get("node_id", [None])[0]
                    try:
                        result = asyncio.run(_browse_nodes(server_self.opcua_url, node_id))
                        self._send_json(200, {"nodes": result})
                    except Exception as e:
                        self._send_json(503, {"error": "opcua browse failed", "detail": str(e)})
                    return

                if path == "/api/sync_status":
                    try:
                        status = server_self.status_getter()
                        self._send_json(200, status if status is not None else {"status": "idle"})
                    except Exception as e:
                        self._send_json(500, {"error": "get status failed", "detail": str(e)})
                    return

                if path == "/api/metrics":
                    try:
                        if server_self.metrics_getter:
                            self._send_json(200, server_self.metrics_getter())
                        else:
                            self._send_json(200, {})
                    except Exception as e:
                        self._send_json(500, {"error": "get metrics failed", "detail": str(e)})
                    return

                if path == "/api/trigger_state":
                    try:
                        if server_self.trigger_state_getter:
                            self._send_json(200, server_self.trigger_state_getter())
                        else:
                            self._send_json(200, {})
                    except Exception as e:
                        self._send_json(500, {"error": "get trigger state failed", "detail": str(e)})
                    return

                self._send_json(404, {"error": "not found", "detail": f"unknown path: {path}"})

            # ---- POST 路由 ----
            def do_POST(self):
                parsed = urlparse(self.path)
                path = parsed.path

                if path == "/api/config":
                    self._handle_post_config()
                    return

                if path == "/api/import_csv":
                    self._handle_import_csv()
                    return

                if path == "/api/trigger_sync":
                    self._handle_trigger_sync()
                    return

                self._send_json(404, {"error": "not found", "detail": f"unknown path: {path}"})

            # ---- 配置保存 ----
            def _handle_post_config(self):
                try:
                    body = self._read_body()
                    updates = json.loads(body.decode("utf-8"))
                    if not isinstance(updates, dict):
                        self._send_json(400, {"error": "invalid body", "detail": "expected JSON object"})
                        return
                    allowed = {
                        "TRIG_NODE_ID", "TRIG_HISTORY_ID", "WATCH_LIST", "NODE_MAPPING",
                        "POLL_INTERVAL", "SETTLE_TIME", "LOOKBACK_MINUTES",
                    }
                    filtered = {k: v for k, v in updates.items() if k in allowed}
                    if not filtered:
                        self._send_json(400, {"error": "no valid fields", "detail": "no editable fields provided"})
                        return
                    # 调优参数类型与范围校验
                    tuned = _validate_tuning(filtered)
                    if tuned is None:
                        self._send_json(400, {"error": "invalid tuning", "detail": _TUNE_VALIDATION_HINT})
                        return
                    save_runtime_config(filtered)
                    self._send_json(200, get_runtime_config_snapshot())
                except json.JSONDecodeError as e:
                    self._send_json(400, {"error": "invalid json", "detail": str(e)})
                except Exception as e:
                    self._send_json(500, {"error": "save failed", "detail": str(e)})

            # ---- CSV 导入 ----
            def _handle_import_csv(self):
                try:
                    content_type = self.headers.get("Content-Type", "")
                    if "multipart/form-data" not in content_type:
                        self._send_json(400, {"error": "invalid content type", "detail": "expected multipart/form-data"})
                        return
                    body = self._read_body()
                    fields = _parse_multipart(body, content_type)
                    if "file" not in fields:
                        self._send_json(400, {"error": "no file", "detail": "form field 'file' not found"})
                        return
                    _, content = fields["file"]
                    text = content.decode("utf-8-sig")
                    reader = csv.reader(io.StringIO(text))
                    rows = list(reader)
                    if not rows:
                        self._send_json(400, {"error": "empty csv", "detail": "CSV file has no rows"})
                        return

                    # 首行若为表头则跳过
                    start = 0
                    if rows[0] and len(rows[0]) >= 2 and str(rows[0][0]).strip().lower() == "history_id":
                        start = 1

                    # 合并到现有配置
                    snap = get_runtime_config_snapshot()
                    watch_list = list(snap.get("WATCH_LIST", []) or [])
                    node_mapping = dict(snap.get("NODE_MAPPING", {}) or {})
                    imported = 0
                    for row in rows[start:]:
                        if not row or len(row) < 2:
                            continue
                        hid = row[0].strip()
                        rid = row[1].strip()
                        if not hid or not rid:
                            continue
                        if hid not in watch_list:
                            watch_list.append(hid)
                        node_mapping[hid] = rid
                        imported += 1

                    save_runtime_config({
                        "WATCH_LIST": watch_list,
                        "NODE_MAPPING": node_mapping,
                    })
                    self._send_json(200, {
                        "imported_count": imported,
                        "WATCH_LIST": watch_list,
                        "NODE_MAPPING": node_mapping,
                    })
                except Exception as e:
                    self._send_json(500, {"error": "import failed", "detail": str(e)})

            # ---- 触发同步 ----
            def _handle_trigger_sync(self):
                try:
                    server_self.trigger_sync_callback()
                    self._send_json(200, {"status": "triggered"})
                except Exception as e:
                    self._send_json(500, {"error": "trigger failed", "detail": str(e)})

        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="WebUIServer", daemon=True
        )
        self._thread.start()
        logger.info(f"Web UI listening on :{self.port}")

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Web UI stopped")
