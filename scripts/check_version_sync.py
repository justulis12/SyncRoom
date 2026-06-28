from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INIT_PATH = ROOT / "src" / "syncroom" / "__init__.py"
PYPROJECT_PATH = ROOT / "pyproject.toml"
ISS_PATH = ROOT / "packaging" / "windows" / "SyncRoom.iss"


def read_init_version() -> str:
    match = re.search(r'__version__\s*=\s*"([^"]+)"', INIT_PATH.read_text(encoding="utf-8"))
    if not match:
        raise RuntimeError(f"Could not find __version__ in {INIT_PATH}")
    return match.group(1)


def read_pyproject_version() -> str:
    payload = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    return str(payload["project"]["version"])


def read_iss_version() -> str:
    match = re.search(r'#define\s+MyAppVersion\s+"([^"]+)"', ISS_PATH.read_text(encoding="utf-8"))
    if not match:
        raise RuntimeError(f"Could not find MyAppVersion in {ISS_PATH}")
    return match.group(1)


def main() -> int:
    versions = {
        str(INIT_PATH.relative_to(ROOT)): read_init_version(),
        str(PYPROJECT_PATH.relative_to(ROOT)): read_pyproject_version(),
        str(ISS_PATH.relative_to(ROOT)): read_iss_version(),
    }
    unique_versions = set(versions.values())
    if len(unique_versions) != 1:
        print("Version mismatch detected:", file=sys.stderr)
        for path, version in versions.items():
            print(f"  {path}: {version}", file=sys.stderr)
        return 1

    version = unique_versions.pop()
    print(f"Version sync OK: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
