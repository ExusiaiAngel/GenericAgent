# Network Monitor SOP

## 触发词

- "网络监控"
- "检查网络"
- "network check"
- "网络状态"

## 目的

对当前 Windows 开发环境的网络栈执行完整巡检，覆盖网络接口状态、路由表、DNS 解析、外网连通性、监听端口和代理配置六大检查区，输出结构化的网络监控报告并给出网络健康评分与 SPOF 风险标注。

本 SOP 定位为 **完整网络层诊断**：每个检查区给出独立状态标记，总报告包含 DNS 冗余度、连通性矩阵和代理链路状态，以 🟢🟡🔴 表情符号标注状态等级。

## 前置条件

1. 已读取 `memory/sandbox_policy.md` — 确认写入边界
2. Windows PowerShell 环境可用（管理员权限可获取更完整信息，但不是必须）
3. 本 SOP 仅执行只读网络探测，不修改任何配置

## 输入

无 — 使用默认 GenericAgent 工作区，自动检测网络状态。可选 `{target_hosts}` 参数指定额外连通性检查目标。

## 步骤

### Step 1: 一次性数据采集（批量 PowerShell）

将所有六个检查区的命令合并为一个脚本执行，减少 round-trip：

```powershell
Write-Output "=== INTERFACES ==="
Write-Output "--- ipconfig ---"
ipconfig 2>&1
Write-Output "--- interface count ---"
(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object Status -eq 'Up').Count
Write-Output "--- default interface ---"
Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object Status -eq 'Up' | Select-Object Name, LinkSpeed

Write-Output ""
Write-Output "=== ROUTING ==="
Write-Output "--- default route ---"
Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Select-Object DestinationPrefix, NextHop, InterfaceIndex, RouteMetric
Write-Output "--- full routing table ---"
Get-NetRoute -ErrorAction SilentlyContinue | Select-Object DestinationPrefix, NextHop, InterfaceAlias | Format-Table -AutoSize
Write-Output "--- gateway reachable ---"
$gateway = (Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Select-Object -First 1).NextHop
if ($gateway) {
    Test-Connection -ComputerName $gateway -Count 2 -Quiet
    Write-Output "Gateway: $gateway reachable"
} else {
    Write-Output "WARNING: no default gateway found"
}

Write-Output ""
Write-Output "=== DNS ==="
Write-Output "--- DNS server addresses ---"
Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | 
    Where-Object { $_.ServerAddresses } | Select-Object InterfaceAlias, ServerAddresses
Write-Output "--- DNS server count ---"
$dnsServers = Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | 
    ForEach-Object { $_.ServerAddresses } | Sort-Object -Unique
Write-Output "$($dnsServers.Count) unique DNS servers: $($dnsServers -join ', ')"
Write-Output "--- DNS resolution (github.com) ---"
Resolve-DnsName github.com -Type A -ErrorAction SilentlyContinue | Select-Object Name, IPAddress | Format-Table -AutoSize
if (-not $?) { Write-Output "(DNS resolution failed)" }
Write-Output "--- DNS resolution (pypi.org) ---"
Resolve-DnsName pypi.org -Type A -ErrorAction SilentlyContinue | Select-Object Name, IPAddress | Format-Table -AutoSize
if (-not $?) { Write-Output "(DNS resolution failed)" }

Write-Output ""
Write-Output "=== CONNECTIVITY ==="
Write-Output "--- pypi.org HTTP ---"
try {
    $r = Invoke-WebRequest -Uri https://pypi.org -Method Head -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
    Write-Output "pypi.org: $($r.StatusCode) ($($r.ContentLength) bytes)"
} catch { Write-Output "pypi.org: FAILED ($($_.Exception.Message))" }
Write-Output "--- github.com HTTP ---"
try {
    $r = Invoke-WebRequest -Uri https://github.com -Method Head -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
    Write-Output "github.com: $($r.StatusCode)"
} catch { Write-Output "github.com: FAILED ($($_.Exception.Message))" }
Write-Output "--- google.com HTTP ---"
try {
    $r = Invoke-WebRequest -Uri https://google.com -Method Head -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
    Write-Output "google.com: $($r.StatusCode)"
} catch { Write-Output "google.com: UNREACHABLE" }
Write-Output "--- IPv4 connectivity ---"
Test-Connection -ComputerName 8.8.8.8 -Count 2 -Quiet
if ($?) { Write-Output "IPv4: OK" } else { Write-Output "IPv4: FAILED" }

Write-Output ""
Write-Output "=== PORTS ==="
Write-Output "--- listening TCP ports ---"
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | 
    Select-Object LocalAddress, LocalPort, OwningProcess | Format-Table -AutoSize
Write-Output "--- port count (TCP listen) ---"
$ports = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue
Write-Output "$($ports.Count) listening ports"
Write-Output "--- unusual ports (>10000) ---"
$ports | Where-Object LocalPort -gt 10000 | Select-Object LocalPort, OwningProcess | Format-Table -AutoSize

Write-Output ""
Write-Output "=== PROXY ==="
Write-Output "HTTP_PROXY=$([Environment]::GetEnvironmentVariable('HTTP_PROXY', 'User'))"
Write-Output "HTTPS_PROXY=$([Environment]::GetEnvironmentVariable('HTTPS_PROXY', 'User'))"
Write-Output "NO_PROXY=$([Environment]::GetEnvironmentVariable('NO_PROXY', 'User'))"
Write-Output "GENERICAGENT_PROXY=$([Environment]::GetEnvironmentVariable('GENERICAGENT_PROXY', 'User'))"
# Also check process-level env
Write-Output "--- process env ---"
Write-Output "HTTP_PROXY=$env:HTTP_PROXY"
Write-Output "HTTPS_PROXY=$env:HTTPS_PROXY"
```

