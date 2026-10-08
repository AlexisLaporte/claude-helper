#!/usr/bin/env bash
# Sets claude-helper up on this machine. Idempotent: safe to run again.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || { echo "claude-helper needs Python 3.11 or later" >&2; exit 1; }

if [ -d "$HOME/.codex" ]; then
  mkdir -p "$HOME/.codex/skills"
  ln -sfn "$REPO/codex/skills/claude-bridge" "$HOME/.codex/skills/claude-bridge"
  echo "→ Codex skill linked: ~/.codex/skills/claude-bridge"
fi

line="export PATH=\"$REPO/bin:\$PATH\""
if grep -qxF "$line" "$HOME/.bashrc" 2>/dev/null; then
  echo "= PATH already set in ~/.bashrc"
else
  cat <<EOF

Add this line at the very END of your shell startup file (~/.bashrc, and ~/.profile
if it puts ~/.local/bin back in front), so the \`claude\` wrapper comes before the
official binary:

  $line

EOF
fi

echo "Then check:  claude-helper profile doctor"
