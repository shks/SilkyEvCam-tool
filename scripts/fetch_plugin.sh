#!/bin/bash
# CenturyArks の SilkyEvCam HAL プラグインを取得して vendor/silky-hal/ に配置する。
#
# プラグインは CenturyArks の proprietary バイナリ（LICENSE_CA.txt: "may be used only
# when using CenturyArks's products or services"）なので、このリポジトリには含めない。
# 代わりにこのスクリプトで取得する。バイナリ版は登録フォーム不要で直接ダウンロードできる。
#
# 注意: OpenEB のバージョンと厳密に対応する。readme.txt にも
# "This pack is for Metavision version 5.2.0. Cannot be used with version 5.1.1 or earlier"
# と明記されている。OpenEB を上げるときは必ずこちらも上げること。
set -euo pipefail

VERSION="5.2.0"
URL="https://centuryarks.com/wp-content/uploads/2026/03/SilkyEvCam_Plugin_Installer_for_ubuntu_v${VERSION}.zip"

EVCAM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR="${EVCAM_ROOT}/vendor"
ZIP="${VENDOR}/SilkyEvCam_Plugin_Installer_for_ubuntu_v${VERSION}.zip"

OS_VERSION_ID="$(grep '^VERSION_ID=' /etc/os-release | cut -d'"' -f2)"

mkdir -p "${VENDOR}"

if [ ! -f "${ZIP}" ]; then
    echo "downloading ${URL}"
    curl -fL -o "${ZIP}" "${URL}"
fi

rm -rf "${VENDOR}/silkyevcam-plugin"
unzip -o -q "${ZIP}" -d "${VENDOR}/silkyevcam-plugin"

SRC="${VENDOR}/silkyevcam-plugin/SilkyEvCam_Plugin_Installer_for_ubuntu_v${VERSION}/SilkyEvCam_Plugin_Installer_for_ubuntu_v${VERSION}/resources"

if [ ! -d "${SRC}/${OS_VERSION_ID}" ]; then
    echo "ERROR: この OS バージョン (${OS_VERSION_ID}) 向けのバイナリが同梱されていません" >&2
    echo "       同梱されているのは: $(ls "${SRC}" | tr '\n' ' ')" >&2
    exit 1
fi

# 付属の CA_Silky_installer.sh は使わない。
# あれは ~/.bashrc に MV_HAL_PLUGIN_PATH を追記し /usr/lib と /usr/bin に
# system-wide でファイルを置くため、ローカルビルドツリー方式と衝突する。
mkdir -p "${VENDOR}/silky-hal/plugins" "${VENDOR}/silky-hal/bin"
cp -p "${SRC}/${OS_VERSION_ID}/libsilky_common_plugin.so" "${VENDOR}/silky-hal/plugins/"
cp -p "${SRC}/${OS_VERSION_ID}/silkyevcam_platform_info" "${VENDOR}/silky-hal/bin/"
cp -p "${SRC}/${OS_VERSION_ID}/silkyevcam_mask_pixel_util" "${VENDOR}/silky-hal/bin/"
chmod +x "${VENDOR}/silky-hal/bin/"*

echo
echo "配置しました:"
echo "  ${VENDOR}/silky-hal/plugins/libsilky_common_plugin.so"
echo "  ${VENDOR}/silky-hal/bin/"
echo
echo "udev ルールは別途 root 権限で入れる必要があります:"
echo "  sudo cp -p ${SRC}/ca_device.rules /etc/udev/rules.d/"
echo "  sudo udevadm control --reload-rules && sudo udevadm trigger"
