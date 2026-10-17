#!/usr/bin/env python3
"""chaos_daemon_child.py —— 混沌测试的 daemon 子进程（由 test_chaos.py 拉起）

跑真实的 sm_daemon 主循环，仅两处测试性替换：
  1) do_set_guarded 换成随机耗时的假点击（绝不碰真实开关）；
  2) save_last_seq 包一层：把每次执行的 seq 追加到 CHAOS_EXEC_LOG，
     供父进程断言「同一 seq 全程至多执行一次」（C1）与水位单调（C2）。
子进程本地把 REQUEST_STALE_AFTER 收紧到 3 秒，让 stale 回收在混沌时间窗内
真实发生（这是生产语义不变、只调测试时钟）。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))
import sm_daemon as sd   # noqa: E402


def main() -> int:
    td = os.environ["CHAOS_DIR"]
    sd.DIR = td
    sd.REQ_PATH = os.path.join(td, "request.json")
    sd.INFLIGHT_PATH = os.path.join(td, "request.inflight")
    sd.RESULT_PATH = os.path.join(td, "result.json")
    sd.SEQ_PATH = os.path.join(td, "last-seq.json")
    sd.LOG_PATH = os.path.join(td, "chaos-daemon.log")
    # 注意：REQUEST_STALE_AFTER 只被 guard 消费，这里改它没有效果——
    # guard 侧的收紧由 test_chaos.py 对 stage_manager_guard 模块做
    exec_log = os.environ["CHAOS_EXEC_LOG"]
    sd.log("chaos daemon child started")

    def fake_do_set(desired):
        time.sleep(0.05 + (int(time.time() * 1000) % 40) / 100.0)  # 50–450ms 伪随机
        return True, "chaos"

    sd.do_set_guarded = fake_do_set
    orig_save = sd.save_last_seq

    def save(seq: int) -> None:
        with open(exec_log, "a", encoding="utf-8") as f:
            f.write(f"{seq}\n")
        orig_save(seq)

    sd.save_last_seq = save
    sd.log("chaos daemon child started")
    while True:
        try:
            sd.process_once()
        except Exception as e:      # noqa: BLE001 —— 主循环兜底，混沌下必须活着
            try:
                sd.log(f"chaos loop error: {type(e).__name__}")
            except Exception:
                pass
        time.sleep(0.1)


if __name__ == "__main__":
    sys.exit(main())
