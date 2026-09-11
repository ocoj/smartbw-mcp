#!/bin/bash
# SmartBW MCP — 版本号一键同步（pyproject / serverInfo / CHANGELOG）
#
# 用法：
#   bash scripts/bump-version.sh 2.3.9          # 指定目标版本
#   bash scripts/bump-version.sh                # 不带参数：显示当前版本与用法
#
# 分工（重要）：
#   - 本脚本只把「三处版本号改成一致」并生成 CHANGELOG 骨架，**不提交、不打 tag**
#   - tag 与 GitHub Release 由 `.github/workflows/release.yml` 在 push 到 main 后
#     自动完成（依据 pyproject.toml 的版本号推导 tag；已存在则跳过，幂等）
#
# 因此一次发版的完整流程是：
#   1) bash scripts/bump-version.sh <x.y.z>
#   2) 补全 CHANGELOG 该条目正文
#   3) git add -A && git commit && git push
#   4) CI 自动打 tag + 建 Release（无需手工操作）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

PYPROJECT="pyproject.toml"
SERVER="smartbw_mcp_server.py"
CHANGELOG="CHANGELOG.md"

CUR=$(grep -m1 '^version' "$PYPROJECT" | cut -d'"' -f2)

NEW="${1:-}"
if [ -z "$NEW" ]; then
    echo "用法: bash scripts/bump-version.sh <x.y.z>"
    echo "当前版本: $CUR"
    exit 0
fi

if ! echo "$NEW" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "❌ 版本号格式应为 x.y.z（收到: $NEW）"
    exit 1
fi

if [ "$CUR" = "$NEW" ]; then
    echo "⚠️  当前已是 $NEW，无需修改"
    exit 0
fi

# 提示 bump 级别（仅信息，不做判断）
LEVEL="patch"
IFS=. read -r c_major c_minor _ <<<"$CUR"
IFS=. read -r n_major n_minor _ <<<"$NEW"
[ "$n_minor" != "$c_minor" ] && LEVEL="minor"
[ "$n_major" != "$c_major" ] && LEVEL="major"

TODAY=$(date +%Y-%m-%d)
echo "========================================"
echo "  版本同步: $CUR → $NEW  ($LEVEL)"
echo "========================================"

# ---------------------------------------------------------------------------
# 1) pyproject.toml
# ---------------------------------------------------------------------------
sed -i.bak -E "s/^version = \"[^\"]+\"/version = \"$NEW\"/" "$PYPROJECT" && rm -f "$PYPROJECT.bak"
echo "  ✓ $PYPROJECT"

# ---------------------------------------------------------------------------
# 2) smartbw_mcp_server.py 的 serverInfo.version
# ---------------------------------------------------------------------------
sed -i.bak -E \
    "s/(\"serverInfo\": \{\"name\": \"smartbw-mcp\", \"version\": )\"[^\"]+\"/\1\"$NEW\"/" \
    "$SERVER" && rm -f "$SERVER.bak"
echo "  ✓ $SERVER (serverInfo)"

# ---------------------------------------------------------------------------
# 3) CHANGELOG.md —— 已存在该版本条目则跳过，否则插入骨架
# ---------------------------------------------------------------------------
if grep -q "^## \[$NEW\]" "$CHANGELOG"; then
    echo "  • $CHANGELOG 已有 [$NEW] 条目，跳过插入"
else
    python3 - "$NEW" "$TODAY" <<'PY'
import pathlib
import sys

new, today = sys.argv[1], sys.argv[2]
path = pathlib.Path("CHANGELOG.md")
text = path.read_text(encoding="utf-8")

header = "# 变更日志\n"
if not text.startswith(header):
    sys.exit(f"❌ {path} 结构不符：缺少一级标题 '{header.strip()}'")

rest = text[len(header):].lstrip("\n")
skeleton = (
    f"## [{new}] - {today}\n\n"
    f"> 一句话说明本次变更的主题（依据来源：issue / 审计报告条目 / 用户反馈）。\n\n"
    f"### 🔧 修复\n\n- \n\n"
    f"### 🧪 测试\n\n- \n"
)
path.write_text(header + "\n" + skeleton + "\n" + rest, encoding="utf-8")
print("  ✓ CHANGELOG.md（已插入骨架，请补全正文）")
PY
fi

# ---------------------------------------------------------------------------
# 一致性自检
# ---------------------------------------------------------------------------
echo
python3 - <<'PY'
import pathlib, re, sys
v = re.search(r'^version\s*=\s*"([^"]+)"',
              pathlib.Path("pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
sv = re.search(r'"serverInfo":\s*\{"name":\s*"smartbw-mcp",\s*"version":\s*"([^"]+)"',
               pathlib.Path("smartbw_mcp_server.py").read_text(encoding="utf-8"))
cv = re.search(r'^##\s*\[([^\]]+)\]',
               pathlib.Path("CHANGELOG.md").read_text(encoding="utf-8"), re.M)
vals = {"pyproject": v, "serverInfo": sv.group(1) if sv else None,
        "CHANGELOG": cv.group(1) if cv else None}
print("一致性自检:", vals)
if len({x for x in vals.values() if x}) != 1:
    sys.exit("❌ 三处版本不一致，请检查")
print("✅ 三处一致")
PY

echo
echo "下一步："
echo "  1) 补全 CHANGELOG.md 的 [$NEW] 条目正文"
echo "  2) git add -A && git commit -m \"release: $NEW ...\""
echo "  3) git push origin main"
echo "     → CI 会自动打 tag v$NEW 并创建 GitHub Release（无需手工操作）"
