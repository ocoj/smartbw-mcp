#!/bin/bash
# SmartBW MCP — 提交前检查：确保开发目录不存在真实配置文件
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'

# 可疑的配置文件名（排除 .example 模板）
FORBIDDEN_FILES=$(find "$SCRIPT_DIR" \
    -maxdepth 5 \
    -not -path '*/.git/*' \
    -not -path '*/__pycache__/*' \
    -not -path '*/.pytest_cache/*' \
    -not -path '*/node_modules/*' \
    \( \
        -name 'config.json' \
        -o -name 'config.yml' \
        -o -name 'config.yaml' \
        -o -name '.env' \
        -o -name '*.token' \
        -o -name '*.key' \
        -o -name '*.pem' \
    \) \
    -not -name '*.example*' \
    -not -name 'config.example.json' \
    2>/dev/null)

if [ -n "$FORBIDDEN_FILES" ]; then
    echo -e "${RED}❌ 提交被阻止！发现疑似真实配置文件:${NC}"
    echo "$FORBIDDEN_FILES" | while read f; do
        echo -e "   ${RED}→${NC} $f"
    done
    echo ""
    echo "  正确做法:"
    echo "    cp $SCRIPT_DIR/config.example.json ~/.config/bitwarden-mcp/config.json"
    echo "    然后删除开发目录中的真实配置文件"
    echo ""
    echo "  如确认是 false positive: git commit --no-verify"
    exit 1
fi

echo -e "${GREEN}✅ 配置安全检查通过${NC}"

# ============================================================
# 内容级脱敏扫描（只查文件名防不住内容里的真实地址/凭据）
#
# 原则：
#   - 只扫描 git 跟踪的文件（未跟踪的本地草稿不阻断提交）
#   - 命中时只打印「文件:行号」，**绝不打印匹配内容本身**
#   - 用白名单放行占位域与公共域，避免误报
# ============================================================
ALLOWED='example\.(com|org|net)|\.invalid|\.test|your[-_]|yourserver|vault\.example|vaultwarden\.example|localhost|127\.0\.0\.1|0\.0\.0\.0|bitwarden\.com|bitwarden\.net|github\.com|githubusercontent\.com|python\.org|pypi\.org|npmjs\.com|nodejs\.org|keepachangelog\.com|semver\.org|shields\.io|opensource\.org|ubuntu\.com|debian\.org|centos\.org|redhat\.com|systemd\.io|specifications\.freedesktop\.org|modelcontextprotocol\.io'

CONTENT_FAILED=0

scan_pattern() {
    local label="$1" pattern="$2"
    local hits
    hits=$(git -C "$SCRIPT_DIR" ls-files -z 2>/dev/null \
        | xargs -0 grep -nIiE "$pattern" 2>/dev/null \
        | grep -viE "$ALLOWED" || true)
    if [ -n "$hits" ]; then
        echo -e "${RED}❌ 内容扫描命中 [$label]（仅列位置，不显示内容）:${NC}"
        echo "$hits" | cut -d: -f1,2 | sed 's/^/   → /'
        CONTENT_FAILED=1
    fi
}

# 内网 IP 段
scan_pattern "内网 IP" '(^|[^0-9])(10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3})([^0-9]|$)'
# 常见凭据 token 前缀
scan_pattern "凭据 token 前缀" '(xox[baprs]-|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,})'
# 邮箱（白名单外）
scan_pattern "邮箱" '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
# 域名（白名单外）
scan_pattern "域名" 'https?://[A-Za-z0-9.-]+\.[A-Za-z]{2,}'

if [ "$CONTENT_FAILED" -ne 0 ]; then
    echo ""
    echo "  若确认为误报：把该域/邮箱加入本脚本的 ALLOWED 白名单，"
    echo "  或在特殊情况下使用 git commit --no-verify。"
    exit 1
fi

echo -e "${GREEN}✅ 内容脱敏扫描通过${NC}"
exit 0
