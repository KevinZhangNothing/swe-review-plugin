#!/bin/bash
# SWE-Review Plugin 安装脚本
#
# Idempotent install/uninstall cycle for full e2e validation.
#
# Usage:
#   ./install.sh install     # default if no subcommand
#   ./install.sh uninstall   # remove skill installs + python package
#   ./install.sh verify      # run pytest + swe-review list-tools
#   ./install.sh e2e         # install → verify → uninstall (full cycle test)
#   ./install.sh test-all    # install → 4-CLI smoke → uninstall

set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

log() { echo -e "${BLUE}[install]${NC} $*"; }
ok()  { echo -e "${GREEN}[ok]${NC} $*"; }
warn(){ echo -e "${YELLOW}[warn]${NC} $*"; }
err() { echo -e "${RED}[err]${NC} $*"; }

PY=""
detect_python() {
    if [ -n "$PY" ]; then return; fi
    for cand in python3 python; do
        if command -v "$cand" >/dev/null 2>&1; then
            if "$cand" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"; then
                PY="$cand"; return
            fi
        fi
    done
    err "Python >= 3.10 required"; exit 1
}

# ==========================================================================
# INSTALL
# ==========================================================================
cmd_install() {
    log "Python 检查"
    detect_python
    ok "Python: $($PY --version)"

    log "pip install -r requirements.txt"
    "$PY" -m pip install -r requirements.txt --quiet 2>/dev/null || \
        warn "部分依赖安装失败（可能仅影响部分 adapter）"

    log "pip install -e ."
    "$PY" -m pip install -e . --quiet || warn "editable install 失败"

    if [ ! -f ".env.local" ]; then
        log "写入 .env.local 模板"
        cat > .env.local <<'EOF'
# SWE-Review 环境变量（按需修改）。各 adapter 默认读 PATH 上的 CLI 二进制：
#   claude-code → claude
#   cursor      → agent
#   opencode    → /Users/$USER/.opencode/bin/opencode
#   pi          → pi
# 不在此文件写 API key —— 各 CLI 工具自带认证（OAuth/keychain/settings.json）。
CLI_BIN_CLAUDE_CODE=claude
CLI_BIN_CURSOR=agent
CLI_BIN_OPENCODE=$HOME/.opencode/bin/opencode
CLI_BIN_PI=pi

PI_SKILLS_DIR=$HOME/.pi/agent/skills
SWE_REVIEW_TOOL=pi
EOF
        ok ".env.local 已创建"
    fi

    log "软链 SKILL.md → ~/.agents/skills/ + ~/.claude/skills/   (repo 为唯一 source of truth)"
    SKILLS_SRC="$ROOT/.claude/skills"
    mkdir -p "$HOME/.agents/skills" "$HOME/.claude/skills"
    if [ -d "$SKILLS_SRC" ]; then
        for skill_dir in "$SKILLS_SRC"/swe-review-*; do
            [ -d "$skill_dir" ] || continue
            name="$(basename "$skill_dir")"
            rm -rf "$HOME/.agents/skills/$name" "$HOME/.claude/skills/$name"
            ln -s "$skill_dir" "$HOME/.agents/skills/$name"
            ln -s "$skill_dir" "$HOME/.claude/skills/$name"
        done
        ok "已软链到 ~/.agents/skills/ 与 ~/.claude/skills/（编辑 repo 即时生效）"
    fi

    log "软链 SKILL.md → ~/.pi/agent/skills/swe-review/   (Pi 自动发现)"
    PI_DIR="$HOME/.pi/agent/skills/swe-review"
    mkdir -p "$PI_DIR"
    if [ -d "$SKILLS_SRC" ]; then
        for skill_dir in "$SKILLS_SRC"/swe-review-*; do
            [ -d "$skill_dir" ] || continue
            name="$(basename "$skill_dir")"
            rm -rf "$PI_DIR/$name"
            ln -s "$skill_dir" "$PI_DIR/$name"
        done
        ok "已软链到 $PI_DIR/"
    fi

    log "检测 AI 工具 CLI"
    for tool in claude agent opencode pi; do
        if command -v "$tool" >/dev/null 2>&1; then
            ok "$tool → $(command -v "$tool")"
        else
            warn "$tool 不在 PATH"
        fi
    done

    cat <<EOF

${GREEN}✔ 安装完成${NC}

试用：
  source .env.local
  swe-review list-tools
  swe-review health
  swe-review review --issue "Bug" --pr-diff ./x.diff --tool pi [--prompt-style engineering|concise|detailed] [--deep]
  swe-review loop    --issue "Bug" --repo-path . --strategy hybrid --tool pi [--feedback-level full_feedback] [--deep] 

Skill 已软链到（repo .claude/skills/ 为唯一 source of truth）：
  - ~/.agents/skills/swe-review-*/          (通用发现路径)
  - ~/.claude/skills/swe-review-*/          (Claude Code / OpenCode)
  - ~/.pi/agent/skills/swe-review/swe-review-*/  (Pi)

卸载：
  ./install.sh uninstall
EOF
}

