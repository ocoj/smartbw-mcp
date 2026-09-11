#!/bin/bash
# SmartBW MCP — 版本号一键同步（pyproject / serverInfo / CHANGELOG）
#
# 用法：
#   bash scripts/bump-version.sh 2.3.9          # 显式指定目标版本
#   bash scripts/bump-version.sh --auto         # 依据 conventional commits 自动推导
#   bash scripts/bump-version.sh                # 不带参数：显示当前版本与用法
#
# --auto 的推导规则（统计范围：最新 tag → HEAD）：
#   BREAKING CHANGE / 类型后带 `!`   → major
#   feat                             → minor
#   其余（fix/docs/test/ci/refactor/chore…）→ patch
#   无语提交                          → 不 bump（退出码 0，不做修改）
#
# 说明：docs/test/ci 这类提交也计 patch，是刻意对齐本仓库既有习惯
# （2.3.2 ~ 2.3.8 均为 patch 递增）。
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

SKELETON=""
MODE="explicit"
NEW="${1:-}"

if [ "$NEW" = "--auto" ] || [ "$NEW" = "-a" ]; then
    MODE="auto"
elif [ -z "$NEW" ]; then
    echo "用法:"
    echo "  bash scripts/bump-version.sh <x.y.z>   # 显式指定版本"
    echo "  bash scripts/bump-version.sh --auto    # 依据 conventional commits 自动推导"
    echo "当前版本: $CUR"
    exit 0
fi

# ---------------------------------------------------------------------------
# --auto：从提交信息推导版本号，并生成 CHANGELOG 骨架
# ---------------------------------------------------------------------------
if [ "$MODE" = "auto" ]; then
    echo "========================================"
    echo "  自动推导版本（conventional commits）"
    echo "========================================"
    SKELETON="$(mktemp)"
    NEW="$(python3 - "$CUR" "$SKELETON" <<'PY'
import pathlib
import re
import subprocess
import sys

cur, skeleton_path = sys.argv[1], sys.argv[2]

# 统计范围：最新 tag → HEAD（无 tag 则全历史）
tag = subprocess.run(["git", "describe", "--tags", "--abbrev=0"],
                     capture_output=True, text=True).stdout.strip()

# 防重复 bump：推导基线是「最新 tag」，因此同一批提交若被重复 --auto 会连续递增。
# 判据：当前版本尚未被 tag 覆盖，但 CHANGELOG 已含该版本条目 ⇒ 上一次已 bump 过
# （只是还没提交/发版），此时应停下，而不是再涨一个版本。
tag_ver = tag.lstrip("v") if tag else ""
changelog_text = pathlib.Path("CHANGELOG.md").read_text(encoding="utf-8")
if tag_ver and cur != tag_ver and f"## [{cur}]" in changelog_text:
    sys.stderr.write(
        f"  ⚠️  当前版本 {cur} 未被最新 tag（{tag}）覆盖，且 CHANGELOG 已含该条目\n"
        f"     ⇒ 判定为「已 bump 未发版」，本次不重复 bump。\n"
        f"     如确需再次 bump，请显式指定：bash scripts/bump-version.sh <x.y.z>\n")
    print("SKIP")
    sys.exit(0)

rng = f"{tag}..HEAD" if tag else "HEAD"
raw = subprocess.run(["git", "log", rng, "--format=%s%x1f%b%x1e"],
                     capture_output=True, text=True).stdout

commits = []
for chunk in raw.split("\x1e"):
    chunk = chunk.strip("\n")
    if not chunk.strip():
        continue
    subject, _, body = chunk.partition("\x1f")
    commits.append((subject.strip(), body))

if not commits:
    print("NONE")
    sys.exit(0)

