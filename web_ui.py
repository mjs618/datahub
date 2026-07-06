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
  nav { background: #fff; border-bottom: 1px solid #e0e0e0; display: flex; padding: 0 24px; }
  nav button { background: none; border: none; padding: 12px 20px; cursor: pointer; font-size: 14px; color: #666; border-bottom: 2px solid transparent; }
  nav button:hover { color: #1f2d3d; }
  nav button.active { color: #1f2d3d; border-bottom-color: #409eff; font-weight: 600; }
  main { padding: 24px; }
  .panel { display: none; background: #fff; border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); padding: 24px; }
  .panel.active { display: block; }
  .field { margin-bottom: 16px; }
  .field label { display: block; font-size: 13px; color: #606266; margin-bottom: 6px; font-weight: 600; }
  .field input[type=text], .field textarea { width: 100%; padding: 8px 10px; border: 1px solid #dcdfe6; border-radius: 4px; font-size: 13px; font-family: "Consolas", monospace; }
  .field textarea { min-height: 120px; resize: vertical; }
  .field .hint { font-size: 12px; color: #909399; margin-top: 4px; }
  .btn { display: inline-block; padding: 8px 20px; background: #409eff; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }
  .btn:hover { background: #66b1ff; }
  .btn:disabled { background: #a0cfff; cursor: not-allowed; }
  .btn-sm { padding: 4px 12px; font-size: 12px; }
  .btn-warn { background: #e6a23c; }
  .btn-warn:hover { background: #ebb563; }
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
  .file-drop { border: 2px dashed #dcdfe6; border-radius: 6px; padding: 32px; text-align: center; color: #909399; }
</style>
</head>
<body>
<header>
  <h1>Data Hub 控制台</h1>
</header>
<nav>
  <button class="tab-btn active" data-tab="config">配置</button>
  <button class="tab-btn" data-tab="nodes">节点浏览</button>
  <button class="tab-btn" data-tab="csv">CSV 导入</button>
  <button class="tab-btn" data-tab="sync">同步</button>
</nav>
<main>
  <!-- 配置 Tab -->
  <div id="tab-config" class="panel active">
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
    <button class="btn" onclick="saveConfig()">保存配置</button>
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
    <div class="field">
      <label>CSV 文件</label>
      <input type="file" id="csv-file" accept=".csv">
      <div class="hint">CSV 格式：每行两列 history_id,rtdb_id（首行可为表头）。history_id 加到 WATCH_LIST，同时建立 NODE_MAPPING。</div>
    </div>
    <button class="btn" onclick="importCsv()">导入</button>
    <div style="margin-top:16px;">
      <pre id="csv-result">导入结果将显示在这里</pre>
    </div>
  </div>

  <!-- 同步 Tab -->
  <div id="tab-sync" class="panel">
    <div class="field">
      <label>手动触发同步</label>
      <button class="btn" onclick="triggerSync()">立即同步</button>
      <span id="sync-trigger-msg" style="margin-left:12px;"></span>
    </div>
    <div class="field">
      <label>最近同步状态</label>
      <button class="btn btn-sm" onclick="loadSyncStatus()">刷新</button>
      <pre id="sync-status">点击刷新查看状态</pre>
    </div>
  </div>
</main>

<script>
// ---- Tab 切换 ----
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  });
});

function showMsg(elId, msg, ok) {
  const el = document.getElementById(elId);
  el.textContent = msg;
  el.style.color = ok ? '#67c23a' : '#f56c6c';
}

// ---- 配置 Tab ----
async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const cfg = await res.json();
    document.getElementById('cfg-trig-node-id').value = cfg.TRIG_NODE_ID || '';
    document.getElementById('cfg-trig-history-id').value = cfg.TRIG_HISTORY_ID || '';
    document.getElementById('cfg-watch-list').value = (cfg.WATCH_LIST || []).join('\\n');
    const mapping = cfg.NODE_MAPPING || {};
    document.getElementById('cfg-node-mapping').value = Object.keys(mapping)
      .map(k => k + '|' + mapping[k]).join('\\n');
  } catch (e) {
    alert('加载配置失败: ' + e.message);
  }
}

async function saveConfig() {
  const watchList = document.getElementById('cfg-watch-list').value
    .split('\\n').map(s => s.trim()).filter(s => s);
  const mappingText = document.getElementById('cfg-node-mapping').value;
  const nodeMapping = {};
  mappingText.split('\\n').forEach(line => {
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
    TRIG_NODE_ID: document.getElementById('cfg-trig-node-id').value.trim(),
    TRIG_HISTORY_ID: document.getElementById('cfg-trig-history-id').value.trim(),
    WATCH_LIST: watchList,
    NODE_MAPPING: nodeMapping
  };
  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || err.error || ('HTTP ' + res.status));
    }
    showMsg('cfg-msg', '保存成功', true);
  } catch (e) {
    alert('保存失败: ' + e.message);
  }
}

// ---- 节点浏览 Tab ----
let selectedNode = null;

async function browseNodes(nodeId) {
  const url = nodeId ? ('/api/opcua/nodes?node_id=' + encodeURIComponent(nodeId)) : '/api/opcua/nodes';
  const res = await fetch(url);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || err.error || ('HTTP ' + res.status));
  }
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
            if (children.length === 0) {
              li.classList.add('leaf');
            }
          } catch (err) {
            alert('加载子节点失败: ' + err.message);
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
    renderTree(nodes, document.getElementById('tree-root'));
  } catch (e) {
    document.getElementById('tree-root').innerHTML = '<li style="color:#f56c6c;">加载失败: ' + e.message + '</li>';
  }
}

function selectNode(node, labelEl) {
  selectedNode = node;
  document.querySelectorAll('.label.selected').forEach(el => el.classList.remove('selected'));
  if (labelEl) labelEl.classList.add('selected');
  const detail = document.getElementById('node-detail');
  let html = '';
  ['node_id', 'browse_name', 'display_name', 'node_class'].forEach(k => {
    html += '<div class="detail-row"><span class="k">' + k + '</span><div class="v">' + (node[k] != null ? node[k] : '') + '</div></div>';
  });
  html += '<div class="detail-row"><span class="k">value</span><div class="v">' + (node.value != null ? JSON.stringify(node.value) : '(不可读)') + '</div></div>';
  html += '<div style="margin-top:16px;"><button class="btn btn-sm btn-warn" onclick="setAsTrigger()">设为触发节点</button></div>';
  detail.innerHTML = html;
}

function setAsTrigger() {
  if (!selectedNode) return;
  document.getElementById('cfg-trig-node-id').value = selectedNode.node_id;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelector('.tab-btn[data-tab="config"]').classList.add('active');
  document.getElementById('tab-config').classList.add('active');
  showMsg('cfg-msg', '已填入节点 ID，请点击保存', true);
}

// ---- CSV 导入 Tab ----
async function importCsv() {
  const fileInput = document.getElementById('csv-file');
  if (!fileInput.files.length) {
    alert('请先选择 CSV 文件');
    return;
  }
  const file = fileInput.files[0];
  const formData = new FormData();
  formData.append('file', file);
  document.getElementById('csv-result').textContent = '导入中...';
  try {
    const res = await fetch('/api/import_csv', { method: 'POST', body: formData });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || err.error || ('HTTP ' + res.status));
    }
    const data = await res.json();
    document.getElementById('csv-result').textContent =
      '导入 ' + data.imported_count + ' 条\\n\\nWATCH_LIST:\\n' +
      JSON.stringify(data.WATCH_LIST, null, 2) + '\\n\\nNODE_MAPPING:\\n' +
      JSON.stringify(data.NODE_MAPPING, null, 2);
    loadConfig();
  } catch (e) {
    alert('导入失败: ' + e.message);
    document.getElementById('csv-result').textContent = '导入失败: ' + e.message;
  }
}

// ---- 同步 Tab ----
async function triggerSync() {
  try {
    const res = await fetch('/api/trigger_sync', { method: 'POST' });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || err.error || ('HTTP ' + res.status));
    }
    showMsg('sync-trigger-msg', '已触发', true);
    setTimeout(loadSyncStatus, 1000);
  } catch (e) {
    alert('触发失败: ' + e.message);
  }
}

async function loadSyncStatus() {
  try {
    const res = await fetch('/api/sync_status');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    document.getElementById('sync-status').textContent = JSON.stringify(data, null, 2);
  } catch (e) {
    document.getElementById('sync-status').textContent = '获取状态失败: ' + e.message;
  }
}

// ---- 初始化 ----
loadConfig();
loadTopNodes();
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

    def __init__(self, opcua_url, trigger_sync_callback, status_getter, port=8089):
        self.opcua_url = opcua_url
        self.trigger_sync_callback = trigger_sync_callback
        self.status_getter = status_getter
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
                    allowed = {"TRIG_NODE_ID", "TRIG_HISTORY_ID", "WATCH_LIST", "NODE_MAPPING"}
                    filtered = {k: v for k, v in updates.items() if k in allowed}
                    if not filtered:
                        self._send_json(400, {"error": "no valid fields", "detail": "no editable fields provided"})
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
