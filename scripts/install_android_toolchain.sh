#!/usr/bin/env bash
#
# Install the host mobile toolchain IOSForge needs for the emulator + crawl stage:
#   JDK 17, Android SDK (cmdline-tools/platform-tools/emulator/build-tools),
#   an x86_64 Google-APIs system image, an AVD named "mvp", and Flutter (stable).
#
# Idempotent: re-running skips components that are already present.
# Target: Ubuntu 24.04 x86_64, headless, /dev/kvm available.
#
# Usage:  sudo bash scripts/install_android_toolchain.sh
#
set -euo pipefail

ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-/opt/android-sdk}"
FLUTTER_ROOT="${FLUTTER_ROOT:-/opt/flutter}"
API_LEVEL="${API_LEVEL:-34}"
BUILD_TOOLS="${BUILD_TOOLS:-34.0.0}"
SYSTEM_IMAGE="${SYSTEM_IMAGE:-system-images;android-${API_LEVEL};google_apis;x86_64}"
AVD_NAME="${AVD_NAME:-mvp}"
AVD_DEVICE="${AVD_DEVICE:-pixel_6}"
CMDLINE_TOOLS_VER="${CMDLINE_TOOLS_VER:-11076708}"
CMDLINE_TOOLS_URL="https://dl.google.com/android/repository/commandlinetools-linux-${CMDLINE_TOOLS_VER}_latest.zip"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "This script must run as root (installs system packages under /opt)." >&2
    exit 1
  fi
}

install_apt_deps() {
  log "Installing system packages (JDK 17, unzip, emulator runtime libs)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq \
    openjdk-17-jdk-headless unzip wget curl ca-certificates git \
    libgl1-mesa-dri libnss3 libxcursor1 libxdamage1 libxcomposite1 \
    libxi6 libxtst6 libpulse0 libasound2t64 libxrandr2 libxrender1 fontconfig
}

install_cmdline_tools() {
  local dest="${ANDROID_SDK_ROOT}/cmdline-tools/latest"
  if [[ -x "${dest}/bin/sdkmanager" ]]; then
    log "cmdline-tools already present — skipping"
    return
  fi
  log "Downloading Android command-line tools (${CMDLINE_TOOLS_VER})"
  local tmp; tmp="$(mktemp -d)"
  wget -q -O "${tmp}/cmdline-tools.zip" "${CMDLINE_TOOLS_URL}"
  unzip -q "${tmp}/cmdline-tools.zip" -d "${tmp}"
  mkdir -p "${dest}"
  mv "${tmp}/cmdline-tools/"* "${dest}/"
  rm -rf "${tmp}"
}

sdk() { "${ANDROID_SDK_ROOT}/cmdline-tools/latest/bin/sdkmanager" --sdk_root="${ANDROID_SDK_ROOT}" "$@"; }

install_sdk_packages() {
  log "Accepting SDK licenses"
  yes | sdk --licenses >/dev/null || true
  log "Installing platform-tools, emulator, platform, build-tools, system image"
  sdk "platform-tools" "emulator" \
      "platforms;android-${API_LEVEL}" \
      "build-tools;${BUILD_TOOLS}" \
      "${SYSTEM_IMAGE}"
}

create_avd() {
  local avdmgr="${ANDROID_SDK_ROOT}/cmdline-tools/latest/bin/avdmanager"
  if "${avdmgr}" list avd 2>/dev/null | grep -q "Name: ${AVD_NAME}$"; then
    log "AVD '${AVD_NAME}' already exists — skipping"
    return
  fi
  log "Creating AVD '${AVD_NAME}' (${AVD_DEVICE}, ${SYSTEM_IMAGE})"
  echo "no" | "${avdmgr}" create avd \
    --name "${AVD_NAME}" \
    --package "${SYSTEM_IMAGE}" \
    --device "${AVD_DEVICE}" \
    --force
}

write_profile() {
  log "Writing /etc/profile.d/android-sdk.sh (ANDROID_SDK_ROOT + PATH)"
  cat > /etc/profile.d/android-sdk.sh <<EOF
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT}"
export ANDROID_HOME="${ANDROID_SDK_ROOT}"
export PATH="\${PATH}:${ANDROID_SDK_ROOT}/cmdline-tools/latest/bin:${ANDROID_SDK_ROOT}/platform-tools:${ANDROID_SDK_ROOT}/emulator:${FLUTTER_ROOT}/bin"
EOF
  chmod 0644 /etc/profile.d/android-sdk.sh
}

install_flutter() {
  if [[ -x "${FLUTTER_ROOT}/bin/flutter" ]]; then
    log "Flutter already present — skipping clone"
  else
    log "Cloning Flutter (stable) into ${FLUTTER_ROOT}"
    git clone --depth 1 -b stable https://github.com/flutter/flutter.git "${FLUTTER_ROOT}"
  fi
  git config --system --add safe.directory "${FLUTTER_ROOT}" || true
  log "Precaching Flutter Android artifacts"
  "${FLUTTER_ROOT}/bin/flutter" config --no-analytics >/dev/null || true
  "${FLUTTER_ROOT}/bin/flutter" precache --android --no-ios --no-web \
    --no-linux --no-windows --no-macos --no-fuchsia
}

verify() {
  log "Verification"
  "${ANDROID_SDK_ROOT}/platform-tools/adb" --version | head -1
  "${ANDROID_SDK_ROOT}/emulator/emulator" -list-avds
  ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT}" "${FLUTTER_ROOT}/bin/flutter" doctor -v \
    | sed -n '1,30p' || true
}

require_root
install_apt_deps
install_cmdline_tools
install_sdk_packages
create_avd
write_profile
install_flutter
verify

log "Done. Open a new shell (or 'source /etc/profile.d/android-sdk.sh') to load PATH."
