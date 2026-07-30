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
exit 0
