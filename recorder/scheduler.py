"""録画の定期実行（スケジューラ).

毎日決まった時間帯だけ、固定長のチャンクに区切って録画し続ける。
設定はブラウザ（/api/scheduler）から変更でき、out/scheduler.json に永続化される。

設計上の要点:

- チャンクの境界は壁時計のグリッドに揃える（10 分なら :00, :10, :20 …）。
  録画 ID が ISO 時刻なので、ファイル名を見ればどの時間帯か分かる。
  時間帯の開始・終了はグリッドに揃わなくてよい（最初と最後のチャンクだけ短くなる）。

- スケジューラは「自分が開始した録画」だけを止める。手動録画には触らない。
  手動録画中に時間帯に入った場合は、手動録画が終わるまで待ってから始める。

- チャンク切替は stop → start の逐次処理なので、境界で数十 ms の欠落がある。
  ストリームは止めないので欠落はこの切替コストだけ。

- 空き容量が min_free_gb を下回ったら次のチャンクを開始しない（録画中のものは
  完走させる）。Pi の SD カードは高レートだと数時間で埋まるため、これが無いと
  ルートファイルシステムを食い潰して SSH すら入れなくなる。

判定ロジックは純関数（in_window / chunk_end / validate_config）に分離してあり、
実カメラ無しでテストできる（tests/test_scheduler.py）。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

_HM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# UI で提示する選択肢。API はテスト用に 1〜720 分の任意値も受けるが、
# 運用はこの中から選ぶこと（README「schedule パネル」参照）。
CHUNK_CHOICES = (5, 10, 20, 30, 60)


@dataclass
class ScheduleConfig:
    enabled: bool = False
    start: str = "08:00"        # HH:MM
    end: str = "18:00"          # HH:MM。start > end なら夜跨ぎ、start == end なら 24 時間
    chunk_minutes: int = 10
    dest_dir: str = ""          # 空なら既定（out/recordings）
    min_free_gb: float = 2.0    # これを下回ったら新しいチャンクを開始しない


def validate_config(cfg: dict) -> list[str]:
    """設定 dict の問題点を日本語で列挙する。空リストなら合格。"""
    errors = []
    for key in ("start", "end"):
        v = cfg.get(key, "")
        if not isinstance(v, str) or not _HM.fullmatch(v):
            errors.append(f"{key} は HH:MM 形式で指定してください（例 08:00）")
    cm = cfg.get("chunk_minutes")
    if not isinstance(cm, int) or not (1 <= cm <= 720):
        errors.append("chunk_minutes は 1〜720 の整数で指定してください")
    mf = cfg.get("min_free_gb")
    if not isinstance(mf, (int, float)) or mf < 0:
        errors.append("min_free_gb は 0 以上の数値で指定してください")
    return errors


def _parse_hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def in_window(now: dt.datetime, start: str, end: str) -> bool:
    """now が録画時間帯に入っているか。

    start == end は「常時録画」。start > end は夜跨ぎ（22:00→06:00 なら
    22:00〜24:00 と 00:00〜06:00）。終了時刻ちょうどは含まない。
    """
    t = now.time()
    s, e = _parse_hm(start), _parse_hm(end)
    if s == e:
        return True
    if s < e:
        return s <= t < e
    return t >= s or t < e


def window_end_at(now: dt.datetime, start: str, end: str) -> dt.datetime:
    """now が時間帯内にあるとき、その時間帯の終了時刻を返す。"""
    e = _parse_hm(end)
    end_today = now.replace(hour=e.hour, minute=e.minute, second=0, microsecond=0)
    s = _parse_hm(start)
    if s == e:
        return now + dt.timedelta(days=365)     # 常時録画。事実上「終わらない」
    if s < e:
        return end_today
    # 夜跨ぎ: 00:00〜end の側にいるなら今日の end、start〜24:00 の側なら明日の end
    return end_today if now.time() < e else end_today + dt.timedelta(days=1)


def chunk_end(now: dt.datetime, chunk_minutes: int, window_end: dt.datetime) -> dt.datetime:
    """このチャンクを終えるべき時刻。

    壁時計のグリッド（00:00 起点で chunk_minutes 刻み）の次の境界。
    時間帯の終了が先に来るならそちら。境界ちょうどに開始した場合は次の境界。
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (now - midnight).total_seconds()
    step = chunk_minutes * 60
    boundary = midnight + dt.timedelta(seconds=(int(elapsed) // step + 1) * step)
    return min(boundary, window_end)


class Scheduler:
    """1 秒周期で判定し、録画の開始・停止コールバックを叩くだけのスレッド。

    録画の実処理（meta 作成・postproc 起動）は app.py 側の共通関数に委ねる。
    ここではカメラにもファイルにも直接触らない。
    """

    def __init__(self, config_path: Path,
                 get_status: Callable[[], object],
                 start_fn: Callable[[str], str],
                 stop_fn: Callable[[], str | None],
                 free_bytes_fn: Callable[[], int]) -> None:
        self.config_path = config_path
        self._get_status = get_status
        self._start = start_fn
        self._stop = stop_fn
        self._free_bytes = free_bytes_fn

        self._lock = threading.Lock()
        self.config = self._load()
        self.owned_id: str | None = None        # 自分が開始した録画の ID
        self._chunk_deadline: dt.datetime | None = None
        self.last_error: str | None = None
        self.skipping_disk = False              # 容量不足でチャンク開始を見送り中

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ── 設定 ──────────────────────────────────────────────────────────
    def _load(self) -> ScheduleConfig:
        try:
            data = json.loads(self.config_path.read_text())
            cfg = ScheduleConfig(**{k: v for k, v in data.items()
                                    if k in ScheduleConfig.__dataclass_fields__})
            if validate_config(asdict(cfg)):
                return ScheduleConfig()          # 壊れた設定は既定に戻す（無効状態で安全）
            return cfg
        except (OSError, ValueError, TypeError):
            return ScheduleConfig()

    def save(self, cfg: ScheduleConfig) -> None:
        with self._lock:
            self.config = cfg
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2))

    def snapshot(self) -> dict:
        """UI 表示用の設定 + 実行状態。"""
        with self._lock:
            cfg = asdict(self.config)
        now = dt.datetime.now()
        active = in_window(now, cfg["start"], cfg["end"])
        out = dict(cfg)
        out["in_window"] = active
        out["owned_recording_id"] = self.owned_id
        out["chunk_deadline"] = self._chunk_deadline.isoformat(timespec="seconds") \
            if self._chunk_deadline else None
        out["last_error"] = self.last_error
        out["skipping_disk"] = self.skipping_disk
        out["chunk_choices"] = list(CHUNK_CHOICES)
        return out

    # ── スレッド ──────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop_event.wait(1.0):
            try:
                self._tick(dt.datetime.now())
            except Exception as exc:  # noqa: BLE001 — 判定ループは死なせない。UI に出す
                self.last_error = f"{type(exc).__name__}: {exc}"

    def _tick(self, now: dt.datetime) -> None:
        with self._lock:
            cfg = self.config
        status = self._get_status()
        recording = getattr(status, "recording", False)
        rec_id = getattr(status, "recording_id", None)

        owns = self.owned_id is not None and rec_id == self.owned_id
        active = cfg.enabled and getattr(status, "connected", False) \
            and in_window(now, cfg.start, cfg.end)

        # 止めるべきか: 時間帯の外に出た / 無効化された / チャンク境界に達した
        if owns and recording:
            if not active:
                self._stop_owned()
                return
            if self._chunk_deadline and now >= self._chunk_deadline:
                self._stop_owned()
                # 次のチャンクは次の tick で開始する（stop の完了を待つ意味もある）
                return

        # 始めるべきか
        if active and not recording:
            free_gb = self._free_bytes() / 1e9
            if free_gb < cfg.min_free_gb:
                self.skipping_disk = True
                return
            self.skipping_disk = False
            wend = window_end_at(now, cfg.start, cfg.end)
            if (wend - now).total_seconds() < 10:
                return                            # 終了間際に短切れのチャンクを作らない
            deadline = chunk_end(now, cfg.chunk_minutes, wend)
            try:
                new_id = self._start(f"定期録画 {cfg.start}-{cfg.end} / {cfg.chunk_minutes}分")
            except RuntimeError as exc:
                # 手動録画との競合など。次の tick で再判定する
                self.last_error = str(exc)
                return
            self.owned_id = new_id
            self._chunk_deadline = deadline
            self.last_error = None

    def stop_owned(self) -> bool:
        """自分が開始した録画を今すぐ止める。設定 API が無効化時に使う。"""
        if self.owned_id is None:
            return False
        self._stop_owned()
        return True

    def _stop_owned(self) -> None:
        try:
            self._stop()
        except RuntimeError as exc:
            self.last_error = str(exc)
        self.owned_id = None
        self._chunk_deadline = None
