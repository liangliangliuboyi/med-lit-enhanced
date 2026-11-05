#!/usr/bin/env bash
# medlit-agent 安装脚本（macOS / Linux / Git Bash）
#
#   ./install/install.sh                    # 自动检测可用的 agent
#   ./install/install.sh workbuddy          # 指定目标
#   ./install/install.sh claude-code
#   ./install/install.sh cursor
#   ./install/install.sh ~/.myagent/skills  # 或者直接给一个目录
#
# 脚本只做一件事：把 skills/medlit-agent/ 复制到目标 agent 的 skills 目录。
# 密钥需要你之后自己填（脚本会提示）。

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_DIR/skills/medlit-agent"
SKILL_NAME="medlit-agent"

if [ ! -f "$SRC/SKILL.md" ]; then
  echo "✗ 找不到 $SRC/SKILL.md，请在仓库根目录下运行本脚本。" >&2
  exit 1
fi

target="${1:-auto}"
dest=""
case "$target" in
  auto)
    for cand in "$HOME/.workbuddy/skills" "$HOME/.claude/skills" "$HOME/.agents/skills" "$HOME/.codex/skills"; do
      if [ -d "$(dirname "$cand")" ]; then dest="$cand"; break; fi
    done
    if [ -z "$dest" ]; then dest="$HOME/.agents/skills"; fi
    ;;
  workbuddy)     dest="$HOME/.workbuddy/skills" ;;
  claude-code)   dest="$HOME/.claude/skills" ;;
  codex)         dest="$HOME/.agents/skills" ;;
  cursor)        dest="$HOME/.cursor/skills" ;;
  *)             dest="$target" ;;
esac

mkdir -p "$dest"
if [ -e "$dest/$SKILL_NAME" ]; then
  echo "→ 已存在 $dest/$SKILL_NAME，先备份为 .bak-$(date +%Y%m%d-%H%M)"
  mv "$dest/$SKILL_NAME" "$dest/$SKILL_NAME.bak-$(date +%Y%m%d-%H%M)"
fi
cp -r "$SRC" "$dest/$SKILL_NAME"

echo "✓ 已安装到 $dest/$SKILL_NAME"

# 生成 .env 模板（不覆盖已有的）
if [ ! -f "$dest/$SKILL_NAME/.env" ]; then
  cp "$dest/$SKILL_NAME/.env.example" "$dest/$SKILL_NAME/.env"
  echo "→ 已生成 .env 模板：$dest/$SKILL_NAME/.env"
fi

cat <<EOF

接下来请编辑 $dest/$SKILL_NAME/.env，填入：
  SILICONFLOW_API_KEY=   （必填，模型服务密钥，例如硅基流动）
  NCBI_API_KEY=          （选填，有则限速从 3/s 提到 10/s）
  NCBI_EMAIL=            （建议填你自己的邮箱）

然后自检：
  python "$dest/$SKILL_NAME/scripts/selftest.py"

EOF
