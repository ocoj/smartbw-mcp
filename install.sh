#!/bin/bash
# SmartBW MCP — 一键安装脚本
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${BLUE}[$1/${TOTAL_STEPS}]${NC} $2"; }
ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="$HOME/.config/bitwarden-mcp"
DAEMON_DIR="$HOME/.smartbw-mcp"
TOTAL_STEPS=5

# ============================================================
# Step 0: 安全自检
# ============================================================
if [ "$CONFIG_DIR" = "$SCRIPT_DIR" ] || [[ "$CONFIG_DIR" == "$SCRIPT_DIR"* ]]; then
    err "配置目录不能在项目目录下。将使用默认: $CONFIG_DIR"
    exit 1
fi

echo ""
echo "========================================"
echo "  SmartBW MCP 安装"
echo "========================================"
echo ""

# ============================================================
# Step 1: 检查依赖
# ============================================================
log "1" "检查基础环境..."

MISSING=""
command -v python3 &>/dev/null || MISSING="$MISSING python3"
command -v node    &>/dev/null || MISSING="$MISSING node"
command -v npm     &>/dev/null || MISSING="$MISSING npm"

if [ -n "$MISSING" ]; then
    err "缺少必要程序:$MISSING"
    echo "  Ubuntu/Debian: sudo apt install python3 nodejs npm"
    echo "  CentOS/RHEL:   sudo yum install python3 nodejs npm"
    exit 1
fi
ok "python3 $(python3 --version 2>&1 | awk '{print $2}'), node $(node --version), npm $(npm --version)"

# ============================================================
# Step 2: 安装 npm 依赖
# ============================================================
log "2" "安装 npm 依赖 (bw CLI + MCP Server)..."

NEED_NPM=""
npm list -g --depth=0 2>/dev/null | grep -q "@bitwarden/cli"        || NEED_NPM="$NEED_NPM @bitwarden/cli"
npm list -g --depth=0 2>/dev/null | grep -q "@bitwarden/mcp-server" || NEED_NPM="$NEED_NPM @bitwarden/mcp-server"

if [ -n "$NEED_NPM" ]; then
    npm install -g $NEED_NPM && ok "npm 依赖安装完成" || { err "npm 安装失败"; exit 1; }
else
    ok "npm 依赖已就绪"
fi

# Python 依赖
if python3 -c "import cryptography" 2>/dev/null; then
    ok "Python cryptography 已安装"
else
    log "2" "安装 Python cryptography..."
    pip3 install cryptography 2>/dev/null || python3 -m pip install cryptography 2>/dev/null || \
        warn "cryptography 安装失败（不影响基本使用，凭据将以明文存储）"
fi

# ============================================================
# Step 3: 配置凭据
# ============================================================
log "3" "配置凭据..."

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

if [ -f "$CONFIG_DIR/config.json" ]; then
    ok "检测到已有 config.json，跳过"
else
    echo ""
    echo "  请提供 Vaultwarden 连接信息（后续可编辑 $CONFIG_DIR/config.json 修改）:"
    echo ""
    read -p "  服务器地址 (如 https://vault.example.com): " BW_HOST
    read -p "  登录邮箱: " BW_EMAIL
    read -s -p "  主密码 (不显示): " BW_MASTER_PASSWORD
    echo ""
    read -p "  API Key (推荐, 直接回车跳过): " BW_API_KEY

    cat > "$CONFIG_DIR/config.json" << EOF
{
  "bw_host": "$BW_HOST",
  "email": "$BW_EMAIL",
  "master_password": "$BW_MASTER_PASSWORD",
  "client_id": "$BW_API_KEY",
  "client_secret": "",
  "mcp_server_path": "",
  "connection_timeout_seconds": 15
}
EOF
    chmod 600 "$CONFIG_DIR/config.json"
    ok "配置已保存: $CONFIG_DIR/config.json"
fi

# ============================================================
# Step 4: 启动守护进程
# ============================================================
log "4" "启动守护进程..."

mkdir -p "$DAEMON_DIR"

if command -v systemctl &>/dev/null && systemctl --user daemon-reload 2>/dev/null; then
    # systemd 模式
    SERVICE_FILE="$HOME/.config/systemd/user/smartbw-daemon.service"
    mkdir -p "$(dirname "$SERVICE_FILE")"
    cat > "$SERVICE_FILE" << SERVICE_EOF
[Unit]
Description=SmartBW MCP Daemon - Vaultwarden 代理守护进程
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $SCRIPT_DIR/mcp_daemon.py
Restart=always
RestartSec=10
StandardOutput=append:%h/.smartbw-mcp/daemon.log
StandardError=append:%h/.smartbw-mcp/daemon.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
SERVICE_EOF

    systemctl --user daemon-reload
    systemctl --user enable --now smartbw-daemon 2>/dev/null || {
        warn "systemd 启动失败，尝试后台模式..."
        nohup python3 "$SCRIPT_DIR/mcp_daemon.py" > "$DAEMON_DIR/daemon.log" 2>&1 &
        echo $! > "$DAEMON_DIR/daemon.pid"
    }
    sleep 2
    if systemctl --user is-active smartbw-daemon >/dev/null 2>&1; then
        ok "daemon 已通过 systemd 启动"
    fi
else
    # 后台进程模式
    nohup python3 "$SCRIPT_DIR/mcp_daemon.py" > "$DAEMON_DIR/daemon.log" 2>&1 &
    echo $! > "$DAEMON_DIR/daemon.pid"
    ok "daemon 已后台启动 (PID $(cat $DAEMON_DIR/daemon.pid))"
fi

# ============================================================
# Step 5: MCP 客户端配置提示
# ============================================================
log "5" "MCP 客户端配置"

cat << 'EOF'

  ╔══════════════════════════════════════════════╗
  ║  在你的 MCP 客户端配置中添加：                ║
  ╠══════════════════════════════════════════════╣
  ║                                              ║
  ║  "smartbw": {                                ║
  ║    "command": "python3",                     ║
  ║    "args": ["smartbw_mcp_server.py的路径"]    ║
  ║  }                                           ║
  ║                                              ║
  ║  具体路径和格式因客户端而异，                   ║
  ║  详见 docs/integrations/ 目录                 ║
  ║                                              ║
  ╚══════════════════════════════════════════════╝

EOF

echo ""
echo "========================================"
echo "  ✅ 安装完成"
echo ""
echo "  日志: tail -f ~/.smartbw-mcp/daemon.log"
echo "========================================"
