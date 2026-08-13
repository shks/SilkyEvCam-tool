// SilkyEvCam キャプチャレイテンシの実測.
//
// camera.cd().add_callback() の中で、イベントのセンサタイムスタンプと host の
// CLOCK_MONOTONIC を突き合わせ、バッファリング遅延の分布を出す。
//
// ---------------------------------------------------------------------------
// 何が測れて、何が測れないか（重要）
// ---------------------------------------------------------------------------
// センサ時刻と host 時刻は同期していないので、両者の差には未知の定数オフセット C
// （= 光子到達からコールバックまでの最小遅延: センサ読み出し + USB 転送 + デコード）
// が乗る。ソフトウェアだけでは C を分離できない。
//
// そこで本ツールは C を「近傍時間窓での最小値」として差し引き、
//
//     報告値 = 実レイテンシ - C   (= ベストケースからの上乗せ分)
//
// を出す。これは USB バッファリングに起因する遅延そのもので、
// MV_PSEE_DEBUG_PLUGIN_USB_PACKET_SIZE を振ったときに変化するのはまさにこの項。
// 絶対値が要るなら、host が既知時刻に光る LED など外部刺激が別途必要。
//
// ベースラインに「全区間の最小値 + 一次回帰でのドリフト除去」を使うと、EVT3 の
// タイムスタンプ破綻（NonMonotonicTimeHigh）が 1 回混ざっただけで回帰が壊れ、
// 全数値が汚染される。実際に初回計測で -5800 ppm という非現実的なドリフトが出た。
// そのため「前後 W 秒の移動最小値」をベースラインに使う。これはクロックドリフトにも
// 孤立したタイムスタンプ跳びにも影響されない。
// ---------------------------------------------------------------------------

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <string>
#include <thread>
#include <vector>

#include <metavision/hal/facilities/i_decoder.h>
#include <metavision/hal/facilities/i_erc_module.h>
#include <metavision/sdk/base/events/event_cd.h>
#include <metavision/sdk/stream/camera.h>

namespace {

// コールバック 1 回分の要約。コールバック内では確保もロックもしない。
struct Batch {
    int64_t host_us;  // steady_clock
    int64_t s_first;  // バッチ先頭イベントのセンサ時刻 [us]
    int64_t s_last;   // バッチ末尾イベントのセンサ時刻 [us]
    uint32_t n;
};

int64_t now_us() {
    return std::chrono::duration_cast<std::chrono::microseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

double percentile(const std::vector<double> &sorted, double p) {
    if (sorted.empty()) {
        return 0.0;
    }
    const auto idx = static_cast<size_t>(p / 100.0 * static_cast<double>(sorted.size() - 1) + 0.5);
    return sorted[std::min(idx, sorted.size() - 1)];
}

void print_dist(const char *label, const char *unit, std::vector<double> v) {
    std::sort(v.begin(), v.end());
    double sum = 0.0;
    for (double x : v) {
        sum += x;
    }
    const double mean = v.empty() ? 0.0 : sum / static_cast<double>(v.size());
    std::printf("  %-22s mean %8.1f | p50 %8.1f | p90 %8.1f | p99 %8.1f | max %9.1f  %s\n", label, mean,
                percentile(v, 50), percentile(v, 90), percentile(v, 99), percentile(v, 100), unit);
}

// h は昇順。窓 [h[i]-W, h[i]] の最小値。
std::vector<double> causal_min(const std::vector<double> &h, const std::vector<double> &v, double W) {
    std::vector<double> out(v.size());
    std::deque<size_t> dq;
    for (size_t i = 0; i < v.size(); ++i) {
        while (!dq.empty() && v[dq.back()] >= v[i]) {
            dq.pop_back();
        }
        dq.push_back(i);
        while (h[dq.front()] < h[i] - W) {
            dq.pop_front();
        }
        out[i] = v[dq.front()];
    }
    return out;
}

// 窓 [h[i], h[i]+W] の最小値。
std::vector<double> anticausal_min(const std::vector<double> &h, const std::vector<double> &v, double W) {
    std::vector<double> out(v.size());
    std::deque<size_t> dq;
    for (size_t k = v.size(); k > 0; --k) {
        const size_t i = k - 1;
        while (!dq.empty() && v[dq.back()] >= v[i]) {
            dq.pop_back();
        }
        dq.push_back(i);
        while (h[dq.front()] > h[i] + W) {
            dq.pop_front();
        }
        out[i] = v[dq.front()];
    }
    return out;
}

[[noreturn]] void usage(const char *argv0) {
    std::fprintf(stderr,
                 "usage: %s [--seconds N] [--window S] [--erc RATE] [--robust] [--csv] [--label TEXT]\n"
                 "\n"
                 "  --seconds N   計測秒数 (default 10)\n"
                 "  --window S    ベースライン移動最小値の窓幅 秒 (default 1.0)\n"
                 "  --warmup MS   起動直後を捨てる時間 ms (default 500)\n"
                 "  --erc RATE    ERC を有効化しイベントレートを RATE ev/s に制限\n"
                 "  --robust      MV_FLAGS_EVT3_ROBUST_DECODER を有効化\n"
                 "  --csv         サマリを CSV 1 行で出力（スイープ用）\n"
                 "  --label TEXT  CSV の識別ラベル\n",
                 argv0);
    std::exit(2);
}

} // namespace

int main(int argc, char **argv) {
    double seconds    = 10.0;
    double window_s   = 1.0;
    double warmup_ms  = 500.0;
    uint64_t dropped_warmup = 0;
    uint32_t erc_rate = 0;
    bool csv          = false;
    bool robust       = false;
    std::string label = "-";

    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        if (a == "--seconds" && i + 1 < argc) {
            seconds = std::atof(argv[++i]);
        } else if (a == "--window" && i + 1 < argc) {
            window_s = std::atof(argv[++i]);
        } else if (a == "--warmup" && i + 1 < argc) {
            warmup_ms = std::atof(argv[++i]);
        } else if (a == "--erc" && i + 1 < argc) {
            erc_rate = static_cast<uint32_t>(std::atoll(argv[++i]));
        } else if (a == "--robust") {
            robust = true;
        } else if (a == "--csv") {
            csv = true;
        } else if (a == "--label" && i + 1 < argc) {
            label = argv[++i];
        } else {
            usage(argv[0]);
        }
    }

