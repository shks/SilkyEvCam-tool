# EvCam — SilkyEvCam + OpenEB セットアップ

CenturyArks **SilkyEvCam** を Ubuntu 24.04 で動かすための環境。
Prophesee の **OpenEB**（Metavision SDK の OSS 部分, Apache 2.0）をソースからビルドして使う。
有償の Metavision SDK は使わない。

## 使い方

```bash
source /home/maeda/projects/EvCam/env.sh

metavision_viewer                 # ライブ表示
python scripts/smoke_test.py      # 疎通確認（イベント数を出す）
```

`env.sh` を source したシェルでのみ有効。`~/.bashrc` は変更していない。

## 構成

```
SilkyEvCam (USB3, VID 31f7)
      ↓
libsilky_common_plugin.so     CenturyArks 製 HAL プラグイン。MV_HAL_PLUGIN_PATH で発見される
      ↓
libmetavision_hal.so.5        OpenEB。EVT3 デコード
      ↓
metavision_viewer / metavision_core (Python)
```

```
EvCam/
├── env.sh                    環境設定（これを source する）
├── openeb/                   OpenEB 5.2.0 ソース + build/（git 管理外）
├── vendor/
│   ├── SilkyEvCam_Plugin_Installer_for_ubuntu_v5.2.0.zip
│   ├── silkyevcam-plugin/    展開したもの（git 管理外）
│   └── silky-hal/            実際に使う配置
│       ├── plugins/libsilky_common_plugin.so
│       └── bin/silkyevcam_platform_info, silkyevcam_mask_pixel_util
├── scripts/smoke_test.py
└── .venv/                    Python 3.12 venv（git 管理外）
```

## バージョン固定

**OpenEB は 5.2.0 に固定。** プラグインの `readme.txt` に明記されている:

> This pack is for Metavision version 5.2.0. Cannot be used with version 5.1.1 or earlier

OpenEB を上げるときは、必ず https://centuryarks.com/download-2/ から対応するプラグインも上げること。

## セットアップ手順（実際に通したもの）

### 1. 依存パッケージ（要 root）

```bash
sudo apt-get update
sudo apt-get install -y cmake libboost-all-dev libusb-1.0-0-dev \
  libprotobuf-dev protobuf-compiler libhdf5-dev libglew-dev libglfw3-dev
```

`libopencv-dev` / `ffmpeg` / `build-essential` は導入済みだった。

### 2. Python venv

Ubuntu 24.04 は PEP 668 でシステム Python への pip を拒否するため venv に隔離。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install numpy opencv-python h5py scipy pytest
.venv/bin/python -m pip install "pybind11==2.13.6"
```

**pybind11 は 2.13.6 に固定。** OpenEB 5.2.0 は `find_package(pybind11 2.7)` で 2.x 系を前提にしており、
最新の 3.1.0 は API 非互換のリスクがある。

### 3. OpenEB のビルド

```bash
EVCAM_ROOT="$(pwd)"
git clone https://github.com/prophesee-ai/openeb.git --branch 5.2.0

cmake -S openeb -B openeb/build \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_TESTING=OFF \
  -DCOMPILE_PYTHON3_BINDINGS=ON \
  -DPython3_EXECUTABLE="${EVCAM_ROOT}/.venv/bin/python" \
  -Dpybind11_DIR="$("${EVCAM_ROOT}/.venv/bin/python" -c 'import pybind11; print(pybind11.get_cmake_dir())')" \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
  -Wno-dev

cmake --build openeb/build -- -j$(nproc)
```

`install` はしない（ローカルビルドツリー方式）。

### 4. プラグイン配置

```bash
./scripts/fetch_plugin.sh
```

プラグインは CenturyArks の proprietary バイナリなのでリポジトリには含めていない
（`LICENSE_CA.txt`: "may be used only when using CenturyArks's products or services"）。
上記スクリプトが zip を取得して `vendor/silky-hal/` に配置する。

**同梱の `CA_Silky_installer.sh` は使わない。** あれは
`~/.bashrc` に `export MV_HAL_PLUGIN_PATH=...` を追記し、`/usr/lib/CenturyArks/` と `/usr/bin` に
system-wide でファイルを置くため、ローカルツリー方式と衝突する。
インストーラの残りの仕事は udev ルールだけなので、それは手動で入れる（次項）。

### 5. udev ルール（要 root）

```bash
sudo cp vendor/silkyevcam-plugin/.../resources/ca_device.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

