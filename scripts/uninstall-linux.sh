#!/usr/bin/env bash
set -euo pipefail

rm -rf "${HOME}/.local/share/syncroom"
rm -f "${HOME}/.local/bin/syncroom"
rm -f "${HOME}/.local/share/applications/syncroom.desktop"
rm -f "${HOME}/.local/share/icons/hicolor/scalable/apps/syncroom.svg"

echo "SyncRoom removed from your user account."
