#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
uv_cache_dir="${UV_CACHE_DIR:-/tmp/uv-cache}"
python_version="${XIAN_ABCI_VALIDATE_PYTHON:-3.14}"

if command -v uv >/dev/null 2>&1; then
  uv_bin="uv"
elif [[ -x "${repo_root}/.venv/bin/uv" ]]; then
  uv_bin="${repo_root}/.venv/bin/uv"
elif [[ -x "${repo_root}/../xian-cli/.venv/bin/uv" ]]; then
  uv_bin="${repo_root}/../xian-cli/.venv/bin/uv"
else
  printf 'uv is required but was not found\n' >&2
  exit 1
fi

cd "${repo_root}"

UV_CACHE_DIR="${uv_cache_dir}" "${uv_bin}" sync \
  --python "${python_version}" \
  --group dev \
  --extra native \
  --reinstall-package xian-tech-fastpath-core
UV_CACHE_DIR="${uv_cache_dir}" "${uv_bin}" run --python "${python_version}" \
  pytest tests/unit/test_native_fastpath_parity.py
