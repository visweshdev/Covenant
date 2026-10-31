#!/bin/sh
# Install Covenant with pipx (its own environment, `covenant` on your PATH).
#   sh install.sh            # the latest release from PyPI
#   sh install.sh --github   # the newest code from GitHub (main branch)
set -eu

REPO_URL="git+https://github.com/visweshdev/Covenant.git"
PY=$(command -v python3 || command -v python || true)
[ -n "$PY" ] || { echo "covenant: Python 3.11 or newer is needed (python.org)"; exit 1; }
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "covenant: Python 3.11 or newer is needed"; exit 1; }

if command -v pipx >/dev/null 2>&1; then
  PIPX=pipx
else
  echo "Installing pipx..."
  "$PY" -m pip install --user --quiet pipx
  "$PY" -m pipx ensurepath >/dev/null
  PIPX="$PY -m pipx"
fi

if [ "${1:-}" = "--github" ]; then
  $PIPX install --force "$REPO_URL"
else
  $PIPX install --force covenant-agents
fi

echo
echo "Done. In a repo:"
echo "  covenant compile --pack safe-refactor \"what you want the agent to do\""
echo "  covenant init"
echo "(Open a new terminal if 'covenant' isn't found yet.)"