    // デコーダは生成時に env を読むので、カメラを開く前に設定する
    if (robust) {
        setenv("MV_FLAGS_EVT3_ROBUST_DECODER", "1", 1);
    }

    // コールバック中に再確保が起きないよう先に確保しておく
    std::vector<Batch> batches;
    batches.reserve(1u << 21);

    Metavision::Camera camera;
    try {
        camera = Metavision::Camera::from_first_available();
    } catch (const Metavision::CameraException &e) {
        std::fprintf(stderr, "カメラを開けませんでした: %s\n", e.what());
        return 1;
    }

    // デコードエラーを種類別に数える。
    // NonMonotonicTimeHigh はタイムスタンプ破綻＝計測結果が信用できない。
    // NonContinuousTimeHigh は time_high が 2 段以上飛んだだけで、疎なシーンでは
    // 正常に起きる（イベントが無い区間を跨ぐため）。両者を混ぜて警告してはいけない。
    std::atomic<uint64_t> v_nonmono{0};
    std::atomic<uint64_t> v_other{0};
    if (auto *dec = camera.get_device().get_facility<Metavision::I_Decoder>()) {
        dec->add_protocol_violation_callback([&](Metavision::DecoderProtocolViolation v) {
            if (v == Metavision::DecoderProtocolViolation::NonMonotonicTimeHigh) {
                ++v_nonmono;
            } else {
                ++v_other;
            }
        });
    }

    if (erc_rate > 0) {
        auto *erc = camera.get_device().get_facility<Metavision::I_ErcModule>();
        if (erc == nullptr) {
            std::fprintf(stderr, "警告: このカメラは ERC 非対応。--erc を無視します\n");
            erc_rate = 0;
        } else {
            erc->enable(true);
            erc->set_cd_event_rate(erc_rate);
            erc_rate = erc->get_cd_event_rate();
        }
    }

    camera.cd().add_callback([&batches](const Metavision::EventCD *begin, const Metavision::EventCD *end) {
        if (begin == end) {
            return; // 空バッファは遅延の指標にならない
        }
        if (batches.size() == batches.capacity()) {
            return; // 再確保を避ける。上限に達したら以降は捨てる
        }
        batches.push_back(Batch{now_us(), begin->t, (end - 1)->t, static_cast<uint32_t>(end - begin)});
    });

