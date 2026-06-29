from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path


API_URL = "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest"
YT_DLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"


def main() -> None:
    destination = Path(sys.argv[1]).resolve()
    destination.mkdir(parents=True, exist_ok=True)

    with urllib.request.urlopen(API_URL) as response:
        payload = json.load(response)

    assets = payload.get("assets", [])
    chosen = None
    for asset in assets:
        name = str(asset.get("name", ""))
        if name.startswith("mpv-x86_64") and name.endswith(".7z"):
            chosen = asset
            break

    if chosen is None:
        raise SystemExit("Could not find a suitable mpv Windows build asset.")

    archive_path = destination / chosen["name"]
    print(f"Downloading {chosen['browser_download_url']}")
    urllib.request.urlretrieve(chosen["browser_download_url"], archive_path)
    print(f"Saved to {archive_path}")

    seven_zip = shutil.which("7z")
    if seven_zip is None:
        raise SystemExit("7z is required to extract the mpv archive.")

    import subprocess

    subprocess.run([seven_zip, "x", str(archive_path), f"-o{destination}"], check=True)
    archive_path.unlink(missing_ok=True)

    runtime_dir = destination / "runtime"
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    mpv_exe = None
    for path in destination.rglob("mpv.exe"):
        if "runtime" not in path.parts:
            mpv_exe = path
            break

    if mpv_exe is None:
        raise SystemExit("Could not locate mpv.exe after extraction.")

    source_dir = mpv_exe.parent
    for item in source_dir.iterdir():
        target = runtime_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)

    yt_dlp_path = runtime_dir / "yt-dlp.exe"
    print(f"Downloading {YT_DLP_URL}")
    urllib.request.urlretrieve(YT_DLP_URL, yt_dlp_path)
    print(f"Saved yt-dlp to {yt_dlp_path}")

    print(f"Prepared normalized mpv runtime in {runtime_dir}")


if __name__ == "__main__":
    main()
