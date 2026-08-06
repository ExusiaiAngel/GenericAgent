# Config Audit SOP

## 触发词

- "配置审计"
- "检查我的开发配置"
- "config audit"

## 目的

对当前 Linux 开发环境（Ubuntu 24.04，root）进行配置层面的安全检查，审计 Shell、Git、SSH、环境变量、Docker 五大配置区，输出结构化的配置审计报告并给出配置完整性评分。

## 前置条件

1. 已读取 `memory/sandbox_policy.md` — 确认写入边界
2. 已读取 `memory/env_audit_sop.md` — 了解环境审计基线
3. Linux bash 环境（Ubuntu 24.04，root）可用
4. **CRITICAL SECURITY RULE**: 任何步骤中绝对禁止打印 token、password、secret、key 等敏感值

## 输入

无 — 使用默认 GenericAgent 工作区，自动检测开发环境配置。

## 步骤

### Step 1: 一次性数据采集（批量 bash，含安全过滤）

**安全红线**：以下命令均已做安全过滤 —— `git config --list` 使用 `grep -v` 排除 token/password 行；SSH 检查仅用 `ls` 列出文件，不使用 `cat` 读取任何密钥内容。

```bash
echo "=== SHELL ==="
echo "--- bash version ---"
bash --version | head -1
echo "--- profile files ---"
ls -la "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile" 2>/dev/null || echo "profile: NOT FOUND"
echo "--- login shell ---"
echo "SHELL=$SHELL UID=$(id -u)"

echo ""
echo "=== GIT ==="
echo "--- git version ---"
git --version 2>&1
echo "--- git global config (SAFE: token/password filtered) ---"
git config --list --global 2>&1 | grep -viE 'token|password|secret|key'
echo "--- git aliases ---"
git config --global --get-regexp alias 2>&1

echo ""
echo "=== SSH ==="
sshDir="$HOME/.ssh"
echo "--- .ssh directory ---"
ls -la "$sshDir" 2>/dev/null || echo ".ssh: NOT FOUND"
echo "--- key count ---"
keys=$(ls "$sshDir"/id_* 2>/dev/null)
echo "$(printf '%s\n' "$keys" | grep -c .) key files"
echo "--- known_hosts ---"
if [ -f "$sshDir/known_hosts" ]; then
    echo "known_hosts: $(stat -c %s "$sshDir/known_hosts") bytes"
else
    echo "known_hosts: NOT FOUND"
fi
echo "--- ssh-agent ---"
ssh-add -l 2>&1 || echo "ssh-agent not running or no identities"

echo ""
echo "=== ENV ==="
echo "--- GenericAgent/Python variables ---"
env | grep -iE 'GENERICAGENT|PYTHON|PATH|PROXY' | grep -viE 'TOKEN|PASSWORD|SECRET|KEY'
echo "--- key tool versions ---"
python3 --version 2>&1
node --version 2>&1 || echo "(node not installed)"
npm --version 2>&1 || echo "(npm not installed)"

echo ""
echo "=== DOCKER ==="
echo "--- client ---"
docker --version 2>&1 || { echo "(docker not installed)"; exit 0; }
echo "--- server info ---"
docker info 2>&1 | grep -iE 'Server Version|Operating System|Storage Driver|Containers:|Images:|Running'
```

### Step 2: 分析并生成报告

对 Step 1 的原始输出进行分析，按以下 5 个分区组织报告：

| 分区 | 内容 | 状态标记 |
|------|------|----------|
| 1. Shell 配置 | bash 版本、profile 文件存在性、登录 shell 配置 | 🟢🟡🔴 |
| 2. Git 全局配置 | user.name / user.email / credential.helper / aliases（已过滤敏感值） | 🟢🟡🔴 |
| 3. SSH 密钥 | `~/.ssh/` 目录文件列表、密钥文件数量、known_hosts 状态、ssh-agent 状态 | 🟢🟡🔴 |
| 4. 环境变量 | GenericAgent 相关变量、Python/Node.js 版本、PATH 状态 | 🟢🟡🔴 |
| 5. Docker 状态 | client/server 版本、引擎运行状态、容器/镜像数量（仅当 Docker 已安装） | 🟢🟡🔴 |

每个分区包含：基本信息表格 + 简短评价（1-2 句）。

#### 配置完整性评分计算规则

