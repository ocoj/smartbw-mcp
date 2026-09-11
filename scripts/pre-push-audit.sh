#!/bin/bash
# SmartBW MCP — 发布前脱敏审计（推送 / 发版前执行）
#
# 与 scripts/pre-commit.sh 的分工：
#   - pre-commit.sh   守「提交」：暂存内容的敏感模式 + 真实配置文件名
#   - 本脚本          守「推送/发版」：额外覆盖 ① 全量跟踪文件 ② 待推送提交的新增行
#                     ③ 提交作者邮箱 ④ 不应被跟踪的文件 ⑤ tag 注解内容
#
# 用法：
#   bash scripts/pre-push-audit.sh                  # 对比 origin/main
#   bash scripts/pre-push-audit.sh --range <rev>    # 指定对比基线（如上一个 tag）
#
# 原则：命中时只打印「文件:行号」，**绝不打印匹配内容**（避免审计本身造成二次泄露）。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR" || exit 1

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
FAILED=0

# ---------------------------------------------------------------------------
# 白名单：占位域、公共域，以及 RFC 2606 保留域（example.* / .invalid / .test）
# 注意：正则里要转义点号；本脚本与 pre-commit.sh 自身含规则文本，需在扫描时排除。
# ---------------------------------------------------------------------------
ALLOWED='example\.(com|org|net)|\.invalid|\.test|your[-_]|yourserver|vault\.example|vaultwarden\.example|localhost|127\.0\.0\.1|0\.0\.0\.0|bitwarden\.com|bitwarden\.net|github\.com|githubusercontent\.com|python\.org|pypi\.org|npmjs\.com|nodejs\.org|keepachangelog\.com|semver\.org|shields\.io|opensource\.org|ubuntu\.com|debian\.org|centos\.org|redhat\.com|systemd\.io|specifications\.freedesktop\.org|modelcontextprotocol\.io'

# 敏感模式：凭据前缀 / 内网地址 / 真实邮箱域
PATTERNS='(xox[baprs]-|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|x-access-token)'
PATTERNS="$PATTERNS|(^|[^0-9])(10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3})([^0-9]|$)"
PATTERNS="$PATTERNS|@(163|qq|gmail|outlook|hotmail|126)\.com"

# 规则定义处，扫描时排除（它们本身包含模式文本）
SELF_EXCLUDE='^scripts/pre-push-audit\.sh:|^scripts/pre-commit\.sh:'

RANGE="origin/main"
if [ "${1:-}" = "--range" ] && [ -n "${2:-}" ]; then RANGE="$2"; fi

header() { echo -e "\n${BLUE}── $1${NC}"; }
pass()   { echo -e "   ${GREEN}✓${NC} $1"; }
fail()   { echo -e "   ${RED}✗${NC} $1"; FAILED=1; }
warn()   { echo -e "   ${YELLOW}!${NC} $1"; }

echo "========================================"
echo "  发布前脱敏审计"
echo "  对比基线: $RANGE"
echo "========================================"

# ---------------------------------------------------------------------------
# ① 全部跟踪文件的内容扫描
# ---------------------------------------------------------------------------
header "① 跟踪文件内容（凭据 / 内网 IP / 真实邮箱域）"
HITS=$(git ls-files -z 2>/dev/null \
    | xargs -0 grep -nIiE "$PATTERNS" 2>/dev/null \
    | grep -viE "$ALLOWED" \
    | grep -vE "$SELF_EXCLUDE" || true)
if [ -n "$HITS" ]; then
    fail "命中（仅列位置）:"
    echo "$HITS" | cut -d: -f1,2 | sed 's/^/     → /'
else
    pass "$(git ls-files | wc -l) 个跟踪文件均未命中"
fi

# ---------------------------------------------------------------------------
# ② 待推送提交的新增行扫描
# ---------------------------------------------------------------------------
header "② 待推送提交的新增行"
if git rev-parse --verify --quiet "$RANGE" >/dev/null 2>&1; then
    COMMITS=$(git rev-list --count "$RANGE..HEAD" 2>/dev/null || echo 0)
    if [ "$COMMITS" -eq 0 ]; then
        pass "无待推送提交（HEAD 与 $RANGE 一致）"
    else
        HITS=$(git diff "$RANGE..HEAD" 2>/dev/null \
            | grep -E '^\+' | grep -vE '^\+\+\+' \
            | grep -iE "$PATTERNS" | grep -viE "$ALLOWED" || true)
        if [ -n "$HITS" ]; then
            fail "$COMMITS 个待推送提交的新增行命中（仅列内容，请自行核对）:"
            echo "$HITS" | head -10 | sed 's/^/     → /'
        else
            pass "$COMMITS 个待推送提交的新增行未命中"
        fi
    fi
else
    warn "基线 $RANGE 不存在（跳过；可用 --range 指定）"
fi

# ---------------------------------------------------------------------------
# ③ 提交作者：不应出现个人邮箱
# ---------------------------------------------------------------------------
header "③ 提交作者邮箱"
if git rev-parse --verify --quiet "$RANGE" >/dev/null 2>&1; then
    BAD_AUTHORS=$(git log --format='%h %ae' "$RANGE..HEAD" 2>/dev/null \
        | grep -viE 'noreply\.github\.com$' || true)
    if [ -n "$BAD_AUTHORS" ]; then
        fail "存在非 GitHub noreply 邮箱:"
        echo "$BAD_AUTHORS" | sed 's/^/     → /'
    else
        pass "全部为 GitHub noreply"
    fi
fi

# ---------------------------------------------------------------------------
# ④ 不应被跟踪的文件
# ---------------------------------------------------------------------------
header "④ 不应被跟踪的文件（真实配置 / 密钥 / 凭据）"
BAD_FILES=$(git ls-files 2>/dev/null \
    | grep -iE '(^|/)\.env$|\.key$|\.pem$|\.token$|(^|/)config\.json$|\.secrets(/|$)|credential' || true)
if [ -n "$BAD_FILES" ]; then
    fail "以下文件不应进入版本控制:"
    echo "$BAD_FILES" | sed 's/^/     → /'
else
    pass "未被跟踪"
fi

# ---------------------------------------------------------------------------
# ⑤ tag 注解内容
# ---------------------------------------------------------------------------
header "⑤ tag 注解"
TAGS=$(git tag --list 2>/dev/null)
if [ -z "$TAGS" ]; then
    warn "仓库无 tag"
else
    TAG_HITS=""
    for t in $TAGS; do
        HIT=$(git tag -n99 "$t" 2>/dev/null | grep -iE "$PATTERNS" | grep -viE "$ALLOWED" || true)
        [ -n "$HIT" ] && TAG_HITS="$TAG_HITS$HIT"$'\n'
    done
    if [ -n "$TAG_HITS" ]; then
        fail "tag 注解命中:"
        echo "$TAG_HITS" | sed 's/^/     → /'
    else
        pass "$(echo "$TAGS" | wc -l) 个 tag 的注解均未命中"
    fi
fi

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
echo
if [ "$FAILED" -eq 0 ]; then
    echo -e "${GREEN}✅ 发布前审计通过${NC}"
    exit 0
fi
echo -e "${RED}❌ 发布前审计未通过 —— 请先处理上述命中项再推送/发版${NC}"
echo "   如确认误报，可把该域/邮箱加入本脚本的 ALLOWED 白名单。"
exit 1