    camera.start();
    const int64_t t_start  = now_us();
    const int64_t deadline = t_start + static_cast<int64_t>((warmup_ms / 1e3 + seconds) * 1e6);
    while (camera.is_running() && now_us() < deadline) {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    camera.stop();

    // 起動直後は FX3 側に溜まっていた分がまとめて降ってくるので捨てる。
    // これを含めると lag_max が全条件で ~251 ms に張り付き、定常時の挙動が見えない。
    {
        const int64_t cutoff = t_start + static_cast<int64_t>(warmup_ms * 1000.0);
        const auto it = std::find_if(batches.begin(), batches.end(),
                                     [cutoff](const Batch &b) { return b.host_us >= cutoff; });
        dropped_warmup = static_cast<uint64_t>(std::distance(batches.begin(), it));
        batches.erase(batches.begin(), it);
    }

    if (batches.size() < 10) {
        std::fprintf(stderr,
                     "バッチが %zu 個しか取れませんでした。シーンが静止していませんか？\n"
                     "カメラの前で手を振るなどして輝度変化を作ってください。\n",
                     batches.size());
        return 1;
    }

    const int64_t host_t0   = batches.front().host_us;
    const int64_t sensor_t0 = batches.front().s_first;

    const size_t n = batches.size();
    std::vector<double> h(n), l_new(n), l_old(n), span(n), evs(n), interval;
    interval.reserve(n - 1);

    uint64_t total_events = 0;
    uint64_t out_of_order = 0;
    for (size_t i = 0; i < n; ++i) {
        const Batch &b = batches[i];
        h[i]           = static_cast<double>(b.host_us - host_t0);
        l_new[i]       = h[i] - static_cast<double>(b.s_last - sensor_t0);
        l_old[i]       = h[i] - static_cast<double>(b.s_first - sensor_t0);
        span[i]        = static_cast<double>(b.s_last - b.s_first);
        evs[i]         = b.n;
        total_events += b.n;
        if (i > 0) {
            interval.push_back(h[i] - h[i - 1]);
            if (b.s_first < batches[i - 1].s_last) {
                ++out_of_order;
            }
        }
    }

    // ベースライン C は前後 window_s 秒の移動最小値。ドリフトと孤立跳びの両方に強い。
    const double W    = window_s * 1e6;
    const auto fwd    = causal_min(h, l_new, W);
    const auto bwd    = anticausal_min(h, l_new, W);
    std::vector<double> base(n);
    for (size_t i = 0; i < n; ++i) {
        base[i] = std::min(fwd[i], bwd[i]);
    }

    for (size_t i = 0; i < n; ++i) {
        l_new[i] -= base[i];
        l_old[i] -= base[i];
    }

    // 移動最小値の端点差からドリフトを推定（回帰と違い外れ値に潰されない）
    const double drift_ppm = (h.back() > 0) ? (base.back() - base.front()) / h.back() * 1e6 : 0.0;

    const double duration_s = h.back() / 1e6;
    const double rate_evs   = static_cast<double>(total_events) / duration_s;

    // 実際に効いている USB 設定を読み戻す
    const char *e_pkt = std::getenv("MV_PSEE_DEBUG_PLUGIN_USB_PACKET_SIZE");
    const char *e_asy = std::getenv("MV_PSEE_DEBUG_PLUGIN_USB_ASYNC_TRANSFER");
    const long pkt    = e_pkt ? std::atol(e_pkt) : 128 * 1024;
    const long asy    = e_asy ? std::atol(e_asy) : 20;

    if (csv) {
        std::vector<double> s_old = l_old, s_int = interval;
        std::sort(s_old.begin(), s_old.end());
        std::sort(s_int.begin(), s_int.end());
        std::printf("%s,%ld,%ld,%u,%.3f,%llu,%.0f,%zu,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%llu,%llu\n", label.c_str(), pkt,
                    asy, erc_rate, duration_s, static_cast<unsigned long long>(total_events), rate_evs, n,
                    percentile(s_old, 50), percentile(s_old, 99), percentile(s_old, 100), percentile(s_int, 50),
                    percentile(s_int, 99), drift_ppm, static_cast<unsigned long long>(v_nonmono.load()),
                    static_cast<unsigned long long>(out_of_order));
        return 0;
    }

    std::printf("\n=== capture latency probe ===\n");
    std::printf("  duration          %.2f s\n", duration_s);
    std::printf("  events            %llu  (%.3f Mev/s)\n", static_cast<unsigned long long>(total_events),
                rate_evs / 1e6);
    std::printf("  callbacks         %zu  (%.0f /s)\n", n, static_cast<double>(n) / duration_s);
    std::printf("  USB packet size   %ld B%s\n", pkt, e_pkt ? "" : "  (default)");
    std::printf("  USB async xfers   %ld%s\n", asy, e_asy ? "" : "  (default)");
    if (erc_rate > 0) {
        std::printf("  ERC               %u ev/s\n", erc_rate);
    }
    std::printf("  Evt3 decoder      %s\n", robust ? "robust" : "default");
    std::printf("  warmup dropped    %llu batches (%.0f ms)\n", static_cast<unsigned long long>(dropped_warmup),
                warmup_ms);
    std::printf("  drift             %.1f ppm  (移動最小値の端点差)\n", drift_ppm);

    const uint64_t vm = v_nonmono.load();
    const uint64_t vo = v_other.load();
    if (vm > 0 || out_of_order > 0) {
        std::printf("\n  !! NonMonotonicTimeHigh %llu, out-of-order batches %llu\n",
                    static_cast<unsigned long long>(vm), static_cast<unsigned long long>(out_of_order));
        std::printf("     タイムスタンプが壊れています。下の数値は信用できません。\n");
    }
    if (vo > 0) {
        std::printf("  (NonContinuousTimeHigh %llu — 疎なシーンでは正常。無害)\n",
                    static_cast<unsigned long long>(vo));
    }

    std::printf("\n  ベストケースからの上乗せ分。絶対レイテンシではない（冒頭のコメント参照）\n");
    print_dist("lag (oldest event)", "us", l_old);
    print_dist("lag (newest event)", "us", l_new);
    std::printf("\n  参考:\n");
    print_dist("callback interval", "us", interval);
    print_dist("batch span", "us", span);
    print_dist("events / callback", "  ", evs);
    std::printf("\n");

    return 0;
}