中身:

```
KERNEL=="*", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ACTION=="add", ATTR{idVendor}=="31f7", MODE="666", TAG="causb_dev"
```

SilkyEvCam は **VID `31f7`（CenturyArks）**。Prophesee の `88-cyusb.rules`（VID `04b4`）は
Prophesee EVK 用なので**入れていない**。

## ハマりどころ

### cmake 4.2.0 は問題なかった

この PC の `cmake` は `/usr/local/bin` に手動導入された 4.2.0。OpenEB が古い
`cmake_minimum_required` を使っていると configure が落ちる懸念があったが、実測では警告のみ:

```
CMake Deprecation Warning at CMakeLists.txt:20 (cmake_minimum_required):
  Compatibility with CMake < 3.10 will be removed from a future version of CMake.
```

apt の cmake 3.28.3 も入れてあるが、必須ではない。

### CUDA は PATH に無い

CUDA 13.0 は入っているが `/usr/local/cuda/bin` が PATH に無く、そのままだと
`Looking for a CUDA compiler - NOTFOUND` で黙って CPU のみのビルドになる。
`-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc` を明示すること。

### setup_env.sh は MV_HAL_PLUGIN_PATH を上書きする

`openeb/build/utils/scripts/setup_env.sh` は `MV_HAL_PLUGIN_PATH` を
OpenEB 自身のプラグインパスで**上書き**する。Silky プラグインの追加は必ずこの後に
**追記**すること（`env.sh` はそうしている）。

## レイテンシについて

目標は end-to-end で数 ms 以内。支配要因は以下（OpenEB のソースを読んで確認）。

### ① アプリ層のスライシング

Python の `EventsIterator` は `delta_t` 分バッファしてから返すので、窓幅がそのまま
レイテンシの床になる（`events_iterator.py:54`、デフォルト 10 ms）。
**数 ms を狙う経路に Python は使わない。** C++ の
`camera.cd().add_callback()`（`sdk/modules/stream/cpp/include/metavision/sdk/stream/camera.h:277`）
ならデコード済みバッファが届いた時点で即コールバックされる。

### ② USB 転送層

`hal_psee_plugins/src/boards/utils/psee_libusb_data_transfer.cpp:34-36`:

```cpp
constexpr size_t USB_TIME_OUT                = 100;        // ms
constexpr size_t N_ASYNC_TRANFERS_PER_DEVICE = 20;
constexpr size_t PACKET_SIZE                 = 128 * 1024;
```

環境変数で上書きできる。CenturyArks プラグインは OpenEB 5.2.0 の `hal_psee_plugins` を
そのままビルドしたものなので（`strings` でビルドパス `openeb520Silky` を確認）、同じ変数が効く:

- `MV_PSEE_DEBUG_PLUGIN_USB_PACKET_SIZE`
- `MV_PSEE_DEBUG_PLUGIN_USB_ASYNC_TRANSFER`
- `MV_PSEE_DEBUG_PLUGIN_USB_TIME_OUT`

**誤解しやすい点が 2 つある。**

`:219` のコメント `// Note: unlike raw libusb, partial transfers are reported as completed` のとおり、
FX3 が短パケットで区切れば 128 KiB 未満でも即届く。**PACKET_SIZE は上限であって下限ではない。**
実際の遅延は FX3 ファーム（クローズド）の挙動次第なので、**実測が要る**。

`:208-216` の `LIBUSB_TRANSFER_TIMED_OUT` は「データがゼロだった」ケースで、バッファは
破棄されず再投入されるだけ。**`MV_PSEE_DEBUG_PLUGIN_USB_TIME_OUT` を下げてもレイテンシは下がらない。**

### ③ ERC で最悪値を抑える

USB 帯域を超えると内部 FIFO に溜まってレイテンシが跳ねる。
`hal/cpp/include/metavision/hal/facilities/i_erc_module.h` の `set_cd_event_rate()` で
イベントレートに上限をかけると最悪レイテンシが有界になる。

