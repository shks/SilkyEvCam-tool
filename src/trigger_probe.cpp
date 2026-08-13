// 絶対レイテンシ測定のための trigger 経路プローブ.
//
// ---------------------------------------------------------------------------
// なぜ Raspberry Pi や host GPIO が要らないか
// ---------------------------------------------------------------------------
// SilkyEvCam は I_TriggerOut（周期パルス生成）と I_TriggerIn（外部パルスの
// タイムスタンプ付け）を両方持ち、さらに TriggerIn には LOOPBACK チャネルがある。
// つまり「カメラが自分でパルスを出し、自分でタイムスタンプを打つ」ことができる。
//
// これが決定的で、host とセンサのクロックを同期する必要が一切ない。
// 別マシン（Pi 等）を刺激源にすると、測ろうとしている 4 ms より大きい
// クロック同期誤差（chrony/NTP で ms オーダー）が乗ってしまい本末転倒になる。
//
// 段階:
//   Step 1 (このツール / 配線不要):
//       trigger_out → LOOPBACK → trigger_in。センサ時刻での完全周期信号が得られる。
//       host 到着時刻と突き合わせれば、シーン依存のない綺麗な транспорт遅延が測れる。
//   Step 2 (LED 配線が必要):
//       trigger_out の外部端子に LED を付けてセンサに向ける。
//       Δ = t_cd(発光) - t_trigger(loopback) が「光→イベント」の絶対遅延。
//       両方ともセンサクロックなのでオフセット未知の問題が消える。
// ---------------------------------------------------------------------------

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <string>
#include <thread>
#include <vector>

#include <metavision/hal/facilities/i_trigger_in.h>
#include <metavision/hal/facilities/i_trigger_out.h>
#include <metavision/sdk/base/events/event_cd.h>
#include <metavision/sdk/base/events/event_ext_trigger.h>
#include <metavision/sdk/stream/camera.h>

namespace {

struct Trig {
    int64_t host_us;
    int64_t t;  // センサ時刻
    short p;
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
    if (v.empty()) {
        std::printf("  %-24s (データなし)\n", label);
        return;
    }
    std::sort(v.begin(), v.end());
    double sum = 0.0;
    for (double x : v) {
        sum += x;
    }
    std::printf("  %-24s mean %9.1f | p50 %9.1f | p90 %9.1f | p99 %9.1f | max %9.1f  %s\n", label,
                sum / static_cast<double>(v.size()), percentile(v, 50), percentile(v, 90), percentile(v, 99),
                percentile(v, 100), unit);
}

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

} // namespace

