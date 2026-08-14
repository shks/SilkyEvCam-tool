#!/bin/bash
# 実験: OpenEB 同梱の Prophesee プラグインに SilkyEvCam の USB ID を登録して、
# CenturyArks の proprietary プラグイン無しでカメラが開けるか試す。
#
# 根拠と限界:
#   Treuzell の USB ID リストは既定で空で、tz_camera_discovery.h:46 に
#   「プラグイン同士が同じボードを取り合わないよう、担当ボードだけ登録する」と
#   意図が書かれている。つまりプラグインの差は「どの USB ID を名乗るか」に集約される。
#   SilkyEvCam VGA のセンサ世代 Gen3.1 は "psee,ccam5_fpga" として OpenEB に実装済み
#   （TzCcam5Gen31: VGA ジオメトリ・バイアス・ROI・トリガ入出力）。
#
#   ただし USB 照合を通っても、その先の Treuzell ハンドシェイクや初期化で
#   CenturyArks 独自の差分に当たって失敗する可能性は残る。あくまで実験。
#
# 使い方:
#   ./scripts/probe_silky_usb.sh            # まず条件を満たすか確認し、PID を得る
#   ./scripts/try_psee_plugin_with_silky.sh 0x1234 [subclass]
#
# 元に戻すには:
#   git -C openeb checkout -- hal_psee_plugins/src/plugin/psee_universal.cpp
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "usage: $0 <PID> [subclass]    例: $0 0x1234" >&2
    echo "  PID は scripts/probe_silky_usb.sh が表示します" >&2
    exit 1
fi

PID="$1"
[ "${PID#0x}" = "${PID}" ] && PID="0x${PID}"
SUBCLASS="${2:-0x19}"
VID="0x31f7"   # CenturyArks

EVCAM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${EVCAM_ROOT}/openeb/hal_psee_plugins/src/plugin/psee_universal.cpp"
ANCHOR="    tz_cam_discovery->add_usb_id(0x1FC9, 0x5838, 0x19);"

if [ ! -f "${SRC}" ]; then
    echo "ERROR: ${SRC} がありません。OpenEB を clone してください。" >&2
    exit 1
fi

if grep -q "0x31f7" "${SRC}"; then
    echo "すでにパッチ済みです:"
    grep -n "0x31f7" "${SRC}"
else
    if ! grep -qF "${ANCHOR}" "${SRC}"; then
        echo "ERROR: 挿入位置が見つかりません（OpenEB のバージョン差の可能性）。" >&2
        echo "       ${SRC} の add_usb_id 群の直後に手で 1 行足してください:" >&2
        echo "       tz_cam_discovery->add_usb_id(${VID}, ${PID}, ${SUBCLASS});" >&2
        exit 1
    fi
    # 「CenturyArks SilkyEvCam を試すための追加」と分かる形で 1 行だけ足す
    awk -v anchor="${ANCHOR}" -v line="    tz_cam_discovery->add_usb_id(${VID}, ${PID}, ${SUBCLASS});" '
        {print}
        $0 == anchor {print "    // 実験: CenturyArks SilkyEvCam (scripts/try_psee_plugin_with_silky.sh)"; print line}
    ' "${SRC}" > "${SRC}.tmp" && mv "${SRC}.tmp" "${SRC}"
    echo "パッチしました:"
    grep -n -B1 "0x31f7" "${SRC}"
fi

echo
echo "プラグインを再ビルドします（差分ビルド）..."
cmake --build "${EVCAM_ROOT}/openeb/build" --target hal_plugin_prophesee \
    -- -j"${EVCAM_BUILD_JOBS:-$(nproc)}"

cat <<'EOF'

次の手順:

  source ./env.sh
  metavision_hal_ls                # 検出されるか
  metavision_viewer                # 開けるか

検出されない場合は、どこで落ちているかをログで見る:

  MV_LOG_LEVEL=TRACE metavision_hal_ls 2>&1 | head -40

  - USB 照合まで届いていない  → VID/PID/subclass が違う
  - 照合は通るがその先で失敗  → Treuzell は喋るが初期化に独自差分がある
    （このときログに compatible 文字列が出るので、OpenEB の
      TzRegisterBuildMethod に登録済みの名前と一致するか確認する）

デバイスを開くには udev ルールが要ります（未導入なら scripts/ca_device.rules を参照）。
EOF
