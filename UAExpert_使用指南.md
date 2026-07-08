# UAExpert 使用指南 - 浏览 OPC UA 服务器节点

## 下载和安装 UAExpert

UAExpert 是一个免费的 OPC UA 客户端工具，由 Unified Automation 提供。

**下载地址**：https://www.unified-automation.com/downloads/opc-ua-clients.html

**安装步骤**：
1. 下载 UAExpert 安装程序（Windows版本）
2. 运行安装程序，按照提示完成安装
3. 启动 UAExpert

## 连接到 OPC UA 服务器

### 1. 添加服务器连接

1. 打开 UAExpert
2. 点击菜单 `Server` → `Add Server`
3. 在 Discovery URL 中输入服务器地址：
   ```
   opc.tcp://192.168.1.35:6810
   ```
4. 点击 `Find Servers` 按钮
5. 如果服务器在线，会显示在列表中，选择它并点击 `Add`

### 2. 连接服务器

1. 在左侧服务器列表中，双击刚才添加的服务器
2. 如果需要认证，输入用户名和密码（或选择 Anonymous）
3. 点击 `Connect` 按钮

### 3. 浏览地址空间

连接成功后，可以浏览服务器地址空间：

1. **查看命名空间表**：
   - 展开 `Server` → `NamespaceMetadata`
   - 查看 NamespaceArray，确认命名空间索引对应的 URI

2. **浏览 Objects 节点**（用户自定义节点通常在这里）：
   - 展开 `Objects` 节点
   - 查看所有子节点和属性

3. **查找特定节点**：
   - 使用搜索功能（Ctrl+F）查找节点名称
   - 或手动浏览查找我们关心的节点：
     - AC_AGC01、AC_AGC02、AC_AGC03、AC_AGC04
     - AC_AVC01、AC_AVC02、AC_AVC03、AC_AVC04
     - AC_PFC01、AC_PFC02、AC_PFC03、AC_PFC04
     - FC_AGC01 等反馈节点
     - YEAR_AGC01、MON_AGC01 等时间分量节点

## 查看节点详细信息

### NodeID 格式

在 UAExpert 中查看节点时，注意以下信息：

1. **NodeId 显示格式**：
   - 右键点击节点 → `Properties`
   - 查看 NodeId 字段，格式可能是：
     - `ns=2;s=AC_AGC01`（字符串标识符）
     - `ns=2;i=1001`（整数标识符）
     - `ns=2;g=UUID`（GUID标识符）

2. **命名空间索引**：
   - ns=0：OPC UA标准命名空间
   - ns=1：服务器命名空间
   - ns=2及以上：用户自定义命名空间（检查实际索引）

3. **标识符类型**：
   - **s=**：字符串标识符（String）
   - **i=**：整数标识符（Numeric）
   - **g=**：GUID标识符
   - **b=**：字节串标识符（ByteString）

## 导出节点列表

### 方法1：手动记录

1. 浏览到目标节点
2. 右键点击 → `Properties`
3. 记录 NodeId、BrowseName、DisplayName
4. 重复此步骤记录所有相关节点

### 方法2：使用 UAExpert 的导出功能

某些版本的 UAExpert 支持导出地址空间：
1. 选择根节点（如 Objects）
2. 右键 → `Export`（如果有此选项）
3. 选择导出格式（CSV、XML等）
4. 保存文件

## 验证节点存在性

### 检查清单

使用 UAExpert 确认以下节点是否存在：

**AGC模块（4个任务）**：
- [ ] AC_AGC01（触发点）
- [ ] FC_AGC01（反馈点）
- [ ] YEAR_AGC01, MON_AGC01, DAY_AGC01, HOUR_AGC01, MIN_AGC01, SEC_AGC01（开始时间）
- [ ] YEAR_AGC11, MON_AGC11, DAY_AGC11, HOUR_AGC11, MIN_AGC11, SEC_AGC11（结束时间）
- [ ] AGC17, AGC18（数据点）

重复检查 AGC02、AGC03、AGC04 的类似节点。

**AVC模块（4个任务）**：
- [ ] AC_AVC01
- [ ] FC_AVC01
- [ ] 时间分量节点和数据点

**PFC模块（4个任务）**：
- [ ] AC_PFC01
- [ ] FC_PFC01
- [ ] 时间分量节点和数据点

## 记录正确的 NodeID

将发现的正确 NodeID 格式记录下来，用于更新 tasks_config.py：

### 示例记录表

| 节点名称 | NodeID格式 | 命名空间索引 | 标识符类型 |
|---------|-----------|------------|----------|
| AC_AGC01 | ns=2;s=AC_AGC01 | 2 | 字符串 |
| FC_AGC01 | ns=2;i=1234 | 2 | 整数 |
| ... | ... | ... | ... |

## 下一步操作

1. 如果节点不存在：
   - 联系 OPC UA 服务器管理员，确认节点配置
   - 或修改 tasks_config.py 以匹配现有节点

2. 如果命名空间索引不匹配：
   - 修改 tasks_config.py 中的 OPCUA_NS 变量

3. 如果标识符类型不匹配：
   - 修改 tasks_config.py 中的 _node() 函数

## 常见问题

### Q: UAExpert 无法连接服务器

**检查**：
- 服务器IP和端口是否正确
- 网络是否可达（ping测试）
- 防火墙是否阻止了端口6810
- 服务器是否启动并运行

### Q: 连接成功但找不到节点

**可能原因**：
- 节点在错误的命名空间
- 节点标识符格式不同（整数 vs 字符串）
- 节点在非 Objects 根节点下

**解决**：
- 搜索 BrowseName 而不是依赖路径
- 检查所有命名空间
- 查看服务器文档或联系管理员

### Q: 发现节点但格式不同

**示例**：
- 预期：ns=2;s=AC_AGC01
- 实际：ns=3;i=1005

**解决**：
- 更新 tasks_config.py：
  ```python
  OPCUA_NS = 3  # 更新命名空间索引
  
  def _node(point_name: str) -> str:
      # 如果是整数标识符，需要建立映射表
      # 或使用不同的标识符格式
      return f"ns={OPCUA_NS};i={get_numeric_id(point_name)}"
  ```

## 参考资料

- UAExpert 官方文档：https://documentation.unified-automation.com/
- OPC UA 规范：https://opcfoundation.org/developer-tools/specifications-unified-architecture
- asyncua 文档：https://asyncua.readthedocs.io/