PAT = re.compile(r'^(?P<t>[a-z]+)(\((?P<s>[^)]*)\))?(?P<b>!)?:\s*(?P<subj>.+)$', re.I)
GROUPS = [
    ("feat", "🚀 特性"), ("fix", "🔧 修复"), ("perf", "⚡ 性能"),
    ("refactor", "♻️ 重构"), ("test", "🧪 测试"), ("docs", "📝 文档"),
    ("ci", "🤖 工程"), ("chore", "🧹 杂项"), ("other", "📌 其他"),
]
bucket = {k: [] for k, _ in GROUPS}
breaking, has_feat = [], False

for subject, body in commits:
    m = PAT.match(subject)
    if not m:                                   # 非规范提交也计入（归入"其他"）
        bucket["other"].append(subject)
        continue
    t = m.group("t").lower()
    if m.group("b") or re.search(r'^BREAKING[ -]CHANGE:', body, re.M):
        breaking.append(subject)
    if t == "feat":
        has_feat = True
    bucket[t if t in bucket else "other"].append(subject)

level = "major" if breaking else ("minor" if has_feat else "patch")
mj, mn, pt = (int(x) for x in cur.split("."))
new = (f"{mj + 1}.0.0" if level == "major"
       else f"{mj}.{mn + 1}.0" if level == "minor"
       else f"{mj}.{mn}.{pt + 1}")

lines = [
    f"> 自 {tag or '初始提交'} 以来共 {len(commits)} 个提交，按类型自动汇总；"
    f"**请补全「为什么」与验证方式**。",
    "",
]
for key, title in GROUPS:
    if not bucket[key]:
        continue
    lines += [f"### {title}", ""] + [f"- {s}" for s in bucket[key]] + [""]
if breaking:
    lines += ["### ⚠️ 破坏性变更", ""] + [f"- {s}" for s in breaking] + [""]
pathlib.Path(skeleton_path).write_text("\n".join(lines), encoding="utf-8")

sys.stderr.write(f"  推导级别: {level}（{len(commits)} 个提交，自 {tag or '初始提交'}）\n")
for key, title in GROUPS:
    if bucket[key]:
        sys.stderr.write(f"    {title}: {len(bucket[key])} 条\n")
print(new)
PY
)"
    if [ "$NEW" = "SKIP" ]; then
        rm -f "$SKELETON"
        exit 0
    fi
    if [ "$NEW" = "NONE" ]; then
        echo "⚠️  自最新 tag 以来没有提交，未做修改"
        rm -f "$SKELETON"
        exit 0
    fi
    echo "  推导版本: $CUR → $NEW"
fi

if [ "$MODE" = "explicit" ] && ! echo "$NEW" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
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
    python3 - "$NEW" "$TODAY" "$SKELETON" <<'PY'
import pathlib
import sys

new, today = sys.argv[1], sys.argv[2]
skeleton_file = sys.argv[3] if len(sys.argv) > 3 else ""
path = pathlib.Path("CHANGELOG.md")
text = path.read_text(encoding="utf-8")

header = "# 变更日志\n"
if not text.startswith(header):
    sys.exit(f"❌ {path} 结构不符：缺少一级标题 '{header.strip()}'")

rest = text[len(header):].lstrip("\n")

# --auto 会传入按提交类型分组的骨架；显式模式则用通用骨架
body = ""
if skeleton_file and pathlib.Path(skeleton_file).exists():
    body = pathlib.Path(skeleton_file).read_text(encoding="utf-8").strip()
if not body:
    body = (
        "> 一句话说明本次变更的主题（依据来源：issue / 审计报告条目 / 用户反馈）。\n\n"
        "### 🔧 修复\n\n- \n\n"
        "### 🧪 测试\n\n- "
    )

path.write_text(header + "\n" + f"## [{new}] - {today}\n\n" + body + "\n\n" + rest,
                encoding="utf-8")
print("  ✓ CHANGELOG.md（已插入骨架，请补全正文）")
PY
fi

# 清理 --auto 的临时骨架文件
[ -n "$SKELETON" ] && rm -f "$SKELETON"

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
