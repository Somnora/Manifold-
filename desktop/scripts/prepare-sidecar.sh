#!/usr/bin/env bash
# Stage the PyInstaller backend where Tauri's externalBin expects it:
# src-tauri/binaries/manifold-backend-<host-target-triple>.
# Build the backend first:  cd backend && uv run pyinstaller ... (see
# docs/desktop-build.md) or scripts/build-backend.sh from the repo root.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"           # desktop/
repo="$(cd "$here/.." && pwd)"
triple="$(rustc -vV | sed -n 's/^host: //p')"
src="$repo/backend/dist/manifold-backend"
[ -f "$src" ] || { echo "missing $src - build the backend binary first" >&2; exit 1; }
mkdir -p "$here/src-tauri/binaries"
cp "$src" "$here/src-tauri/binaries/manifold-backend-$triple"
echo "staged manifold-backend-$triple"