### ④ 無関係なもの

**CUDA はキャプチャレイテンシには効かない**（下流の推論用）。

## レイテンシ実測結果

計測ツール: `src/latency_probe.cpp` → `scripts/build_tools.sh` でビルド。

```bash
source env.sh
./scripts/build_tools.sh
./build/latency_probe --seconds 10
./scripts/latency_sweep.sh 8      # USB パラメータ掃引
```

### 結論: 約 4 ms の配送周期がハードな下限。host 側のチューニングでは下がらない

Gen3.1 VGA / デフォルト設定 / 静止シーン / warmup 500 ms 除外:

| 指標 | mean | p50 | p90 | p99 | max |
|---|---|---|---|---|---|
| lag (バッチ内で最も古いイベント) | 3.80 ms | 3.89 | 4.08 | 4.20 | **5.12 ms** |
| lag (バッチ内で最も新しいイベント) | 0.36 ms | 0.28 | 0.78 | 1.42 | 2.55 ms |
| コールバック間隔 | 4.000 ms | 3.999 | 4.07 | 4.19 | 5.85 ms |
| バッチのセンサ時間幅 | 3.44 ms | 3.55 | 3.87 | 3.96 | 3.99 ms |

コールバック間隔の中央値が **3.999 ms** と異常なほど安定している。これは FX3 ファームウェアが
内部タイマで約 4 ms ごとにバッファをコミットしているため。結果として個々のイベントの
上乗せ遅延は 0〜4 ms に一様分布し、平均約 2 ms、実測最悪 5.1 ms になる。

### USB パラメータ掃引: 効果なし

`scripts/latency_sweep.sh` の結果（`lag_p99`）:

| 設定 | rate | lag_p99 |
|---|---|---|
| packet 128 KiB (default) | 29 kev/s | 4225 us |
| packet 32 KiB | 73 kev/s | 4219 us |
| packet 8 KiB | 358 kev/s | 4189 us |
| packet 4 KiB | 3.5 kev/s | 4187 us |
| async 20 (default) | 515 kev/s | 4208 us |
| async 4 | 3.2 kev/s | 4166 us |
| async 2 | 3.4 kev/s | 4201 us |

**`lag_p99` は全条件で 4.17〜4.23 ms に張り付く。** イベントレートが 3.4 kev/s 〜 515 kev/s と
150 倍変わっても動かない。つまりホスト側のバッファリングではなくカメラ側の配送周期が支配的で、
`MV_PSEE_DEBUG_PLUGIN_USB_PACKET_SIZE` も `_ASYNC_TRANSFER` も**レイテンシには効かない**。

### 測定手法上の注意（2 つとも実際に踏んだ）

**1. ベースラインに全区間の最小値＋一次回帰を使ってはいけない。**
EVT3 の `NonMonotonicTimeHigh` が 1 回混ざるだけで回帰が壊れ、初回計測では
ドリフト **-5800 ppm**（非現実的）、`lag_p50` 32 ms という汚染された数値が出た。
前後 1 秒の**移動最小値**をベースラインにすると、ドリフトは 16〜20 ppm という妥当な値に収まり、
孤立したタイムスタンプ跳びにも影響されない。

**2. 起動直後の約 250 ms を捨てること。**
`camera.start()` 直後は FX3 側に溜まっていた分がまとめて降ってくるため、
`lag_max` が全条件で約 251 ms に張り付いていた。`--warmup 500`（デフォルト）で除外すると
**251,172 us → 5,123 us** になる。定常時の挙動を見たいならこれを含めてはいけない。

**3. `NonContinuousTimeHigh` は警告ではない。**
robust デコーダを使うと 10 秒で 1274 件出るが、これは疎なシーンで `time_high` が
2 段以上飛んだだけで正常。`NonMonotonicTimeHigh` とは区別して扱うこと
（`latency_probe` は種類別に数えている）。

### 測れていないもの

報告値は**ベストケースからの上乗せ分**であって絶対レイテンシではない。
センサ時刻と host 時刻は同期していないので、未知の定数オフセット
C（光子到達 → センサ読み出し → USB 転送 → デコードの最小遅延）が分離できない。

