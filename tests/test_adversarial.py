#!/usr/bin/env python3
"""test_adversarial.py —— 对抗性测试：主动攻击账本协议与状态机

与 test_stage_manager_guard.py（正常行为回归）互补。这里假设攻击者控制
SMG_HOME 下的所有文件内容（半截 JSON、错误类型、伪造 seq、未来时间戳、
未知 action……），验证守卫在敌意输入下的三条底线：

  I1 账本不可信时绝不动 GUI：状态/探测含糊 → 中止（rc=2），绝不派发。
  I2 敌意结果文件绝不能翻转归属：字符串 "false" 不得当成功、伪造 seq
     不得触发无关标记的回滚。
  I3 敌意输入可以导致"这轮不做事"，但不允许崩溃退出码之外的静默错账。

每条攻击 = 安排敌意现场 → 跑 run()（GUI 全打桩、只记录）→ 断言底线。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

results: list[tuple[bool, str]] = []


def check(cond: bool, msg: str) -> None:
    results.append((bool(cond), msg))


def main() -> int:
    import importlib
    mod = importlib.import_module("stage_manager_guard")
    real_set_stage_manager = mod.set_stage_manager   # E4 要走真实派发/回收路径

    def hostile_round(state_raw: str | None, *, screens=3, pref=True,
                      result_raw: str | None = None,
                      inflight_raw: str | None = None,
                      request_raw: str | None = None,
                      stub_set=None, use_real=False):
        """在临时目录里摆好敌意现场并跑一轮。返回 (rc, saved_state, calls)。"""
        with tempfile.TemporaryDirectory() as td:
            mod.STATE_PATH = str(pathlib.Path(td) / "state.json")
            mod.REQ_DIR = td
            mod.REQ_PATH = str(pathlib.Path(td) / "request.json")
            mod.INFLIGHT_PATH = str(pathlib.Path(td) / "request.inflight")
            mod.RESULT_PATH = str(pathlib.Path(td) / "result.json")
            if state_raw is not None:
                pathlib.Path(mod.STATE_PATH).write_text(state_raw, encoding="utf-8")
            if result_raw is not None:
                pathlib.Path(mod.RESULT_PATH).write_text(result_raw, encoding="utf-8")
            if inflight_raw is not None:
                pathlib.Path(mod.INFLIGHT_PATH).write_text(inflight_raw, encoding="utf-8")
            if request_raw is not None:
                pathlib.Path(mod.REQ_PATH).write_text(request_raw, encoding="utf-8")
            mod.screen_count = lambda: screens
            mod.pref_enabled = lambda: pref
            calls: list[bool] = []

            def default_stub(desired, dry_run=False, seq=None):
                calls.append(desired)
                return (True, "stub")

            mod.set_stage_manager = (real_set_stage_manager if use_real
                                     else (stub_set or default_stub))
            rc = mod.run(dry_run=False)
            saved = json.loads(pathlib.Path(mod.STATE_PATH).read_text(encoding="utf-8")) \
                if pathlib.Path(mod.STATE_PATH).is_file() else {}
            req_left = pathlib.Path(mod.REQ_PATH).read_text(encoding="utf-8") \
                if pathlib.Path(mod.REQ_PATH).is_file() else None
            inflight_left = pathlib.Path(mod.INFLIGHT_PATH).read_text(encoding="utf-8") \
                if pathlib.Path(mod.INFLIGHT_PATH).is_file() else None
            return rc, saved, calls, req_left, inflight_left

    now = datetime.now(timezone.utc)
    fresh_at = now.isoformat()
    stale_at = (now - timedelta(seconds=999)).isoformat()

    # ---- A 类：账本文件本身是敌意的 ------------------------------------

    # A1 半截 JSON：必须隔离留档、当空账本处理，且多屏+开着时照常决策
    rc, saved, calls, _, _ = hostile_round('{"closed_by_script": tru')
    check(rc == 0 and calls == [False] and saved.get("closed_by_script") is True,
          "A1 半截账本：隔离后按无账本正常决策（多屏关）")

    # A2 closed_by_script 是真值字符串 "yes"：账本值必须类型可信，
    #    否则单屏会把用户没开过的台前调度当"待恢复"处理
    rc, saved, calls, _, _ = hostile_round(
        json.dumps({"closed_by_script": "yes", "pref_before_close": "1"}),
        screens=1, pref=True)
    check(calls == [] and saved.get("closed_by_script") is None,
          "A2 字符串账本值必须整份隔离（不认账、不驱动恢复决策）")

    # A3 state.json 是目录：读失败必须走隔离/中止路径，不得崩溃成未定义行为
    rc, saved, calls, _, _ = hostile_round(None, screens=1, pref=False, stub_set=None)
    # （A3 用目录的变体在下方单独跑，hostile_round 只能写文本文件）
    with tempfile.TemporaryDirectory() as td:
        mod.STATE_PATH = str(pathlib.Path(td) / "state.json")
        mod.REQ_DIR = td
        mod.REQ_PATH = str(pathlib.Path(td) / "request.json")
        mod.INFLIGHT_PATH = str(pathlib.Path(td) / "request.inflight")
        mod.RESULT_PATH = str(pathlib.Path(td) / "result.json")
        (pathlib.Path(td) / "state.json").mkdir()          # 目录冒充文件
        mod.screen_count = lambda: 1
        mod.pref_enabled = lambda: False
        mod.set_stage_manager = lambda d, dry_run=False, seq=None: (
            calls.append(d) if False else (True, "stub"))
        rc_dir = mod.run(dry_run=False)
    check(rc_dir in (0, 2), f"A3 state 是目录时必须安全收场（实际 rc={rc_dir}）")

    # A4 账本 seq 是字符串 "7"：与结果里的整数 7 不得判等（弱类型对账
    #    会把伪造的字符串账本和真结果配对）→ 整份隔离，绝无配对
    rc, saved, calls, _, _ = hostile_round(
        json.dumps({"closed_by_script": True, "pref_before_close": True,
                    "closed_by_seq": "7"}),
        screens=3, pref=False,
        result_raw=json.dumps({"ok": False, "msg": "failed", "seq": 7, "at": fresh_at}))
    check(rc == 0 and saved.get("closed_by_seq") != 7,
          "A4 字符串 seq 账本必须整份隔离（不得与整数结果配对回滚）")

    # A5 空文件账本：等同无账本
    rc, saved, calls, _, _ = hostile_round("", screens=3, pref=False)
    check(rc == 0 and calls == [], "A5 空账本：多屏但已关（无账本）→ 不动作")

    # ---- B 类：结果文件是敌意的 ----------------------------------------

    # B1 ok 是字符串 "false"：布尔化后是真值 → 会被当成功！
    #    底线：非 bool 的 ok 不得采信
    rc, saved, calls, _, _ = hostile_round(
        json.dumps({"closed_by_script": True, "pref_before_close": True,
                    "closed_by_seq": 7}),
        screens=3, pref=False,
        result_raw=json.dumps({"ok": "false", "msg": "hostile", "seq": 7,
                               "at": fresh_at}))
    check(saved.get("closed_by_script") is True,
          "B1 字符串 \"false\" 不得被当成 False 成功/失败依据（ok 必须验 bool）")

    # B2 伪造 seq 指向别的请求：不得回滚不相关的标记
    rc, saved, calls, _, _ = hostile_round(
        json.dumps({"closed_by_script": True, "pref_before_close": True,
                    "closed_by_seq": 111}),
        screens=3, pref=False,
        result_raw=json.dumps({"ok": False, "msg": "failed", "seq": 222,
                               "at": fresh_at}))
    check(saved.get("closed_by_script") is True,
          "B2 seq 不匹配的失败结果不得回滚他人标记")

    # B3 reconcile sweep 同样不得被字符串 ok 驱动（is False 恒等判断）。
    # 场景必须选"多屏已关"（早退分支只看标记存留），单屏会走合法恢复路径
    # 而与敌意结果无关
    rc, saved, calls, _, _ = hostile_round(
        json.dumps({"closed_by_script": True, "pref_before_close": True,
                    "closed_by_seq": 7}),
        screens=3, pref=False,
        result_raw=json.dumps({"ok": "false", "msg": "hostile", "seq": 7,
                               "at": fresh_at}))
    check(saved.get("closed_by_script") is True,
          "B3 sweep 的失败判定必须恒等比较（字符串 ok 不得触发回滚）")

    # B4 未来时间戳的结果：时间戳比请求新 → 语义上可采信，但不得崩溃
    rc, saved, calls, _, _ = hostile_round(
        json.dumps({"closed_by_script": True, "pref_before_close": True,
                    "closed_by_seq": 7}),
        screens=3, pref=False,
        result_raw=json.dumps({"ok": False, "msg": "from the future", "seq": 7,
                               "at": (now + timedelta(hours=1)).isoformat()}))
    check(rc in (0, 1), f"B4 未来时间戳结果安全收场（rc={rc}）")

    # B5 result 是目录：等待/对账都不得崩溃
    with tempfile.TemporaryDirectory() as td:
        mod.STATE_PATH = str(pathlib.Path(td) / "state.json")
        mod.REQ_DIR = td
        mod.REQ_PATH = str(pathlib.Path(td) / "request.json")
        mod.INFLIGHT_PATH = str(pathlib.Path(td) / "request.inflight")
        mod.RESULT_PATH = str(pathlib.Path(td) / "result.json")
        pathlib.Path(mod.STATE_PATH).write_text(json.dumps(
            {"closed_by_script": True, "pref_before_close": True,
             "closed_by_seq": 7}), encoding="utf-8")
        (pathlib.Path(td) / "result.json").mkdir()
        mod.screen_count = lambda: 3
        mod.pref_enabled = lambda: True
        mod.set_stage_manager = lambda d, dry_run=False, seq=None: (
            False, "in-flight (result will be collected on next poll)")
        rc_dir = mod.run(dry_run=False)
    check(rc_dir == 0, f"B5 result 是目录时安全收场（rc={rc_dir}）")

    # ---- C 类：请求/inflight 文件是敌意的 ------------------------------

    # C1 request.json 是半截 JSON：等同无待办，照常决策
    rc, saved, calls, _, _ = hostile_round(None, screens=3, pref=True,
                                     request_raw='{"action": "of')
    check(rc == 0 and calls == [False],
          "C1 半截请求文件：隔离后按无待办正常决策")

    # C2 未知 action 的 inflight：不得永久卡死决策（必须按 stale 回收放行）
    rc, saved, calls, _, _ = hostile_round(None, screens=3, pref=True,
                                     inflight_raw=json.dumps(
                                         {"action": "foo", "seq": 5, "at": stale_at}))
    check(rc == 0 and calls == [False],
          "C2 未知 action 的 stale inflight 必须被回收放行（不卡死）")

    # C3 seq 是字符串的 inflight：等待不得崩溃、最终按 in-flight 收场
    rc, saved, calls, _, _ = hostile_round(
        json.dumps({"closed_by_script": True, "pref_before_close": True,
                    "closed_by_seq": 7}),
        screens=3, pref=True,
        inflight_raw=json.dumps({"action": "off", "seq": "7", "at": fresh_at}))
    check(rc == 0, f"C3 字符串 seq 的 inflight 安全收场（rc={rc}）")

    # C4 探测返回字符串屏数：计数不可信 → 中止，绝不当 1/3 用
    with tempfile.TemporaryDirectory() as td:
        mod.STATE_PATH = str(pathlib.Path(td) / "state.json")
        mod.REQ_DIR = td
        mod.REQ_PATH = str(pathlib.Path(td) / "request.json")
        mod.INFLIGHT_PATH = str(pathlib.Path(td) / "request.inflight")
        mod.RESULT_PATH = str(pathlib.Path(td) / "result.json")
        mod.screen_count = lambda: "3"           # 敌意类型
        mod.pref_enabled = lambda: True
        mod.set_stage_manager = lambda d, dry_run=False, seq=None: (
            (_ for _ in ()).throw(AssertionError("计数类型不可信时绝不允许动 GUI")))
        rc_str = mod.run(dry_run=False)
    check(rc_str == 2, f"字符串屏数必须中止（rc={rc_str}）")

    # C5 探测返回浮点屏数：同上（int 严格判定，2.0 也不是合法计数）
    with tempfile.TemporaryDirectory() as td:
        mod.STATE_PATH = str(pathlib.Path(td) / "state.json")
        mod.REQ_DIR = td
        mod.REQ_PATH = str(pathlib.Path(td) / "request.json")
        mod.INFLIGHT_PATH = str(pathlib.Path(td) / "request.inflight")
        mod.RESULT_PATH = str(pathlib.Path(td) / "result.json")
        mod.screen_count = lambda: 2.0
        mod.pref_enabled = lambda: True
        mod.set_stage_manager = lambda d, dry_run=False, seq=None: (
            (_ for _ in ()).throw(AssertionError("浮点计数绝不允许驱动决策")))
        rc_float = mod.run(dry_run=False)
    check(rc_float == 2, f"浮点屏数必须中止（rc={rc_float}）")

    # B6 seq 的弱类型陷阱：Python 里 True==1、7.0==7——伪造 bool/浮点 seq
    #    不得与整数账本配对（采信与对账都必须严格 int 且非 bool）
    rc, saved, calls, _, _ = hostile_round(
        json.dumps({"closed_by_script": True, "pref_before_close": True,
                    "closed_by_seq": 1}),
        screens=3, pref=False,
        result_raw=json.dumps({"ok": False, "msg": "hostile bool seq",
                               "seq": True, "at": fresh_at}))
    check(saved.get("closed_by_script") is True,
          "B6 布尔 seq（True==1）不得触发账本回滚")
    rc, saved, calls, _, _ = hostile_round(
        json.dumps({"closed_by_script": True, "pref_before_close": True,
                    "closed_by_seq": 7}),
        screens=3, pref=False,
        result_raw=json.dumps({"ok": False, "msg": "hostile float seq",
                               "seq": 7.0, "at": fresh_at}))
    check(saved.get("closed_by_script") is True,
          "B6 浮点 seq（7.0==7）不得触发账本回滚")

    # ---- E 类：红队席补充的攻击面（guard 侧） ---------------------------

    # E1 字符串 ok 在 wait_result 也必须被拒（红队 #4：sweep 修了、等待分支漏了）
    with tempfile.TemporaryDirectory() as td:
        mod.RESULT_PATH = str(pathlib.Path(td) / "result.json")
        pathlib.Path(mod.RESULT_PATH).write_text(json.dumps(
            {"ok": "false", "msg": "fake failure", "seq": 7,
             "at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
        ok_w, msg_w = mod.wait_result(time.time() - 5, timeout=0, expected_seq=7)
    check(ok_w is False and "in-flight" in msg_w,
          f"E1 wait_result 必须拒收字符串 ok（实际 ok={ok_w} msg={msg_w!r}）")

    # E2 未来时间戳的 inflight 不得永远挡住 off 门闩（红队 #11）
    rc, saved, calls, _, infl_left = hostile_round(
        None, screens=3, pref=True,
        inflight_raw=json.dumps({"action": "restore", "seq": 9,
                                 "at": "2999-01-01T00:00:00+00:00"}))
    check(rc == 0 and calls == [False] and infl_left is None,
          "E2 at=2999 年的 inflight 必须按畸形回收，off 照常进行")

    # E3 账本有标记但没有可信恢复目标（旧版本残留）：宁可不动也不猜（红队 #5）
    rc, saved, calls, _, _ = hostile_round(
        json.dumps({"closed_by_script": True}), screens=1, pref=True)
    check(rc == 2 and calls == [],
          "E3 缺 pref_before_close 必须拒猜（不得把 False 当默认恢复值）")

    # E4 旧 request.json 过期回收不得连带删掉新鲜 inflight（红队 #7）。
    #     走真实 set_stage_manager：旧 REQ(seq6, at=2000) 被 stale 回收，
    #     新鲜 INFLIGHT(seq7) 必须原地存活
    rc, saved, calls, req_left, infl_left = hostile_round(
        None, screens=3, pref=True, use_real=True,
        request_raw=json.dumps({"action": "off", "seq": 6,
                                "at": "2000-01-01T00:00:00+00:00"}),
        inflight_raw=json.dumps({"action": "off", "seq": 7, "at": fresh_at}))
    check(infl_left is not None and '"seq": 7' in (infl_left or ""),
          "E4 回收旧请求不得连带删除新鲜 inflight（seq 7 租约存活）")

    # E5 非 dict 请求（数组）：隔离不崩、决策照常（红队 #10 guard 侧）
    rc, saved, calls, _, _ = hostile_round(
        None, screens=3, pref=True, request_raw="[1,2,3]")
    check(rc == 0 and calls == [False],
          "E5 数组请求必须隔离留档后按无待办继续（不崩整轮）")

    # E6 屏数 0 / -1：无物理意义，宁可中止（红队 #13 边界）
    for bad in (0, -1):
        rc, saved, calls, _, _ = hostile_round(
            json.dumps({"closed_by_script": True, "pref_before_close": True,
                        "closed_by_seq": 7}), screens=bad, pref=False)
        check(rc == 2 and calls == [],
              f"E6 屏数 {bad} 必须中止（不进单屏恢复分支）")

    # E7 符号链接攻击（红队 #8）：.tmp 被换成指向受害文件的链接时，
    # 写入必须拒走链接（O_NOFOLLOW）→ 派发失败 → 标记回滚，受害文件原样
    with tempfile.TemporaryDirectory() as td:
        victim = pathlib.Path(td) / "victim.txt"
        victim.write_text("precious", encoding="utf-8")
        mod.STATE_PATH = str(pathlib.Path(td) / "state.json")
        mod.REQ_DIR = td
        mod.REQ_PATH = str(pathlib.Path(td) / "request.json")
        mod.INFLIGHT_PATH = str(pathlib.Path(td) / "request.inflight")
        mod.RESULT_PATH = str(pathlib.Path(td) / "result.json")
        os.symlink(victim, mod.REQ_PATH + ".tmp")
        mod.screen_count = lambda: 3
        mod.pref_enabled = lambda: True
        mod.set_stage_manager = real_set_stage_manager
        rc_link = mod.run(dry_run=False)
        saved_link = json.loads(pathlib.Path(mod.STATE_PATH).read_text(encoding="utf-8")) \
            if pathlib.Path(mod.STATE_PATH).is_file() else {}
        victim_intact = victim.read_text(encoding="utf-8") == "precious"
    check(rc_link == 2 and victim_intact
          and "closed_by_script" not in saved_link,
          f"E7 .tmp 符号链接必须拒写（rc={rc_link}, victim 原样={victim_intact}）")

    # ---- D 类：daemon 侧敌意请求（红队 #2/#14/#10/#1 的执行端） ---------
    import sm_daemon as sd
    _d_orig = {k: getattr(sd, k) for k in
               ("DIR", "REQ_PATH", "INFLIGHT_PATH", "RESULT_PATH",
                "SEQ_PATH", "LOG_PATH", "do_set_guarded")}

    def daemon_round(req_obj, last_seq=None):
        with tempfile.TemporaryDirectory() as td:
            sd.DIR = td
            sd.REQ_PATH = str(pathlib.Path(td) / "request.json")
            sd.INFLIGHT_PATH = str(pathlib.Path(td) / "request.inflight")
            sd.RESULT_PATH = str(pathlib.Path(td) / "result.json")
            sd.SEQ_PATH = str(pathlib.Path(td) / "last-seq.json")
            sd.LOG_PATH = str(pathlib.Path(td) / "daemon-test.log")
            pathlib.Path(sd.REQ_PATH).write_text(
                req_obj if isinstance(req_obj, str) else json.dumps(req_obj),
                encoding="utf-8")
            if last_seq is not None:
                pathlib.Path(sd.SEQ_PATH).write_text(
                    json.dumps({"seq": last_seq}), encoding="utf-8")
            executed: list = []

            def fake_do_set(desired):
                executed.append(desired)
                return (True, "stub")

            sd.do_set_guarded = fake_do_set
            try:
                crashed = None
                try:
                    sd.process_once()
                except Exception as e:      # noqa: BLE001 —— 对抗测试就要抓崩溃
                    crashed = repr(e)
                result = json.loads(pathlib.Path(sd.RESULT_PATH).read_text(encoding="utf-8")) \
                    if pathlib.Path(sd.RESULT_PATH).is_file() else None
                watermark = json.loads(pathlib.Path(sd.SEQ_PATH).read_text(encoding="utf-8")).get("seq") \
                    if pathlib.Path(sd.SEQ_PATH).is_file() else None
                inflight_left = pathlib.Path(sd.INFLIGHT_PATH).exists()
            finally:
                for k, v in _d_orig.items():
                    setattr(sd, k, v)
            return executed, result, watermark, inflight_left, crashed

    # D1 巨数 seq 毒化水位（红队 #2）：不执行、不落水位、回报失败、清 inflight
    executed, result, watermark, infl_left, crashed = daemon_round(
        {"action": "off", "seq": 10**50})
    check(executed == [] and watermark is None and infl_left is False
          and crashed is None and result is not None and result.get("ok") is False,
          f"D1 巨数 seq 必须拒收且不污染水位（executed={executed}, wm={watermark}）")

    # D2 非法 seq 变体：bool / 浮点 / 负数（红队 #14）
    for bad_seq in (True, 7.0, -1):
        executed, result, watermark, infl_left, crashed = daemon_round(
            {"action": "off", "seq": bad_seq})
        check(executed == [] and infl_left is False and crashed is None,
              f"D2 非法 seq {bad_seq!r} 必须拒收且不崩")

    # D3 非对象请求（红队 #10 daemon 侧）：不崩、清 inflight、回报失败
    executed, result, watermark, infl_left, crashed = daemon_round("[1,2]")
    check(crashed is None and infl_left is False and executed == []
          and result is not None and result.get("ok") is False,
          f"D3 数组请求不得崩 daemon（crashed={crashed}）")

    # D4 stale 跳过也必须清 inflight（红队 #2 残留部分）
    executed, result, watermark, infl_left, crashed = daemon_round(
        {"action": "off", "seq": 5}, last_seq=10)
    check(executed == [] and infl_left is False,
          "D4 stale 跳过后 inflight 必须清掉（不留幽灵待办）")

    # D5 毒化未遂后正常请求照常执行（水位干净）
    executed, result, watermark, infl_left, crashed = daemon_round(
        {"action": "off", "seq": int(datetime.now(timezone.utc).timestamp() * 1e9)})
    check(executed == [False] and watermark is not None,
          "D5 正常 seq 的请求必须照常执行并落水位")

    # D6 restore_to 字符串 "false" 不得当 True（红队 #1 的 daemon 侧防线）
    executed, result, watermark, infl_left, crashed = daemon_round(
        {"action": "restore", "restore_to": "false", "seq": 99})
    check(executed == [False],
          f"D6 restore_to='false' 不得驱动开台前调度（executed={executed}）")

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
