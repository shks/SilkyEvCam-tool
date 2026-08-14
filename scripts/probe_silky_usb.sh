#!/bin/bash
# SilkyEvCam の USB ディスクリプタを読み、OpenEB 同梱の Prophesee プラグイン
# （Treuzell 経路）で開ける見込みがあるかを判定する。
#
# 背景: プラグインが USB デバイスを拾う条件は tz_libusb_board_command.cpp:66-84 で、
#
#     VID/PID 完全一致 && bInterfaceClass == 0xFF && bInterfaceSubClass == <登録値>
#     && bInterfaceProtocol == 0 (PSEE_EVK_PROTOCOL) && bulk エンドポイント 3 本 (IN/OUT/IN)
#
# Prophesee が psee_universal.cpp で登録している VID は 0x03fd / 0x04b4 / 0x1FC9 の 3 つで、
# CenturyArks の 0x31f7 は入っていない。条件のうち VID 以外が揃っているなら、
# add_usb_id を 1 行足すだけで開ける可能性がある（→ scripts/try_psee_plugin_with_silky.sh）。
#
# root は不要。sysfs は誰でも読める（lsusb -v は環境によって root を要求する）。
set -uo pipefail

VID="${1:-31f7}"
VID="${VID#0x}"

# 判定ロジックをカメラ無しで検証できるように、sysfs の位置を差し替え可能にしておく
# （recorder/postproc.py の parse_info を probe から分離してあるのと同じ理由）。
SYSFS="${EVCAM_SYSFS:-/sys/bus/usb/devices}"

found=0
for dev in "${SYSFS}"/[0-9]*-[0-9]*; do
    [ -f "${dev}/idVendor" ] || continue
    [ "$(cat "${dev}/idVendor")" = "${VID}" ] || continue
    found=1

    pid="$(cat "${dev}/idProduct")"
    echo "デバイス: $(basename "${dev}")"
    echo "  VID:PID     = ${VID}:${pid}"
    for f in manufacturer product serial version speed; do
        [ -f "${dev}/${f}" ] && printf '  %-12s= %s\n' "${f}" "$(tr -d '\n' < "${dev}/${f}")"
    done

    for ifc in "${dev}":*; do
        [ -f "${ifc}/bInterfaceClass" ] || continue
        cls="$(cat "${ifc}/bInterfaceClass")"
        sub="$(cat "${ifc}/bInterfaceSubClass")"
        proto="$(cat "${ifc}/bInterfaceProtocol")"
        neps="$(cat "${ifc}/bNumEndpoints")"

        echo
        echo "  インタフェース $(basename "${ifc}")"
        echo "    bInterfaceClass    = 0x${cls}   (期待 0xff = vendor specific)"
        echo "    bInterfaceSubClass = 0x${sub}   (期待 0x19 = Treuzell)"
        echo "    bInterfaceProtocol = 0x${proto}   (期待 0x00 = PSEE_EVK_PROTOCOL)"
        echo "    bNumEndpoints      = ${neps}      (期待 3)"

        bulk_in=0 bulk_out=0 other=0
        for ep in "${ifc}"/ep_*; do
            [ -f "${ep}/bEndpointAddress" ] || continue
            addr="$(cat "${ep}/bEndpointAddress")"
            attr="$(cat "${ep}/bmAttributes")"
            dir="OUT"
            # bEndpointAddress の bit7 が向き。sysfs は 16 進文字列で返る。
            [ $(( 0x${addr} & 0x80 )) -ne 0 ] && dir="IN"
            kind="other"
            if [ $(( 0x${attr} & 0x03 )) -eq 2 ]; then
                kind="bulk"
                [ "${dir}" = "IN" ] && bulk_in=$((bulk_in + 1)) || bulk_out=$((bulk_out + 1))
            else
                other=$((other + 1))
            fi
            echo "    ep 0x${addr}  ${dir}  ${kind}"
        done

        # 判定。エンドポイントの「順序」までは sysfs のディレクトリ名順では保証できないので、
        # 本数と向きの内訳だけを見る。順序が問題になったら lsusb -v で確認すること。
        # sysfs の数値は 16 進のゼロ詰め文字列で返る（bNumEndpoints は "03"）。
        # 文字列比較すると "3" と一致せず、条件を満たしていても取りこぼす。
        ok=1
        [ $(( 16#${cls} )) -eq $(( 16#ff )) ] || ok=0
        [ $(( 16#${sub} )) -eq $(( 16#19 )) ] || ok=0
        [ $(( 16#${proto} )) -eq 0 ] || ok=0
        [ $(( 16#${neps} )) -eq 3 ] || ok=0
        [ "${bulk_in}" -eq 2 ] && [ "${bulk_out}" -eq 1 ] || ok=0

        echo
        if [ "${ok}" -eq 1 ]; then
            echo "    → 条件を満たしています。VID を登録すれば開ける見込みがあります:"
            echo "         ./scripts/try_psee_plugin_with_silky.sh 0x${pid}"
        else
            echo "    → 条件を満たしません。このインタフェースは Treuzell ではありません。"
            echo "       （複数インタフェースがある機器では、他のインタフェースを確認すること）"
        fi
    done
    echo
done

if [ "${found}" -eq 0 ]; then
    echo "VID ${VID} のデバイスが見つかりません。" >&2
    echo "  - カメラが挿さっているか確認してください（lsusb で一覧が見られます）" >&2
    echo "  - SilkyEvCam の VID は 31f7 です。別の機器を見たいときは引数で VID を渡せます" >&2
    exit 1
fi