int main(int argc, char **argv) {
    double seconds   = 10.0;
    uint32_t period  = 20000; // us
    double duty      = 0.5;
    double warmup_ms = 500.0;
    bool optical     = false;
    std::string chan = "loopback";

    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        if (a == "--seconds" && i + 1 < argc) {
            seconds = std::atof(argv[++i]);
        } else if (a == "--period" && i + 1 < argc) {
            period = static_cast<uint32_t>(std::atoll(argv[++i]));
        } else if (a == "--duty" && i + 1 < argc) {
            duty = std::atof(argv[++i]);
        } else if (a == "--warmup" && i + 1 < argc) {
            warmup_ms = std::atof(argv[++i]);
        } else if (a == "--channel" && i + 1 < argc) {
            chan = argv[++i];
        } else if (a == "--optical") {
            optical = true;
        } else {
            std::fprintf(stderr,
                         "usage: %s [--seconds N] [--period US] [--duty R] [--warmup MS]\n"
                         "          [--channel loopback|main] [--optical]\n"
                         "\n"
                         "  --channel main  外部端子に入れた信号を測る（Step 2 の LED 配線時）\n"
                         "  --optical       trigger エッジ直後の CD イベントとの差を出す\n"
                         "                  （LED をセンサに向けていないと無意味）\n",
                         argv[0]);
            return 2;
        }
    }

    Metavision::Camera camera;
    try {
        camera = Metavision::Camera::from_first_available();
    } catch (const Metavision::CameraException &e) {
        std::fprintf(stderr, "カメラを開けませんでした: %s\n", e.what());
        return 1;
    }

    auto *t_in  = camera.get_device().get_facility<Metavision::I_TriggerIn>();
    auto *t_out = camera.get_device().get_facility<Metavision::I_TriggerOut>();
    if (t_in == nullptr || t_out == nullptr) {
        std::fprintf(stderr, "このカメラは trigger in/out を持っていません\n");
        return 1;
    }

    const auto channel =
        (chan == "main") ? Metavision::I_TriggerIn::Channel::Main : Metavision::I_TriggerIn::Channel::Loopback;

    const auto available = t_in->get_available_channels();
    if (available.find(channel) == available.end()) {
        std::fprintf(stderr, "チャネル %s は使えません\n", chan.c_str());
        return 1;
    }

    if (!t_out->set_period(period) || !t_out->set_duty_cycle(duty)) {
        std::fprintf(stderr, "trigger_out の設定に失敗\n");
        return 1;
    }
    if (!t_in->enable(channel)) {
        std::fprintf(stderr, "trigger_in の有効化に失敗\n");
        return 1;
    }
    if (!t_out->enable()) {
        std::fprintf(stderr, "trigger_out の有効化に失敗\n");
        return 1;
    }

    std::vector<Trig> trigs;
    trigs.reserve(1u << 20);
    std::vector<int64_t> cd_ts;
    if (optical) {
        cd_ts.reserve(1u << 22);
    }

    camera.ext_trigger().add_callback(
        [&trigs](const Metavision::EventExtTrigger *begin, const Metavision::EventExtTrigger *end) {
            const int64_t h = now_us();
            for (auto *e = begin; e != end; ++e) {
                if (trigs.size() == trigs.capacity()) {
                    return;
                }
                trigs.push_back(Trig{h, e->t, e->p});
            }
        });

    if (optical) {
        camera.cd().add_callback([&cd_ts](const Metavision::EventCD *begin, const Metavision::EventCD *end) {
            for (auto *e = begin; e != end; ++e) {
                if (cd_ts.size() == cd_ts.capacity()) {
                    return;
                }
                cd_ts.push_back(e->t);
            }
        });
    }

    camera.start();
    const int64_t t_start = now_us();
    while (camera.is_running() && now_us() < t_start + static_cast<int64_t>((warmup_ms / 1e3 + seconds) * 1e6)) {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    camera.stop();
    t_out->disable();
    t_in->disable(channel);

    const int64_t cutoff = t_start + static_cast<int64_t>(warmup_ms * 1000.0);
    trigs.erase(std::remove_if(trigs.begin(), trigs.end(), [cutoff](const Trig &t) { return t.host_us < cutoff; }),
                trigs.end());

    std::printf("\n=== trigger probe ===\n");
    std::printf("  channel           %s\n", chan.c_str());
    // 立ち上がり・立ち下がりの両エッジがタイムスタンプされるので、
    // エッジ間隔は period*duty と period*(1-duty) の交互になる（duty=0.5 なら等間隔）
    std::printf("  trigger_out       period %u us, duty %.2f  → エッジ間隔 %.0f/%.0f us の交互\n", period, duty,
                period * duty, period * (1.0 - duty));
    std::printf("  trigger events    %zu  (%.1f /s)\n", trigs.size(), trigs.size() / seconds);

    if (trigs.size() < 10) {
        std::fprintf(stderr,
                     "\ntrigger イベントがほとんど取れていません。\n"
                     "LOOPBACK が無効か、trigger_out が動作していない可能性があります。\n");
        return 1;
    }

    const size_t n = trigs.size();
    std::vector<double> h(n), lag(n), sensor_interval, host_interval;
    sensor_interval.reserve(n - 1);
    host_interval.reserve(n - 1);

    const int64_t h0 = trigs.front().host_us;
    const int64_t s0 = trigs.front().t;
    for (size_t i = 0; i < n; ++i) {
        h[i]   = static_cast<double>(trigs[i].host_us - h0);
        lag[i] = h[i] - static_cast<double>(trigs[i].t - s0);
        if (i > 0) {
            sensor_interval.push_back(static_cast<double>(trigs[i].t - trigs[i - 1].t));
            host_interval.push_back(h[i] - h[i - 1]);
        }
    }

    const double W = 1e6;
    const auto fwd = causal_min(h, lag, W);
    const auto bwd = anticausal_min(h, lag, W);
    for (size_t i = 0; i < n; ++i) {
        lag[i] -= std::min(fwd[i], bwd[i]);
    }

    std::printf("\n  センサクロック上の周期性（trigger_out の素性。ここが乱れていたら測定不能）\n");
    print_dist("sensor edge interval", "us", sensor_interval);
    std::printf("\n  host 到着のばらつき。完全周期な信号なのでシーン依存が無く、\n");
    std::printf("  latency_probe より綺麗に配送周期を切り出せる\n");
    print_dist("host arrival lag", "us", lag);
    print_dist("host arrival interval", "us", host_interval);

    if (optical) {
        // 各立ち上がりエッジ直後の最初の CD イベントまでの差。
        // LED がセンサを照らしていない場合、拾うのは環境ノイズなので無意味。
        std::sort(cd_ts.begin(), cd_ts.end());
        std::vector<double> optical_delta;
        for (const auto &t : trigs) {
            if (t.p != 1) {
                continue;
            }
            const auto it = std::upper_bound(cd_ts.begin(), cd_ts.end(), t.t);
            if (it != cd_ts.end()) {
                optical_delta.push_back(static_cast<double>(*it - t.t));
            }
        }
        std::printf("\n  光→イベント（Step 2。LED をセンサに向けていない場合は環境ノイズを拾うだけ）\n");
        std::printf("  CD events collected: %zu\n", cd_ts.size());
        print_dist("optical delta", "us", optical_delta);
    }

    std::printf("\n");
    return 0;
}
