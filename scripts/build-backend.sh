#!/usr/bin/env bash
# Build the desktop backend binary: static-export the dashboard, then
# freeze backend + assets into one PyInstaller executable (backend/dist/).
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> dashboard static export"
cd "$repo/dashboard"
npm ci --no-fund --no-audit
npm run build            # next.config.ts has output: "export" -> out/

echo "==> pyinstaller freeze"
cd "$repo/backend"
uv sync --dev
SEP=":"; [ "${OS:-}" = "Windows_NT" ] && SEP=";"
uv run pyinstaller --noconfirm --clean --onefile --name manifold-backend \
  --add-data "../dashboard/out${SEP}ui" \
  --add-data "../templates${SEP}templates" \
  --add-data "../sidecar${SEP}sidecar" \
  --add-data "../config.yaml${SEP}." \
  --add-data "../docs/manifold-skill.md${SEP}docs" \
  desktop.py
echo "==> built backend/dist/manifold-backend"
