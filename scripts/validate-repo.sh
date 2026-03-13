#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
uv_cache_dir="${UV_CACHE_DIR:-/tmp/uv-cache}"

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

UV_CACHE_DIR="${uv_cache_dir}" "${uv_bin}" sync --group dev
UV_CACHE_DIR="${uv_cache_dir}" "${uv_bin}" run ruff check .
UV_CACHE_DIR="${uv_cache_dir}" "${uv_bin}" run ruff format --check .

pytest_args=("$@")
if [[ "${XIAN_ABCI_COVERAGE:-0}" == "1" ]]; then
  pytest_args=(
    --cov=src/abci
    --cov=src/xian
    --cov-report=term-missing:skip-covered
    --cov-report=xml
    "${pytest_args[@]}"
  )
fi

UV_CACHE_DIR="${uv_cache_dir}" "${uv_bin}" run pytest "${pytest_args[@]}"
