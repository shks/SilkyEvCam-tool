#!/bin/bash
# EvCam 環境設定 — 使い方: リポジトリ直下で source ./env.sh
#
# ~/.bashrc は変更しない方針（ローカルビルドツリー方式）。
# このスクリプトを source したシェルでのみ OpenEB + SilkyEvCam が使える。

EVCAM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVCAM_OPENEB_BUILD="${EVCAM_ROOT}/openeb/build"
EVCAM_SILKY_HAL="${EVCAM_ROOT}/vendor/silky-hal"

if [ ! -f "${EVCAM_OPENEB_BUILD}/utils/scripts/setup_env.sh" ]; then
    echo "ERROR: OpenEB がまだビルドされていません: ${EVCAM_OPENEB_BUILD}" >&2
    return 1 2>/dev/null || exit 1
fi

# OpenEB のビルドツリー用環境設定。
# PATH / MV_HAL_PLUGIN_PATH / PYTHONPATH / HDF5_PLUGIN_PATH を設定する。
# 注意: MV_HAL_PLUGIN_PATH を「上書き」するので、Silky プラグインの追加は必ずこの後で行う。
. "${EVCAM_OPENEB_BUILD}/utils/scripts/setup_env.sh"

# CenturyArks SilkyEvCam の HAL プラグインを追加（上書きではなく追記）
export MV_HAL_PLUGIN_PATH="${MV_HAL_PLUGIN_PATH:-}:${EVCAM_SILKY_HAL}/plugins"

# silkyevcam_platform_info / silkyevcam_mask_pixel_util を PATH へ
export PATH="${EVCAM_SILKY_HAL}/bin:${PATH:-}"

# Silky プラグインは libmetavision_hal.so.5 / libmetavision_sdk_base.so.5 に依存する。
# ビルドツリーの lib を明示的に見せておく（vendor 製ツール単体実行時の保険）。
# ${VAR:-} で受ける。set -u のスクリプトから source されても落ちないようにする
export LD_LIBRARY_PATH="${EVCAM_OPENEB_BUILD}/lib:${LD_LIBRARY_PATH:-}"

# Python venv（PEP 668 回避のためシステム Python は汚さない）
if [ -f "${EVCAM_ROOT}/.venv/bin/activate" ]; then
    . "${EVCAM_ROOT}/.venv/bin/activate"
fi

echo "EvCam env ready"
echo "  MV_HAL_PLUGIN_PATH = ${MV_HAL_PLUGIN_PATH}"
echo "  python             = $(command -v python)"
