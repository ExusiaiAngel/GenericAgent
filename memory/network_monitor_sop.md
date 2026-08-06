# Network Monitor SOP

## 触发词

- "网络监控"
- "检查网络"
- "network check"
- "网络状态"

## 目的

对当前 Linux 开发环境（Ubuntu 24.04，root）的网络栈执行完整巡检，覆盖网络接口状态、路由表、DNS 解析、外网连通性、监听端口和代理配置六大检查区，输出结构化的网络监控报告并给出网络健康评分与 SPOF 风险标注。

本 SOP 定位为 **完整网络层诊断**：每个检查区给出独立状态标记，总报告包含 DNS 冗余度、连通性矩阵和代理链路状态，以 🟢🟡🔴 表情符号标注状态等级。

## 前置条件

1. 已读取 `memory/sandbox_policy.md` — 确认写入边界
2. Linux bash 环境（Ubuntu 24.04，root）可用（root 权限可获取更完整信息，但不是必须）
3. 本 SOP 仅执行只读网络探测，不修改任何配置

## 输入

无 — 使用默认 GenericAgent 工作区，自动检测网络状态。可选 `{target_hosts}` 参数指定额外连通性检查目标。

## 步骤

### Step 1: 一次性数据采集（批量 bash）

将所有六个检查区的命令合并为一个脚本执行，减少 round-trip：

```bash
echo "=== INTERFACES ==="
echo "--- ip addr ---"
ip -brief addr 2>&1
echo "--- interface count (UP) ---"
ip -o link 2>/dev/null | grep -c 'state UP'
echo "--- default interface ---"
ip -o -4 route show default 2>/dev/null | awk '{print $5, $6}'

echo ""
echo "=== ROUTING ==="
echo "--- default route ---"
ip -4 route show default 2>/dev/null
echo "--- full routing table ---"
ip route show 2>/dev/null
echo "--- gateway reachable ---"
gateway=$(ip -4 route show default 2>/dev/null | awk '{print $3; exit}')
if [ -n "$gateway" ]; then
    if ping -c 2 -W 2 "$gateway" >/dev/null 2>&1; then
        echo "Gateway: $gateway reachable"
    else
        echo "Gateway: $gateway UNREACHABLE"
    fi
else
    echo "WARNING: no default gateway found"
fi

echo ""
echo "=== DNS ==="
echo "--- DNS server addresses ---"
grep -E '^\s*nameserver' /etc/resolv.conf 2>/dev/null || echo "(no resolv.conf found)"
echo "--- DNS server count ---"
dns_servers=$(grep -E '^\s*nameserver' /etc/resolv.conf 2>/dev/null | awk '{print $2}' | sort -u)
echo "$(printf '%s\n' "$dns_servers" | grep -c .) unique DNS servers: $(printf '%s\n' "$dns_servers" | tr '\n' ' ')"
echo "--- DNS resolution (github.com) ---"
getent ahostsv4 github.com 2>/dev/null | awk '{print $1}' | sort -u || echo "(DNS resolution failed)"
echo "--- DNS resolution (pypi.org) ---"
getent ahostsv4 pypi.org 2>/dev/null | awk '{print $1}' | sort -u || echo "(DNS resolution failed)"

echo ""
echo "=== CONNECTIVITY ==="
echo "--- pypi.org HTTP ---"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -I https://pypi.org 2>/dev/null)
[ -n "$code" ] && echo "pypi.org: $code" || echo "pypi.org: FAILED"
echo "--- github.com HTTP ---"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -I https://github.com 2>/dev/null)
[ -n "$code" ] && echo "github.com: $code" || echo "github.com: FAILED"
echo "--- google.com HTTP ---"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -I https://google.com 2>/dev/null)
[ -n "$code" ] && echo "google.com: $code" || echo "google.com: UNREACHABLE"
echo "--- IPv4 connectivity ---"
if ping -c 2 -W 2 8.8.8.8 >/dev/null 2>&1; then echo "IPv4: OK"; else echo "IPv4: FAILED"; fi

echo ""
echo "=== PORTS ==="
echo "--- listening TCP ports ---"
ss -tlnp 2>/dev/null
echo "--- port count (TCP listen) ---"
ss -tlnH 2>/dev/null | wc -l
echo "--- unusual ports (>10000) ---"
ss -tlnH 2>/dev/null | awk '{split($4,a,":"); if (a[length(a)]+0 > 10000) print}'

echo ""
echo "=== PROXY ==="
echo "HTTP_PROXY=$HTTP_PROXY"
echo "HTTPS_PROXY=$HTTPS_PROXY"
echo "NO_PROXY=$NO_PROXY"
echo "GENERICAGENT_PROXY=$GENERICAGENT_PROXY"
# Also check system-wide env in /etc/environment
echo "--- /etc/environment ---"
grep -iE 'HTTP_PROXY|HTTPS_PROXY|NO_PROXY' /etc/environment 2>/dev/null || echo "(no proxy in /etc/environment)"
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
/opt/GenericAgent/sandbox/reports/network_monitor_YYYY-MM-DD.md
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

```bash
# 检查输出文件存在且行数合理
report_path="/opt/GenericAgent/sandbox/reports/network_monitor_$(date +%F).md"
wc -l "$report_path"
# 期望: >= 50 行

# 检查所有 6 个分区标题是否存在
grep -cE 'Interfaces|Routing|DNS|Connectivity|Ports|Proxy' "$report_path"
# 期望: >= 6

# 确认 DNS 服务器数量已报告
grep -cE 'DNS.*服务器|nameserver|ServerAddress' "$report_path"
# 期望: >= 2

# 检查评分数值
grep -E '[0-9]+\s*/\s*100' "$report_path"
# 期望: 输出格式如 "98/100"

# 确认只读声明存在
grep -c '只读探测' "$report_path"
# 期望: 1

# 确认连通性结果包含关键站点
grep -cE 'pypi\.org|github\.com' "$report_path"
# 期望: >= 2
```

## 失败恢复

| 尝试次数 | 操作 |
|---------|------|
| 第 1 次失败 | 检查单个命令是否因权限不足报错（部分网络命令需要 root 权限）；`ip` 命令失败时降级到 `ifconfig` / 读取 `/proc/net/route` |
| 第 2 次失败 | 跳过失败的命令，用 `try/catch` 兜底；继续生成不完整报告，缺失分区标记为 ⚠️ UNAVAILABLE |
| 第 3 次失败 | 请求用户介入，展示失败的具体命令和错误输出 |

## 人为确认点

| 操作 | 确认要求 |
|------|----------|
| `curl -I` 外部站点 | 仅执行 HEAD 请求，不下载页面内容 |
| `ping` | ICMP 包数量限制为 2 次 |
| `ss -tlnp` | 需管理员权限可获取 OwningProcess；如失败，降级到无进程信息的端口列表 |

## 预期输出

报告写入：`/opt/GenericAgent/sandbox/reports/network_monitor_YYYY-MM-DD.md`

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-09 | 基于 network monitor 任务 Pass 1 成功运行结晶 |
| v2 | 2026-06-10 | 迁移至 Windows 原生 PowerShell 命令，替换 WSL/Linux 专用命令（ip→ip link, ss→ss -tlnp, dig→Resolve-DnsName 等） |
| v3 | 2026-08-06 | 迁移至 Linux bash 环境（Ubuntu 24.04，root），命令全面 Linux 化（ip/ip route/getent/curl/ss/ping） |

## Provenance

- First successful run: 2026-06-09
- Pass 1 friction: 5 rounds
- Pass 2 optimized: [date TBD]
- Pass 3 crystallized: [date TBD]
