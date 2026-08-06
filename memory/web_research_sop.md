# Controlled Web Research SOP

## 触发词

- "建立低风险网页调研 Skill"
- "网页调研"
- "web research"
- "查询话题"
- "无害文档查询"

## 目的

对指定话题进行低风险网页调研，支持来源捕获和交叉验证。只做浏览/阅读/总结，不做账号操作或表单提交。

## Path Rule (CRITICAL)
All output paths MUST be absolute. The agent's CWD during `--task` execution is `temp/`, so relative paths like `sandbox/reports/` will resolve incorrectly. Always use:
- `/opt/GenericAgent/sandbox/reports/` for reports
- `/opt/GenericAgent/sandbox/workspace/` for workspace files

## 前置条件

1. 已读取 `memory/sandbox_policy.md` — 确认写入权限
2. Python 可用（`python --version`）

## 输入

- **topic**: 调研话题（字符串）
- **allowed_sources**: 允许的来源 URL 列表（可选，默认：官方文档 + 1-2 个知名教程站）

## 工作流

### 路径 0: web_fetch 优先抓取（推荐首选，零依赖）

先尝试使用 `web_fetch` 工具直接抓取目标 URL，零依赖、零配置。

**步骤:**
1. 确定目标 URL 列表（官方文档、PEP、知名站点）
2. 逐个调用 `web_fetch(url, max_chars=15000)` 获取页面文本
3. 若 `web_fetch` 返回压缩乱码，回退到 `code_run(python)` + urllib 获取
4. 交叉验证后输出调研报告

**限制:**
- 对 gzip 压缩页面可能返回乱码（如 python.org/downloads），需回退到 urllib
- 不支持 JS 渲染的 SPA 页面

### 路径 A: code_run + urllib 直接 HTTP（回退首选，无额外依赖）

使用 `code_run(python)` + `urllib`（标准库，无需装包）。

注意：**`web_search`（DuckDuckGo）在此环境被 captcha 拦截**，不要依赖搜索后端。直接确定目标 URL 进行抓取。

**步骤:**
1. 确定目标 URL 列表
2. 用 `code_run(python)` 执行 urllib 请求（设置 `User-Agent` 和 SSL context）
3. 记录 HTTP 状态码和内容长度
4. 如需解析 HTML 则用 stdlib `html.parser` 或安装 `beautifulsoup4`
5. 交叉验证后输出调研报告

```python
# 标准模板
import urllib.request, ssl
ctx = ssl._create_unverified_context()
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ..."})
resp = urllib.request.urlopen(req, context=ctx, timeout=15)
text = resp.read().decode("utf-8", errors="replace")
```

**限制:**
- SPA/JS 渲染页面不可用
- Cloudflare 等保护可能拦截

**步骤:**
1. 确定话题和来源列表
2. 对每个来源发送 HTTP GET 请求
3. 解析 HTML 提取相关内容
4. 记录每个来源的 HTTP 状态码和内容长度
5. 交叉验证：检查多个来源对同一事实的描述是否一致
6. 输出调研摘要

**限制:**
- 仅适用于静态页面，SPA/JS 渲染页面不可用
- 被 Cloudflare 等保护的站点可能被拦截

### 路径 B: 浏览器 TMWebDriver（需 TMWebDriver 依赖正常）

使用 `web_scan` / `web_execute_js` 工具。

**步骤:**
1. 阅读 `memory/tmwebdriver_sop.md` 获取最新操作指南
2. 用 `web_scan(tabs_only=True)` 检查浏览器状态
3. 用 `web_execute_js(script='location.href="URL"')` 导航到来源
4. 用 `web_scan(text_only=True)` 获取页面文本
5. 导航到第二个来源交叉验证
6. 输出调研摘要

**限制:**
- 依赖 `simple-websocket-server` 包
- JS 事件 `isTrusted=false`，敏感操作可能被拦截

## 交叉验证规则

1. 至少使用 **2 个独立来源**
2. 对每个关键事实，检查两个来源的一致性
3. 记录不一致之处（如果有）
4. 优先使用官方文档作为主要来源
5. 教程/博客作为辅助验证

## 输出

调研保存在沙箱中（`/opt/GenericAgent/sandbox/`），包含:
- 来源列表（URL + HTTP 状态码）
- 每个来源提取的内容摘要
- 交叉验证结论

## 验证命令

```bash
# 检查调研结果文件
ls /opt/GenericAgent/sandbox/web_research_*.md
grep -E 'HTTP 200|交叉验证' /opt/GenericAgent/sandbox/web_research_*.md
```

## 演示示例

**话题**: Python `pathlib.Path.read_text()`
**来源**: docs.python.org + realpython.com
**结果**: 两份来源一致确认 `.read_text()` 以文本模式打开文件并返回字符串内容

```
源1: docs.python.org → HTTP 200 (270KB)
  "Return the decoded contents of the pointed-to file as a string"

源2: realpython.com → HTTP 200 (230KB)
  ".read_text() opens the path in text mode and returns the contents as a string"
✅ 交叉验证通过
```

## 失败恢复

| 尝试 | 动作 |
|------|------|
| 第 1 次 | web_fetch 失败 → 切 code_run(urllib) 直连，设 User-Agent |
| 第 2 次 | urllib 被拦 → 换备用来源 URL |
| 第 3 次 | 全部失败 → 请求用户提供替代来源或允许更高风险操作 |

> ⚠️ 已知环境限制：`web_search`（DuckDuckGo）被 captcha 拦截，**不要依赖搜索后端**。直接确定目标 URL 进行抓取。

## 人为确认点

1. **写 memory/**：调研 SOP 写入需要用户批准
2. **高风险来源**：从未知/高风险网站抓取前需确认
3. **表单操作**：任何表单提交或账号操作均禁止

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-08 | 基于 Cold Start Task 5 经验结晶，含直接 HTTP 和浏览器双路径 |
| v2 | 2026-06-10 | 迁移至 Windows 原生路径 |
| v3 | 2026-06-11 | 新增 web_fetch 首选路径，urllib 回退；记录 DuckDuckGo 搜索被拦截的环境限制 |
| v4 | 2026-08-06 | Linux 化：路径分隔符与验证命令迁移至 Linux bash（Ubuntu 24.04，root） |