絶対値が必要なら外部刺激が要る:

- host が既知時刻に GPIO で LED を光らせ、その輝度変化イベントが返ってくるまでを測る
- または `I_TriggerIn` に既知時刻の信号を入れる（要配線）

### 数 ms 目標に対する評価

目標「数 ms 以内」に対し、**平均 約 2 ms / p99 約 4.2 ms**（+ 未測定の定数 C）。
ただし約 4 ms はカメラファームウェア由来のため、**ここから下げる手段はホスト側には無い**。
さらに詰めるなら CenturyArks にファームウェアのコミット周期を問い合わせる必要がある。

## trigger loopback による検証（配線不要）

`src/trigger_probe.cpp`。**Raspberry Pi も host GPIO も要らない。**

このカメラは `I_TriggerOut`（周期パルス生成）と `I_TriggerIn` を両方持ち、
TriggerIn には **LOOPBACK チャネル**がある（`get_available_channels()` →
`{Channel.MAIN: 0, Channel.LOOPBACK: 6}`）。つまりカメラが自分でパルスを出し、
自分でタイムスタンプを打てる。**単一クロックドメインなので同期問題が消える。**

```bash
./build/trigger_probe --seconds 10 --period 8600
```

### 結果: 配送グリッド 4.00 ms を独立に確認

エッジ間隔 4300 us（4 ms と非整数比なので位相が掃引される）で 10 秒:

| 指標 | mean | p50 | p90 | p99 | max |
|---|---|---|---|---|---|
| sensor edge interval | 4300.0 | 4300.0 | 4300.0 | 4300.0 | **4300.0** us |
| host arrival lag | **2045.9** | 2034.0 | 3633.0 | **4025.0** | 7656.0 us |

**`sensor edge interval` は 2328 エッジ全部が 4300.0 us**（max まで一致）。
センサ側のタイムベースにジッタが無く、完璧な基準信号として使える。

そのうえで `host arrival lag` は **[0, 4000 us] の一様分布そのもの**
（mean 2046 ≒ 4000/2、p50 2034、p99 4025）。エッジ間隔 3333 us でも同じ結果。

シーンにもイベントレートにも依存しない完全周期信号で測ってこうなるので、
**配送グリッドが 4.00 ms であることが独立に確定した**。
`latency_probe` のシーン依存な測定と完全に一致する。

つまり任意のイベントのグリッド待ち時間は **平均 2.0 ms / 最悪 4.0 ms**。

## 絶対レイテンシの測定（Step 2、要 LED 配線）

残る未知量は定数 C（光子到達 → センサタイムスタンプ）だけ。これは
**trigger_out の外部端子に LED を付けてセンサに向ければ測れる**:

```bash
./build/trigger_probe --optical --seconds 20
```

Δ = `t_cd(発光)` − `t_trigger(loopback)` を出す。**両方センサクロックなので
オフセット未知の問題が発生しない。**

必要なもの:

- SilkyEvCam の trigger 端子ケーブルとピンアサイン・電圧仕様（CenturyArks に要確認）
- LED + 電流制限抵抗（trigger_out の駆動能力が足りなければトランジスタ）
- LED をセンサに向ける

### なぜ Raspberry Pi を使わないか

1. **プラグインが x86-64 専用。** `file` で確認済み
   （`ELF 64-bit LSB shared object, x86-64`）。zip に ARM/aarch64 バイナリは無い。
   CenturyArks が aarch64 版を出していない限り、**Pi ではカメラ自体が動かない**
2. **Pi を別マシンの刺激源にすると悪化する。** Pi と host のクロックを 4 ms より
   十分細かく同期する必要があるが、chrony/NTP は ms オーダー＝測りたい量と同じ桁。
   ハードウェアタイムスタンプ付き PTP なら足りるが、労力に見合わない
3. **そもそも不要。** カメラが trigger_out + LOOPBACK を持っているので単一
   クロックドメインで完結する

## 未了

- [ ] Step 2: LED 配線して光→イベントの絶対遅延を測定
- [ ] 高イベントレート下での輻輳時挙動と ERC の効果検証
      （掃引時にシーンが揃っておらず、レート依存性を分離できていない）