### Step 2: 分析并生成报告

对 Step 1 的原始输出进行分析，按以下 6 个分区组织报告（每分区包含状态标记 + 基本信息表格 + 1-2 句评价）：

| 分区 | 内容 | 状态标记 |
|------|------|----------|
| 1. Interfaces | 网络接口数量、类型、状态（UP/DOWN）、默认出站接口速度 | 🟢🟡🔴 |
| 2. Routing | 默认网关 IP、路由表条目数、网关可达性 | 🟢🟡🔴 |
| 3. DNS | DNS 服务器列表、服务器数量、github.com/pypi.org 解析结果 | 🟢🟡🔴 |
| 4. Connectivity | pypi.org/github.com/google.com HTTP 状态码、IPv4 外网连通性 | 🟢🟡🔴 |
| 5. Ports | 监听端口总数和列表、异常端口（>10000）数量 | 🟢🟡🔴 |
| 6. Proxy | 代理配置值（含空值表示） | 🟢🟡🔴 |

#### 网络健康评分计算规则

| 维度 | 权重 | 满分 | 扣分条件 |
|:----:|:----:|:----:|----------|
| 接口可用性 | 15% | 15 | 无 UP 状态的接口扣 15 |
| 路由健康 | 15% | 15 | 无默认路由扣 10；网关不可达扣 5 |
| DNS 冗余 | 25% | 25 | 仅 1 个 DNS 服务器扣 15（SPOF 风险）；DNS 解析失败扣 10 |
| 外网连通 | 30% | 30 | pypi.org 不通扣 10；github.com 不通扣 10；IPv4 不通扣 10 |
| 端口安全 | 10% | 10 | 有大量 >10000 的异常端口扣 5 |
| 代理配置 | 5% | 5 | 代理已配置但不可达扣 3 |

评分档位：
- 🟢 **90-100**: 优秀 — 网络栈完整，有冗余 DNS，连通性正常
- 🟡 **70-89**: 良好 — 核心连通正常，存在可改进项
- 🔴 **< 70**: 需关注 — 关键链路缺失或中断

