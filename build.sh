#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="QuotaCard"
APP_DIR="${APP_DIR:-/Applications}"
APP_BUNDLE="${APP_DIR}/${APP_NAME}.app"

mkdir -p "${APP_BUNDLE}/Contents/MacOS" "${APP_BUNDLE}/Contents/Resources"

cat > "${APP_BUNDLE}/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>zh_CN</string>
  <key>CFBundleExecutable</key>
  <string>QuotaCard</string>
  <key>CFBundleIdentifier</key>
  <string>io.github.agent-quota-card</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>AI 限额看板</string>
  <key>CFBundleDisplayName</key>
  <string>AI 限额看板</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>1.1.0</string>
  <key>CFBundleVersion</key>
  <string>2</string>
  <key>LSMinimumSystemVersion</key>
  <string>13.0</string>
  <key>LSUIElement</key>
  <true/>
</dict>
</plist>
PLIST

cp "${ROOT_DIR}/collector/quota_collector.py" "${APP_BUNDLE}/Contents/Resources/quota_collector.py"
chmod +x "${APP_BUNDLE}/Contents/Resources/quota_collector.py"

swiftc -O \
  -framework AppKit \
  -framework SwiftUI \
  -framework Combine \
  -framework CoreGraphics \
  "${ROOT_DIR}/app/QuotaCard.swift" \
  -o "${APP_BUNDLE}/Contents/MacOS/${APP_NAME}"

codesign --force --deep --sign - "${APP_BUNDLE}"

echo "Built: ${APP_BUNDLE}"
