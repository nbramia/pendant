"""Generate Python proto bindings.

Run from the project root:

    uv run --with grpcio-tools python tools/gen_proto.py

The generated code refuses to import on a protobuf runtime older than the
protoc that produced it, so keep the pyproject.toml floor at or above the
"Protobuf Python Version" in the committed *_pb2.py files.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PROTO_DIR = ROOT / "proto"
OUT_DIR = ROOT / "pendant_direct" / "proto"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*_pb2.py"):
        old.unlink()
    for old in OUT_DIR.glob("*_pb2.pyi"):
        old.unlink()

    protos = sorted(PROTO_DIR.glob("*.proto"))
    if not protos:
        print(f"No .proto files in {PROTO_DIR}", file=sys.stderr)
        return 1

    cmd = [
        sys.executable, "-m", "grpc_tools.protoc",
        f"--proto_path={PROTO_DIR}",
        f"--python_out={OUT_DIR}",
        f"--pyi_out={OUT_DIR}",
        *[str(p) for p in protos],
    ]
    print(" ".join(cmd))
    rc = subprocess.call(cmd)
    if rc != 0:
        return rc

    # protoc emits flat imports (`import server_pb2 as server__pb2`) but
    # the modules live in a sub-package. Rewrite them to relative.
    proto_modules = {p.stem + "_pb2" for p in PROTO_DIR.glob("*.proto")}
    for f in OUT_DIR.glob("*_pb2.py"):
        text = f.read_text(encoding="utf-8")
        for mod in proto_modules:
            text = text.replace(f"\nimport {mod} ", f"\nfrom . import {mod} ")
        f.write_text(text, encoding="utf-8")

    # Make the package importable.
    init = OUT_DIR / "__init__.py"
    init.write_text("# auto-generated proto bindings\n", encoding="utf-8")

    # Print what we generated
    for f in sorted(OUT_DIR.glob("*_pb2.py")):
        print(f"  generated {f.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