| 类别 | 权重 | 满分 | 扣分条件 |
|:----:|:----:|:----:|----------|
| Shell 配置 | 20% | 20 | bash < 5.0 扣 5；无 profile 文件扣 5；登录 shell 非 bash 扣 10 |
| Git 配置 | 25% | 25 | 无 `user.name` / `user.email` 扣 15；无常用 aliases 扣 5 |
| SSH 密钥 | 20% | 20 | 0 个密钥扣 15；`known_hosts` 不存在扣 5 |
| 环境变量 | 20% | 20 | Python 不在 PATH 扣 10；Node.js 缺失扣 5 |
| Docker 状态 | 15% | 15 | Docker daemon 不可用扣 15 |

评分档位：
- 🟢 **90-100**: 优秀 — 所有配置区完整，无安全问题
- 🟡 **70-89**: 良好 — 核心链路完整，有非关键改进项
- 🔴 **< 70**: 需处理 — 关键配置缺失

### Step 3: 写入报告

将完整报告写入沙箱输出路径：

```
/opt/GenericAgent/sandbox/reports/config_audit_YYYY-MM-DD.md
```

报告格式约束：
- 每个分区一个独立小节，包含状态标记 + 表格 + 评价
- 末尾附评分明细表和最终评分（进度条 + 分数 + 等级）
- 末尾附改进项列表（按优先级排序）
- 底部标注审计标记：`*报告由环境审计工具自动生成 | 仅执行只读探测，未修改任何配置*`

## 成功标准

- 报告覆盖全部 5 个分区
- SSH 分区必须明确报告密钥数量
- Git 配置中不包含任何 token/password/secret 值（已过滤）
- 所有敏感文件仅使用 `ls` / `stat` 获取元数据，未使用 `cat` 读取内容
- 配置完整性评分基于 5 个维度加权计算，范围 0-100
- 输出文件非空且超过 40 行

## 验证命令

```bash
# 检查输出文件存在且行数合理
report_path="/opt/GenericAgent/sandbox/reports/config_audit_$(date +%F).md"
wc -l "$report_path"
# 期望: >= 40 行

# 检查所有分区标题是否存在
grep -cE 'Shell 配置|SSH 密钥|环境变量|Docker|Git' "$report_path"
# 期望: >= 5

# 安全验证：确认无敏感值泄露
grep -cE 'token|password|secret|^[a-zA-Z0-9+/]{40,}$' "$report_path"
# 期望: 0

# 检查评分数值
grep -E '[0-9]+\s*/\s*100' "$report_path"
# 期望: 输出格式如 "81/100"

# 确认只读声明存在
grep -c '只读探测' "$report_path"
# 期望: 1
```

## 失败恢复

| 尝试次数 | 操作 |
|---------|------|
| 第 1 次失败 | 检查单个命令是否因权限或工具缺失报错 |
| 第 2 次失败 | 跳过失败的命令，继续生成不完整报告，缺失分区标记为 ⚠️ UNAVAILABLE |
| 第 3 次失败 | 请求用户介入 |

## 人为确认点

| 操作 | 确认要求 |
|------|----------|
| `git config --list` | 必须过滤敏感值 |
| `ls ~/.ssh/` | 只列出文件名和权限，**禁止** `cat` 任何 `id_*` 文件内容 |
| `env \| grep` | 过滤仅显示 `GENERICAGENT`/`PYTHON` 相关变量 |

**安全红线（不可违反）**：
1. **绝对禁止** `cat` 任何 SSH 私钥文件
2. **绝对禁止** 输出 `git config --list` 中包含 `token`/`password`/`secret` 关键字的行
3. **mykey.py 绝不可读** — 本 SOP 不涉及 `mykey.py`，但同样适用于所有 SOP 的安全原则

## 预期输出

报告写入：`/opt/GenericAgent/sandbox/reports/config_audit_YYYY-MM-DD.md`

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-09 | 基于 config audit 任务 Pass 1 成功运行结晶 |
| v2 | 2026-06-10 | 迁移至 Windows 原生路径和 PowerShell 命令，移除 WSL 专用检查 |
| v3 | 2026-08-06 | 迁移至 Linux bash 环境（Ubuntu 24.04，root），命令与路径全面 Linux 化 |

## Provenance

- First successful run: 2026-06-09
- Pass 1 friction: 6 rounds
- Security: secrets properly filtered
- Pass 2 optimized: [date TBD]
- Pass 3 crystallized: [date TBD]
