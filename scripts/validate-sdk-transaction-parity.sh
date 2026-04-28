#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
uv_cache_dir="${UV_CACHE_DIR:-/tmp/uv-cache}"
python_version="${XIAN_ABCI_VALIDATE_PYTHON:-3.14}"
xian_py_dir="${XIAN_PY_DIR:-${repo_root}/../xian-py}"
xian_js_dir="${XIAN_JS_DIR:-${repo_root}/../xian-js}"

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

if [[ ! -d "${xian_py_dir}" ]]; then
  printf 'xian-py checkout not found at %s\n' "${xian_py_dir}" >&2
  exit 1
fi
if [[ ! -d "${xian_js_dir}" ]]; then
  printf 'xian-js checkout not found at %s\n' "${xian_js_dir}" >&2
  exit 1
fi

cd "${repo_root}"

UV_CACHE_DIR="${uv_cache_dir}" "${uv_bin}" sync \
  --python "${python_version}" \
  --group dev \
  --extra native \
  --reinstall-package xian-tech-fastpath-core
UV_CACHE_DIR="${uv_cache_dir}" "${uv_bin}" sync \
  --project "${xian_py_dir}" \
  --python "${python_version}" \
  --group dev
npm ci --prefix "${xian_js_dir}"
npm run build --prefix "${xian_js_dir}" --workspace @xian-tech/types
npm run build --prefix "${xian_js_dir}" --workspace @xian-tech/client

XIAN_SDK_TRANSACTION_PARITY=1 \
XIAN_PY_DIR="${xian_py_dir}" \
XIAN_JS_DIR="${xian_js_dir}" \
UV_CACHE_DIR="${uv_cache_dir}" \
"${uv_bin}" run --python "${python_version}" \
  pytest tests/unit/test_sdk_transaction_parity.py
