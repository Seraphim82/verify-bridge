#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERRO] python3 nao encontrado. Instale Python 3.10+ (ex.: sudo apt install python3)."
  exit 1
fi

if ! python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"; then
  echo "[ERRO] E necessario Python 3.10 ou superior."
  python3 --version
  exit 1
fi

exec python3 -m verify_bridge ui "$@"
