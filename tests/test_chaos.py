#!/usr/bin/env python3
"""test_chaos.py —— 第四层对抗：真进程并发混沌

前三层（回归/对抗/fuzz）的共同盲区：guard 与 daemon 从未以两个真进程在
真实文件上乱序并发——所有交错都是单进程内按剧本走的。这一层把两者真跑起来：

  - 子进程：真实 sm_daemon 主循环（点击打桩+随机耗时，见 chaos_daemon_child.py）
  - 本进程：反复执行真实 guard run()，同时混沌线程随机
    截断账本 / 删结果 / 写垃圾请求 / kill -9 daemon 再拉起

断言跨进程契约（安全性质，非活性——不赌时序，CI 不闪）：
  C1 同一 seq 全程至多被执行一次（跨进程执行去重）
  C2 水位记录全程单调不回退（跨 daemon 重启）
  C3 guard 任何一轮 rc ∈ {0,1,2,4}，不向 harness 裸抛
  C4 混沌结束后账本要么类型可信、要么已被隔离（无弱类型残留）

时长固定（秒级），种子可复现。
"""
from __future__ import annotations

import json
import os
import pathlib
import random
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

results: list[tuple[bool, str]] = []


def check(cond: bool, msg: str) -> None:
    results.append((bool(cond), msg))


