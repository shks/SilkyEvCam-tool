#!/bin/bash
# src/ 以下の C++ ツールをビルドする。
# OpenEB は system install していないので、ビルドツリーの CMake config を直接指す。
set -euo pipefail

EVCAM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GEN="${EVCAM_ROOT}/openeb/build/generated/share/cmake"

cmake -S "${EVCAM_ROOT}/src" -B "${EVCAM_ROOT}/build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DMetavisionSDK_DIR="${GEN}/MetavisionSDKCMakePackagesFilesDir" \
    -DMetavisionHAL_DIR="${GEN}/MetavisionHALCMakePackagesFilesDir" \
    -Dhdf5_ecf_DIR="${EVCAM_ROOT}/openeb/build/sdk/modules/stream/cpp/3rdparty/hdf5_ecf" \
    -Wno-dev

cmake --build "${EVCAM_ROOT}/build" -- -j"$(nproc)"

echo
echo "built: ${EVCAM_ROOT}/build/latency_probe"
