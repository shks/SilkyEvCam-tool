"""recorder/scheduler.py の判定ロジック（純関数部分）のテスト.

    .venv/bin/python -m pytest tests/ -q

実カメラは不要。スレッドや API は結合部が薄いので、間違いが入り込みやすい
時刻計算（夜跨ぎ・グリッド境界・時間帯の終了）だけを固める。
"""

import datetime as dt

from recorder.scheduler import (ScheduleConfig, chunk_end, in_window,
                                validate_config, window_end_at)


def t(hhmm: str, day: int = 15) -> dt.datetime:
    h, m = hhmm.split(":")
    return dt.datetime(2026, 8, day, int(h), int(m), 0)


# ── in_window ──────────────────────────────────────────────────────────
def test_daytime_window():
    assert not in_window(t("07:59"), "08:00", "18:00")
    assert in_window(t("08:00"), "08:00", "18:00")
    assert in_window(t("17:59"), "08:00", "18:00")
    assert not in_window(t("18:00"), "08:00", "18:00")     # 終了ちょうどは含まない


def test_overnight_window():
    # 22:00 → 06:00 は夜を跨ぐ
    assert in_window(t("22:00"), "22:00", "06:00")
    assert in_window(t("23:59"), "22:00", "06:00")
    assert in_window(t("00:00"), "22:00", "06:00")
    assert in_window(t("05:59"), "22:00", "06:00")
    assert not in_window(t("06:00"), "22:00", "06:00")
    assert not in_window(t("12:00"), "22:00", "06:00")


def test_always_window():
    # start == end は常時録画
    assert in_window(t("00:00"), "09:00", "09:00")
    assert in_window(t("09:00"), "09:00", "09:00")
    assert in_window(t("23:59"), "09:00", "09:00")


# ── window_end_at ──────────────────────────────────────────────────────
def test_window_end_daytime():
    assert window_end_at(t("10:00"), "08:00", "18:00") == t("18:00")


def test_window_end_overnight_before_midnight():
    # 23:00 にいるなら、終了は「明日の」06:00
    assert window_end_at(t("23:00"), "22:00", "06:00") == t("06:00", day=16)


def test_window_end_overnight_after_midnight():
    # 03:00 にいるなら、終了は「今日の」06:00
    assert window_end_at(t("03:00"), "22:00", "06:00") == t("06:00")


# ── chunk_end ──────────────────────────────────────────────────────────
def test_chunk_aligns_to_wall_clock():
    # 08:03 開始・10 分チャンク → 最初の境界は 08:10（壁時計に揃う）
    assert chunk_end(t("08:03"), 10, t("18:00")) == t("08:10")


def test_chunk_on_boundary_goes_to_next():
    # 境界ちょうどに開始したら次の境界まで（長さゼロのチャンクを作らない）
    assert chunk_end(t("08:10"), 10, t("18:00")) == t("08:20")


def test_chunk_clipped_by_window_end():
    # 時間帯の終了が先に来るなら短く切る
    assert chunk_end(t("17:55"), 10, t("18:00")) == t("18:00")


def test_chunk_60min():
    assert chunk_end(t("09:30"), 60, t("18:00")) == t("10:00")


# ── validate_config ────────────────────────────────────────────────────
def test_validate_ok():
    assert validate_config(vars(ScheduleConfig())) == []
    assert validate_config(dict(start="22:00", end="06:00", chunk_minutes=5,
                                min_free_gb=0)) == []


def test_validate_rejects_bad_values():
    assert validate_config(dict(start="8:00", end="18:00", chunk_minutes=10,
                                min_free_gb=2))          # H:MM は不可（HH:MM のみ）
    assert validate_config(dict(start="08:00", end="24:00", chunk_minutes=10,
                                min_free_gb=2))          # 24:00 は不可
    assert validate_config(dict(start="08:00", end="18:00", chunk_minutes=0,
                                min_free_gb=2))          # 0 分は不可
    assert validate_config(dict(start="08:00", end="18:00", chunk_minutes=10,
                                min_free_gb=-1))         # 負の閾値は不可
