#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

APP_ROOT="${HOME}/.local/share/syncroom"
BIN_DIR="${HOME}/.local/bin"
APP_BIN="${BIN_DIR}/syncroom"
DESKTOP_DIR="${HOME}/.local/share/applications"
ICON_DIR="${HOME}/.local/share/icons/hicolor/scalable/apps"
DESKTOP_FILE="${DESKTOP_DIR}/syncroom.desktop"
ICON_FILE="${ICON_DIR}/syncroom.svg"

mkdir -p "${APP_ROOT}" "${BIN_DIR}" "${DESKTOP_DIR}" "${ICON_DIR}"

if ! command -v python >/dev/null 2>&1; then
  echo "python is required."
  exit 1
fi

if ! command -v mpv >/dev/null 2>&1; then
  echo "mpv is required. On Arch/CachyOS run: sudo pacman -S mpv"
  exit 1
fi

python -m venv "${APP_ROOT}/venv"
"${APP_ROOT}/venv/bin/python" -m pip install --upgrade pip
"${APP_ROOT}/venv/bin/python" -m pip install "${REPO_ROOT}"

cat > "${APP_BIN}" <<EOF
#!/usr/bin/env bash
exec "${APP_ROOT}/venv/bin/syncroom-client" "\$@"
EOF
chmod +x "${APP_BIN}"

sed \
  -e "s|@EXEC_PATH@|${APP_BIN}|g" \
  -e "s|@ICON_PATH@|${ICON_FILE}|g" \
  "${REPO_ROOT}/packaging/linux/syncroom.desktop" > "${DESKTOP_FILE}"
cp "${REPO_ROOT}/assets/syncroom.svg" "${ICON_FILE}"
chmod 644 "${DESKTOP_FILE}" "${ICON_FILE}"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${DESKTOP_DIR}" >/dev/null 2>&1 || true
fi

echo "SyncRoom installed."
echo "Launcher: ${APP_BIN}"
echo "Desktop entry: ${DESKTOP_FILE}"
echo "If the app does not appear in your menu right away, log out and back in once."
