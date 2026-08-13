"""カメラを占有するワーカースレッド.

SilkyEvCam は排他アクセスなので、このプロセスだけがデバイスを開く。
Web ハンドラからはこのクラス経由でしか触らない。

設計上の要点:

- ストリームは起動中ずっと流しっぱなしにし、録画は
  ``I_EventsStream.log_raw_data()`` の ON/OFF だけで切り替える。
  ストリームを止めずに書き込みだけ切り替えられることは実測で確認済み。
  こうすると「録画していないときもプレビューが見える」状態が自然に作れる。

- RAW は EVT3 のまま無変換で書く。デコード結果を書き直すのではないので
  CPU をほとんど食わず、内容も無損失。

- 記録レートは静止シーンでも 0.31 MB/s ある（EVT3 が一定周期で TIME_HIGH
  マーカーを吐くため、イベントが少なくてもファイルは小さくならない）。
  「何も映っていない時間はタダ」ではないので、残容量は常に見えるようにする。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

_MB = 1_000_000


@dataclass
class Status:
    connected: bool = False
    error: str | None = None
    serial: str = ""
    width: int = 0
    height: int = 0
    recording: bool = False
    recording_id: str | None = None
    started_at: float | None = None
    elapsed_s: float = 0.0
    bytes_written: int = 0
    event_rate: float = 0.0
    write_rate: float = 0.0
    total_events: int = 0
    biases: dict = field(default_factory=dict)


class CameraWorker:
    """デバイスを 1 本のスレッドで保持し、録画とプレビューを提供する。"""

    def __init__(self, recordings_dir: Path, preview_fps: float = 20.0,
                 accumulation_us: int = 20000) -> None:
        self.recordings_dir = recordings_dir
        self.preview_fps = preview_fps
        self.accumulation_us = accumulation_us

        self._lock = threading.Lock()
        self._status = Status()
        self._jpeg: bytes | None = None
        self._frame_event = threading.Event()

        # ワーカースレッドへの要求。None は「変更なし」。
        self._want_record: str | None = None   # 開始したい録画 ID
        self._want_stop = False

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ── 外部 API ────────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def status(self) -> Status:
        with self._lock:
            s = Status(**vars(self._status))
        if s.recording and s.started_at:
            s.elapsed_s = time.time() - s.started_at
        return s

    def latest_jpeg(self, timeout: float = 1.0) -> bytes | None:
        """次のフレームを待って JPEG を返す。プレビュー配信用。"""
        self._frame_event.wait(timeout)
        self._frame_event.clear()
        with self._lock:
            return self._jpeg

    def begin_recording(self, rec_id: str) -> None:
        with self._lock:
            if self._status.recording:
                raise RuntimeError("すでに録画中です")
            if not self._status.connected:
                raise RuntimeError("カメラに接続されていません")
            self._want_record = rec_id

    def end_recording(self) -> str | None:
        """録画停止を要求し、対象の録画 ID を返す。

        開始要求がまだワーカーに拾われていない窓（〜20ms）で呼ばれた場合は、
        その開始自体を取り消す。ここで「録画していません」と例外にすると、
        409 を返した直後に録画が始まり、以後誰にも止められなくなる。
        """
        with self._lock:
            if self._want_record is not None:
                cancelled = self._want_record
                self._want_record = None
                return cancelled
            if not self._status.recording:
                raise RuntimeError("録画していません")
            self._want_stop = True
            return self._status.recording_id

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """録画が実際に停止しファイルが閉じるまで待つ。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not self._status.recording and self._want_record is None and not self._want_stop:
                    return True
            time.sleep(0.05)
        return False

    # ── ワーカー本体 ────────────────────────────────────────────────────
    def _run(self) -> None:
        # import はスレッド内で行う。Metavision のバインディングは
        # 読み込み時にデバイスを探しに行くことがあるため。
        from metavision_core.event_io import EventsIterator
        from metavision_core.event_io.raw_reader import initiate_device
        import metavision_sdk_core as msc

        try:
            device = initiate_device("")
        except Exception as exc:  # noqa: BLE001 — 起動失敗は UI に出したい
            with self._lock:
                self._status.error = f"カメラを開けませんでした: {exc}"
            return

        events_stream = device.get_i_events_stream()
        iterator = EventsIterator.from_device(device, mode="mixed", delta_t=20000,
                                              n_events=200_000)
        height, width = iterator.get_size()

        biases = {}
        try:
            biases = dict(device.get_i_ll_biases().get_all_biases())
        except Exception:  # noqa: BLE001 — バイアス非対応でも録画はできる
            pass

        serial = ""
        try:
            serial = device.get_i_hw_identification().get_serial()
        except Exception:  # noqa: BLE001
            pass

        frame_gen = msc.PeriodicFrameGenerationAlgorithm(
            width, height, self.accumulation_us, self.preview_fps)
        frame_gen.set_output_callback(self._on_frame)

        with self._lock:
            self._status.connected = True
            self._status.serial = serial
            self._status.width = width
            self._status.height = height
            self._status.biases = biases

        raw_path: Path | None = None
        rate_t0 = time.time()
        rate_events = 0

        try:
            for events in iterator:
                if self._stop.is_set():
                    break

                # 録画の開始・停止要求を処理する
                with self._lock:
                    want_start = self._want_record
                    want_stop = self._want_stop
                    self._want_record = None
                    self._want_stop = False

                if want_start is not None:
                    d = self.recordings_dir / want_start
                    d.mkdir(parents=True, exist_ok=True)
                    raw_path = d / "events.raw"
                    if events_stream.log_raw_data(str(raw_path)):
                        with self._lock:
                            self._status.recording = True
                            self._status.recording_id = want_start
                            self._status.started_at = time.time()
                            self._status.bytes_written = 0
                    else:
                        raw_path = None
                        with self._lock:
                            self._status.error = "log_raw_data に失敗しました"

                if want_stop:
                    events_stream.stop_log_raw_data()
                    with self._lock:
                        self._status.recording = False
                        self._status.recording_id = None
                        self._status.started_at = None
                    raw_path = None

                n = len(events)
                if n:
                    rate_events += n
                    frame_gen.process_events(events)

                now = time.time()
                if now - rate_t0 >= 0.5:
                    dt = now - rate_t0
                    size = raw_path.stat().st_size if raw_path and raw_path.exists() else 0
                    with self._lock:
                        prev = self._status.bytes_written
                        self._status.event_rate = rate_events / dt
                        self._status.total_events += rate_events
                        self._status.bytes_written = size
                        self._status.write_rate = max(0.0, (size - prev) / dt)
                    rate_events = 0
                    rate_t0 = now
        finally:
            try:
                events_stream.stop_log_raw_data()
            except Exception:  # noqa: BLE001
                pass
            with self._lock:
                self._status.recording = False
                self._status.connected = False

    def _on_frame(self, ts: int, frame: np.ndarray) -> None:
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
        if not ok:
            return
        with self._lock:
            self._jpeg = buf.tobytes()
        self._frame_event.set()
