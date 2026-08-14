#!/bin/bash
# OpenEB 5.2.0 をローカルビルドツリー方式でビルドする（install はしない）。
#
# README の手順 3 を実行可能にしたもの。母艦（x86-64 + CUDA）と
# Raspberry Pi（aarch64 + CUDA 無し）の両方で同じものを使う。
# 違いは nvcc の有無だけなので、あれば渡す・なければ渡さない。
set -euo pipefail

EVCAM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="${EVCAM_ROOT}/.venv/bin/python"

if [ ! -x "${VENV_PYTHON}" ]; then
    echo "ERROR: venv がありません: ${VENV_PYTHON}" >&2
    echo "       python3 -m venv .venv して依存を入れること（README 手順 2）" >&2
    exit 1
fi

if [ ! -d "${EVCAM_ROOT}/openeb" ]; then
    echo "ERROR: openeb/ がありません。README 手順 3 の git clone を先に。" >&2
    exit 1
fi

CMAKE_ARGS=(
    -S "${EVCAM_ROOT}/openeb"
    -B "${EVCAM_ROOT}/openeb/build"
    -DCMAKE_BUILD_TYPE=Release
    -DBUILD_TESTING=OFF
    -DCOMPILE_PYTHON3_BINDINGS=ON
    -DPython3_EXECUTABLE="${VENV_PYTHON}"
    -Dpybind11_DIR="$("${VENV_PYTHON}" -c 'import pybind11; print(pybind11.get_cmake_dir())')"
    -Wno-dev
)

# CUDA は任意。Pi には無いので、nvcc があるときだけ渡す。
if [ -x /usr/local/cuda/bin/nvcc ]; then
    CMAKE_ARGS+=(-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc)
fi

cmake "${CMAKE_ARGS[@]}"

# メモリの少ない機体（Pi 8GB / 4 コア）では -j$(nproc) で OOM することがある。
# EVCAM_BUILD_JOBS で絞れるようにしておく。
cmake --build "${EVCAM_ROOT}/openeb/build" -- -j"${EVCAM_BUILD_JOBS:-$(nproc)}"

echo
echo "built: ${EVCAM_ROOT}/openeb/build"
