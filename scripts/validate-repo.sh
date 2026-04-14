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

UV_CACHE_DIR="${uv_cache_dir}" "${uv_bin}" sync --python "${python_version}" --group dev --extra vm
UV_CACHE_DIR="${uv_cache_dir}" "${uv_bin}" run --python "${python_version}" python build_proto.py
git diff --exit-code -- src/cometbft src/gogoproto src/tendermint
UV_CACHE_DIR="${uv_cache_dir}" "${uv_bin}" run --python "${python_version}" ruff check .
UV_CACHE_DIR="${uv_cache_dir}" "${uv_bin}" run --python "${python_version}" ruff format --check .

pytest_args=("$@")
if [[ "${XIAN_ABCI_COVERAGE:-0}" == "1" ]]; then
  coverage_args=(
    --cov=src/abci
    --cov=src/xian
    --cov-report=term-missing:skip-covered
    --cov-report=xml
  )
  if ((${#pytest_args[@]})); then
    pytest_args=("${coverage_args[@]}" "${pytest_args[@]}")
  else
    pytest_args=("${coverage_args[@]}")
  fi
fi

if ((${#pytest_args[@]})); then
  UV_CACHE_DIR="${uv_cache_dir}" "${uv_bin}" run --python "${python_version}" pytest "${pytest_args[@]}"
else
  UV_CACHE_DIR="${uv_cache_dir}" "${uv_bin}" run --python "${python_version}" pytest
fi
