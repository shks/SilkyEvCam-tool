#!/bin/bash
# MV_PSEE_DEBUG_PLUGIN_USB_PACKET_SIZE を振ってレイテンシへの効きを見る。
#
#   source env.sh && ./scripts/latency_sweep.sh [計測秒数]
#
# シーンを揃えないと比較にならない。掃引中はカメラの前の状況を変えないこと。
set -euo pipefail

SECONDS_PER_RUN="${1:-8}"
EVCAM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROBE="${EVCAM_ROOT}/build/latency_probe"

if [ ! -x "$PROBE" ]; then
    echo "ERROR: $PROBE が無い。scripts/build_tools.sh を先に実行" >&2
    exit 1
fi

echo "label,packet_size,async,erc,duration_s,events,rate_evs,batches,lag_p50_us,lag_p99_us,lag_max_us,int_p50_us,int_p99_us,drift_ppm,nonmono,out_of_order"

for pkt in 131072 32768 8192 4096; do
    MV_PSEE_DEBUG_PLUGIN_USB_PACKET_SIZE="$pkt" \
        "$PROBE" --seconds "$SECONDS_PER_RUN" --csv --label "pkt${pkt}"
done

# 非同期転送数の効きも見る（パケットサイズはデフォルトのまま）
for asy in 20 4 2; do
    MV_PSEE_DEBUG_PLUGIN_USB_ASYNC_TRANSFER="$asy" \
        "$PROBE" --seconds "$SECONDS_PER_RUN" --csv --label "async${asy}"
done
