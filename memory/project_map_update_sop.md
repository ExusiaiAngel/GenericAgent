# Project Map Update SOP

## 触发条件
- 新模块/目录加入项目时
- 发现现有 project_map.md 信息不准确时
- 冷启动/重大重构后

## 更新流程

### 1. 数据采集（1 步替代多步）
```bash
# 一次性采集：顶层文件 + 子目录 + 行数
cd /opt/GenericAgent
echo "=== 顶层 .py/.pyw ==="
ls *.py *.pyw 2>/dev/null
echo "=== 子目录 ==="
for d in memory reflect plugins frontends docs assets; do
    echo "--- $d/ ---"
    ls "$d"/*.py "$d"/*.md 2>/dev/null | head -20
done
echo "=== 行数 ==="
wc -l agentmain.py agent_loop.py ga.py llmcore.py TMWebDriver.py 2>/dev/null
# 检查遗漏
for f in *.py; do
    name=$(basename "$f")
    lines=$(wc -l < "$f")
    if ! grep -qF "$name" memory/project_map.md; then
        echo "WARNING: 未记录: $name ($lines 行)"
    fi
done
```
### 2. 对比现有地图
- 读取 `memory/project_map.md`
- 标记缺失模块、错误行数、过时路径

### 3. 更新
- 优先 patch，避免 overwrite
- 验证表格格式：`|| **名称** | \`路径\` | 说明 |`
- 确认所有路径可访问

### 4. 验证
```bash
# 路径可用性检查
grep -oE '`[^`]+\.(py|md|pyw)`' memory/project_map.md | tr -d '`' | while read -r p; do
    if [ -e "/opt/GenericAgent/$p" ]; then echo "✅ $p"; else echo "WARNING: ⚠️ $p not found"; fi
done
```
## 注意事项
- `mykey.py` 只引用不读取内容
- 行数标注在 `| 说明 |` 列，如 `(1063行)`
- 内存文件 `memory/*.py` 记录在 Specialized Utilities 章节

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-11 | 自动生成版本记录 |
| v3 | 2026-08-06 | 迁移至 Linux bash 环境（Ubuntu 24.04，root），命令全面 Linux 化 |