### Step 3: 写入报告

将完整报告写入沙箱输出路径：

```
D:\GenericAgent\sandbox\reports\network_monitor_YYYY-MM-DD.md
```

报告格式约束：
- 每个分区一个独立小节，包含状态标记 + 表格 + 评价
- 末尾附评分明细表和最终评分（进度条 + 分数 + 等级）
- 末尾附 SPOF 警告区：突出标注 DNS 单点故障风险及建议
- 末尾附改进项列表（按优先级排序）
- 底部标注审计标记：`*报告由网络监控工具自动生成 | 仅执行只读探测，未修改任何网络配置*`

## 成功标准

- 报告覆盖全部 6 个分区：Interfaces、Routing、DNS、Connectivity、Ports、Proxy
- DNS 分区明确报告 DNS 服务器数量和每个服务器地址
- 连通性矩阵覆盖 pypi.org 和 github.com 两大核心站点的 HTTP 状态
- 网络健康评分基于 6 个维度加权计算，范围 0-100
- DNS SPOF 风险被明确标注并给出改进建议
- 所有探测仅执行只读操作

## 验证命令

```powershell
# 检查输出文件存在且行数合理
$reportPath = "D:\GenericAgent\sandbox\reports\network_monitor_$(Get-Date -Format 'yyyy-MM-dd').md"
(Get-Content $reportPath | Measure-Object -Line).Lines
# 期望: >= 50 行

# 检查所有 6 个分区标题是否存在
Select-String -Path $reportPath -Pattern 'Interfaces|Routing|DNS|Connectivity|Ports|Proxy'
# 期望: >= 6

# 确认 DNS 服务器数量已报告
Select-String -Path $reportPath -Pattern 'DNS.*服务器|nameserver|ServerAddress'
# 期望: >= 2

# 检查评分数值
Select-String -Path $reportPath -Pattern '\d+\s*/\s*100'
# 期望: 输出格式如 "98/100"

# 确认只读声明存在
Select-String -Path $reportPath -Pattern '只读探测'
# 期望: 1

# 确认连通性结果包含关键站点
Select-String -Path $reportPath -Pattern 'pypi\.org|github\.com'
# 期望: >= 2
```

## 失败恢复

| 尝试次数 | 操作 |
|---------|------|
| 第 1 次失败 | 检查单个命令是否因权限不足报错（某些网络 cmdlet 需要管理员权限）；`Get-NetAdapter` 失败时降级到 `ipconfig` |
| 第 2 次失败 | 跳过失败的命令，用 `try/catch` 兜底；继续生成不完整报告，缺失分区标记为 ⚠️ UNAVAILABLE |
| 第 3 次失败 | 请求用户介入，展示失败的具体命令和错误输出 |

## 人为确认点

| 操作 | 确认要求 |
|------|----------|
| `Invoke-WebRequest` 外部站点 | 仅执行 HEAD 请求，不下载页面内容 |
| `Test-Connection` | ICMP 包数量限制为 2 次 |
| `Get-NetTCPConnection` | 需管理员权限可获取 OwningProcess；如失败，降级到无进程信息的端口列表 |

## 预期输出

报告写入：`D:\GenericAgent\sandbox\reports\network_monitor_YYYY-MM-DD.md`

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-09 | 基于 network monitor 任务 Pass 1 成功运行结晶 |
| v2 | 2026-06-10 | 迁移至 Windows 原生 PowerShell 命令，替换 WSL/Linux 专用命令（ip→Get-NetAdapter, ss→Get-NetTCPConnection, dig→Resolve-DnsName 等） |

## Provenance

- First successful run: 2026-06-09
- Pass 1 friction: 5 rounds
- Pass 2 optimized: [date TBD]
- Pass 3 crystallized: [date TBD]
