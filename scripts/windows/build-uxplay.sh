#!/bin/bash
# Build UxPlay (the AirPlay receiver) for Windows in MSYS2's UCRT64 environment and
# install it to C:\msys64\ucrt64\bin. Run by scripts/windows/install.ps1; to run it by
# hand, open "MSYS2 UCRT64" and: bash scripts/windows/build-uxplay.sh
#
# Pinned to a commit that was tested with phoneuse. 1.74 has its own mDNS responder,
# so no Bonjour SDK is needed.
#
# UxPlay logs with printf and never flushes; on Windows a pipe makes stdout fully
# buffered, so phoneuse would see "connection request from <phone>" only kilobytes
# later. A forced-include header turns stdout buffering off before main(): a compiler
# flag, not a change to UxPlay's source.
set -euo pipefail
REV=${UXPLAY_REV:-05b0275cb1b726592700e47a6e0307544b3d3a52}
SRC=${UXPLAY_SRC:-$HOME/src/UxPlay}
P=mingw-w64-ucrt-x86_64

pacman -S --needed --noconfirm --disable-download-timeout git \
  $P-gcc $P-cmake $P-ninja $P-pkgconf $P-libplist $P-openssl \
  $P-gstreamer $P-gst-plugins-base $P-gst-plugins-good $P-gst-plugins-bad $P-gst-libav

if [ ! -d "$SRC/.git" ]; then
  mkdir -p "$(dirname "$SRC")"
  git clone https://github.com/FDH2/UxPlay "$SRC"
fi
cd "$SRC"
git fetch -q origin
git checkout -q "$REV"

UNBUF="$SRC/build/phoneuse-unbuffered.h"
mkdir -p build
cat > "$UNBUF" <<'EOF'
/* phoneuse: unbuffered stdout, so a parent process reads each log line at once. */
#include <stdio.h>
static void __attribute__((constructor)) phoneuse_unbuffered_stdout(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
}
EOF
cd build
UNBUF_W=$(cygpath -m "$UNBUF")  # gcc is a native Windows program: give it a Windows path
rm -f CMakeCache.txt
cmake -G Ninja .. -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_FLAGS="-include $UNBUF_W" -DCMAKE_CXX_FLAGS="-include $UNBUF_W"
ninja
cmake --install . --prefix /ucrt64
echo "uxplay installed: $(cygpath -w /ucrt64/bin/uxplay.exe)"