def main() -> int:
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 20261017
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0
    rng = random.Random(seed)
    print(f"chaos seed={seed} duration={duration}s")

    import importlib
    mod = importlib.import_module("stage_manager_guard")
    real_set = mod.set_stage_manager
    orig_paths = {k: getattr(mod, k) for k in
                  ("STATE_PATH", "REQ_DIR", "REQ_PATH", "INFLIGHT_PATH",
                   "RESULT_PATH", "REQUEST_TIMEOUT", "REQUEST_STALE_AFTER")}
    orig_seq = mod._LAST_RESULT_SEQ

    child = str(HERE / "chaos_daemon_child.py")
    seq_base = int(datetime.now(timezone.utc).timestamp() * 1e9)
    deadline = time.time() + duration
    rcs: list = []
    chaos_events = 0
    kills = 0

    with tempfile.TemporaryDirectory() as td:
        mod.STATE_PATH = str(pathlib.Path(td) / "state.json")
        mod.REQ_DIR = td
        mod.REQ_PATH = str(pathlib.Path(td) / "request.json")
        mod.INFLIGHT_PATH = str(pathlib.Path(td) / "request.inflight")
        mod.RESULT_PATH = str(pathlib.Path(td) / "result.json")
        mod.REQUEST_TIMEOUT = 2.0
        # guard 侧的 stale 租约也必须收紧（混沌工程席 #2-2：REQUEST_STALE_AFTER
        # 只被 guard 消费，只改 daemon 侧等于没改——回收路径此前从未在混沌窗
        # 内真实触发）。3 秒让 kill-inflight → 回收 → 重派在窗口内真实发生
        mod.REQUEST_STALE_AFTER = 3.0
        pathlib.Path(mod.STATE_PATH).write_text(json.dumps(
            {"closed_by_script": True, "pref_before_close": True,
             "closed_by_seq": seq_base}), encoding="utf-8")
        exec_log = str(pathlib.Path(td) / "exec.log")

        env = dict(os.environ, CHAOS_DIR=td, CHAOS_EXEC_LOG=exec_log)
        proc = subprocess.Popen([sys.executable, child], env=env,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)

        guard_n = 0
        try:
            while time.time() < deadline:
                # guard 一轮：真实派发路径（结果等待由 REQUEST_TIMEOUT=2 兜底）
                mod.screen_count = lambda: rng.choice([1, 2, 2, 3])
                mod.pref_enabled = lambda: rng.choice([True, False, True])
                mod._LAST_RESULT_SEQ = None
                mod.set_stage_manager = real_set
                try:
                    rcs.append(mod.run(dry_run=False))
                except Exception as e:      # noqa: BLE001 —— C3 要抓裸抛
                    rcs.append(f"ESCAPED:{type(e).__name__}:{e}")
                guard_n += 1

                # 混沌注入（随机挑一种，概率合计 ~35%）
                roll = rng.random()
                if roll < 0.08:
                    p = pathlib.Path(mod.STATE_PATH)
                    if p.is_file():
                        raw = p.read_text(encoding="utf-8")
                        p.write_text(raw[:max(0, len(raw) // 2)], encoding="utf-8")
                        chaos_events += 1
                elif roll < 0.14:
                    pathlib.Path(mod.RESULT_PATH).unlink(missing_ok=True)
                    chaos_events += 1
                elif roll < 0.20:
                    pathlib.Path(mod.REQ_PATH).write_text(
                        rng.choice(['{"action": "of', "[1,2]", "", json.dumps(
                            {"action": "off", "seq": 10**60,
                             "at": "2999-01-01T00:00:00+00:00"})]),
                        encoding="utf-8")
                    chaos_events += 1
                elif roll < 0.24 and proc.poll() is None:
                    proc.send_signal(signal.SIGKILL)
                    kills += 1
                    chaos_events += 1
                    proc = subprocess.Popen([sys.executable, child], env=env,
                                            stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL)
                time.sleep(rng.uniform(0.02, 0.15))
        finally:
            proc.kill()
            proc.wait()
            # 末轮兜底：若最后一次注入恰好截断了账本，循环因到期退出后没有
            # 下一轮 load_state 去隔离——C4 会把正确的隔离实现判成假红
            # （混沌工程席 #3-2）。先跑一轮只看 rc，再取终态快照。
            mod.screen_count = lambda: 1
            mod.pref_enabled = lambda: False
            mod.set_stage_manager = real_set
            try:
                mod.run(dry_run=False)
            except Exception:
                pass
            for k, v in orig_paths.items():
                setattr(mod, k, v)
            mod._LAST_RESULT_SEQ = orig_seq

        # ---- 跨进程契约断言 ----
        exec_path = pathlib.Path(exec_log)
        exec_seqs = [int(line) for line in
                     (exec_path.read_text(encoding="utf-8").splitlines()
                      if exec_path.is_file() else [])
                     if line.strip()]
        # C1：同一 seq 全程至多执行一次
        check(len(exec_seqs) == len(set(exec_seqs)),
              f"C1 同一 seq 被执行多次：{[s for s in exec_seqs if exec_seqs.count(s) > 1][:3]}"
              f"（guard 轮数={guard_n}，执行={len(exec_seqs)}）")
        # C2：水位记录全程单调不回退（跨 daemon 重启），且最终落盘水位与
        # 最后一次执行一致（混沌工程席 #1-2：只看日志验不到 save_last_seq
        # 写坏/写丢的水位文件）
        check(all(a <= b for a, b in zip(exec_seqs, exec_seqs[1:])),
              f"C2 水位记录回退：{exec_seqs[:8]}")
        wm_file = None
        wm_path = pathlib.Path(td) / "last-seq.json"
        if wm_path.is_file():
            try:
                wm_file = json.loads(wm_path.read_text(encoding="utf-8")).get("seq")
            except Exception:
                wm_file = "unparseable"
        check(not exec_seqs or wm_file == exec_seqs[-1],
              f"C2b 水位文件与最后执行不符：wm={wm_file!r} last={exec_seqs[-1] if exec_seqs else None}")
        # C3：guard 逃逸集——严格 int（True==1、1.0==1 的弱类型混入不得过）
        # （混沌工程席 #1-3）
        bad = [r for r in rcs
               if not (isinstance(r, int) and not isinstance(r, bool)
                       and r in (0, 1, 2, 4))]
        check(bad == [],
              f"C3 guard 裸抛/异常 rc：{bad[:3]}（共 {len(rcs)} 轮，混沌注入 {chaos_events} 次，kill {kills} 次）")
        # C4：最终账本三态纪律——closed_by_script 缺席（合法清账）或严格
        # bool；pref/seq 缺席或严格类型（混沌工程席 #1-4：in (True, False)
        # 会接受 1/1.0，也会把合法清账终态判红）
        final_raw = None
        p = pathlib.Path(mod.STATE_PATH)
        if p.is_file():
            final_raw = p.read_text(encoding="utf-8")
        ok_ledger = final_raw is None
        if not ok_ledger:
            try:
                d = json.loads(final_raw)
                cbs = d.get("closed_by_script")
                pbc = d.get("pref_before_close")
                seqv = d.get("closed_by_seq")
                ok_ledger = (
                    (cbs is None or cbs is True or cbs is False)
                    and (pbc is None or isinstance(pbc, bool))
                    and (seqv is None or (isinstance(seqv, int)
                                          and not isinstance(seqv, bool))))
            except Exception:
                ok_ledger = False
        check(ok_ledger, f"C4 混沌后账本类型纪律失守：{final_raw!r:.120}")
        # 观测面：这轮混沌真的发生了事（防空转假绿）。阈值取保守下界，
        # 慢机上不闪（混沌工程席 #3-1）
        check(guard_n >= 3 and len(exec_seqs) >= 1 and chaos_events >= 1,
              f"观测面：guard {guard_n} 轮 / 执行 {len(exec_seqs)} / 混沌 {chaos_events}"
              "——低于阈值说明场景没跑起来")

    failed = [m for ok, m in results if not ok]
    for ok, m in results:
        print(("PASS  " if ok else "FAIL  ") + m)
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("\nFAILED:")
        for m in failed:
            print("  - " + m)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