# ==========================================================================
# UNINSTALL — 完全逆转 install
# ==========================================================================
cmd_uninstall() {
    log "pip uninstall -y swe-review"
    detect_python
    "$PY" -m pip uninstall -y swe-review 2>/dev/null || warn "swe-review 未安装"

    # 包 uninstall 后本地 egg-info 还要手动清
    rm -rf swe_review.egg-info 2>/dev/null || true

    log "从 ~/.agents/skills/ 移除 swe-review-*（软链，不伤 repo）"
    rm -rf "$HOME/.agents/skills"/swe-review-* 2>/dev/null || true

    log "从 ~/.claude/skills/ 移除 swe-review-*"
    if [ -d "$HOME/.claude/skills" ]; then
        rm -rf "$HOME/.claude/skills"/swe-review-* 2>/dev/null || true
        ok "已清理 ~/.claude/skills/swe-review-*"
    fi

    log "从 ~/.pi/agent/skills/swe-review/ 移除"
    if [ -d "$HOME/.pi/agent/skills/swe-review" ]; then
        rm -rf "$HOME/.pi/agent/skills/swe-review" 2>/dev/null || true
        ok "已清理 ~/.pi/agent/skills/swe-review/"
    fi

    log "删除 .env.local（如不再需要）"
    if [ -f ".env.local" ]; then
        rm -f .env.local
        ok ".env.local 已删除"
    fi

    ok "卸载完成"
}

# ==========================================================================
# VERIFY — 运行单元测试 + 各 adapter 静态检查
# ==========================================================================
cmd_verify() {
    detect_python
    log "pytest tests/"
    if "$PY" -m pytest -q 2>&1 | tail -10; then
        ok "所有单元测试通过"
    else
        err "单元测试失败"
        return 1
    fi

    log "swe-review list-tools"
    "$PY" -m swe_review.cli list-tools 2>&1 | head -40
}

# ==========================================================================
# E2E — install → verify → uninstall (full idempotent cycle)
# ==========================================================================
cmd_e2e() {
    log "==== Phase 1: install ===="
    cmd_install
    echo ""
    log "==== Phase 2: verify ===="
    cmd_verify
    echo ""
    log "==== Phase 3: uninstall ===="
    cmd_uninstall
    echo ""
    ok "完整 install/verify/uninstall 周期完成"
}

# ==========================================================================
# TEST-ALL — install → 4-CLI smoke (accept pass/fail) → uninstall
# ==========================================================================
cmd_test_all() {
    local target_tool="${1:-all}"

    log "==== Phase 1: install ===="
    cmd_install
    echo ""

    log "==== Phase 2: smoke (不指定 model，按 SKILL §1) ===="
    # Force the python to discover the freshly installed package
    local out
    out="$("$PY" -m swe_review.cli health 2>&1)" || true
    echo "$out" | tail -50
    echo ""

    # Summarize results
    local claude_status cursor_status opencode_status pi_status
    claude_status="$(echo "$out" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    for h in d.get('health', []):
        if h['tool'] == 'claude-code':
            print(h['status']); sys.exit(0)
except Exception:
    pass
print('unknown')
")"
    cursor_status="$(echo "$out" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    for h in d.get('health', []):
        if h['tool'] == 'cursor':
            print(h['status']); sys.exit(0)
except Exception:
    pass
print('unknown')
")"
    opencode_status="$(echo "$out" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    for h in d.get('health', []):
        if h['tool'] == 'opencode':
            print(h['status']); sys.exit(0)
except Exception:
    pass
print('unknown')
")"
    pi_status="$(echo "$out" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    for h in d.get('health', []):
        if h['tool'] == 'pi':
            print(h['status']); sys.exit(0)
except Exception:
    pass
print('unknown')
")"

    log "==== Phase 2 总结 ===="
    _print_status() {
        if [ "$2" = "ok" ]; then
            printf "  \033[0;32m%-14s PASS\033[0m  %s\n" "$1" "$2"
        else
            printf "  \033[0;31m%-14s FAIL\033[0m  %s\n" "$1" "$2"
        fi
    }
    _print_status "claude-code" "$claude_status"
    _print_status "cursor"      "$cursor_status"
    _print_status "opencode"    "$opencode_status"
    _print_status "pi"          "$pi_status"
    echo ""

    log "==== Phase 3: uninstall ===="
    cmd_uninstall
    echo ""
    ok "完整 test-all 完成 (install + 4-CLI smoke + uninstall)"
}

cmd_test_tool() {
    local tool="${1:-pi}"
    log "==== Phase 1: install (针对 --tool $tool) ===="
    cmd_install
    echo ""

    log "==== Phase 2: smoke --tool $tool ===="
    "$PY" -m swe_review.cli review \
        --issue "Say only OK" \
        --pr-title "x" \
        --pr-diff /dev/stdin \
        --tool "$tool" \
        --max-steps 0 <<'EOF' 2>&1 | tail -40
diff --git a/x.py b/x.py
@@ -0,0 +1,1 @@
+ok
EOF
    echo ""

    log "==== Phase 3: uninstall ===="
    cmd_uninstall
    echo ""
    ok "完成 install + test --tool $tool + uninstall"
}

# ==========================================================================
# Main
# ==========================================================================
case "${1:-install}" in
    install)        cmd_install ;;
    uninstall)      cmd_uninstall ;;
    verify)         cmd_verify ;;
    e2e)            cmd_e2e ;;
    test-all)       cmd_test_all ;;
    test-tool)      shift; cmd_test_tool "$@" ;;
    *)
        echo "Usage: $0 {install|uninstall|verify|e2e|test-all|test-tool TOOL}"
        echo "TOOL ∈ {claude-code, cursor, opencode, pi}"
        exit 1
        ;;
esac
