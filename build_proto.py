"""Generate checked-in Python protobuf stubs for the vendored CometBFT schemas."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
PROTO_ROOT = REPO_ROOT / "protos"
OUTPUT_ROOT = REPO_ROOT / "src"
GRPCIO_TOOLS_REQUIREMENT = "grpcio-tools==1.78.0"


def protoc_command_prefix() -> list[str]:
    try:
        grpc_tools_spec = importlib.util.find_spec("grpc_tools.protoc")
    except ModuleNotFoundError:
        grpc_tools_spec = None

    if grpc_tools_spec is not None:
        return [sys.executable, "-m", "grpc_tools.protoc"]

    uvx = shutil.which("uvx")
    if uvx is not None:
        return [
            uvx,
            "--from",
            GRPCIO_TOOLS_REQUIREMENT,
            "python",
            "-m",
            "grpc_tools.protoc",
        ]

    uv = shutil.which("uv")
    if uv is not None:  # pragma: no cover - compatibility fallback
        return [
            uv,
            "tool",
            "run",
            "--from",
            GRPCIO_TOOLS_REQUIREMENT,
            "python",
            "-m",
            "grpc_tools.protoc",
        ]

    raise SystemExit(
        "grpcio-tools is required to build protobuf stubs. "
        "Install it in the active environment or ensure `uv`/`uvx` is available."
    )


def protoc_environment(protoc_prefix: list[str]) -> dict[str, str] | None:
    command_name = Path(protoc_prefix[0]).name
    if command_name not in {"uv", "uvx"}:
        return None

    cache_dir = REPO_ROOT / ".tmp" / "uv-proto-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = str(cache_dir)
    return env


def iter_proto_files() -> list[Path]:
    return sorted(PROTO_ROOT.rglob("*.proto"))


def expected_output(source: Path) -> Path:
    relative = source.relative_to(PROTO_ROOT)
    return OUTPUT_ROOT / relative.with_name(f"{relative.stem}_pb2.py")


def generate_proto(source: Path, protoc_prefix: list[str]) -> None:
    if not source.exists():
        raise SystemExit(f"Can't find required file: {source}")

    output = expected_output(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    print(f" ~ Generating: {output.relative_to(REPO_ROOT)}...")

    command = [
        *protoc_prefix,
        f"-I{PROTO_ROOT}",
        f"-I{REPO_ROOT}",
        f"--python_out={OUTPUT_ROOT}",
        str(source),
    ]
    subprocess.run(
        command,
        check=True,
        cwd=REPO_ROOT,
        env=protoc_environment(protoc_prefix),
    )


def main() -> int:
    protoc_prefix = protoc_command_prefix()

    for source in iter_proto_files():
        generate_proto(source, protoc_prefix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
