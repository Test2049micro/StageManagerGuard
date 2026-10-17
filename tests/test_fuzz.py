#!/usr/bin/env python3
"""test_fuzz.py —— 状态机属性模糊测试（对抗·深度层）

三层测试的第三层：回归（正常行为）→ 对抗（手工敌意现场）→ 这里的
随机组合轰炸。每轮随机生成一个世界（屏数/pref/账本/请求/结果/桩行为），
跑一轮 guard 或 daemon，然后断言**安全契约不变式**：

  I1 不可信输入绝不驱动 GUI：screens 非 int≥1 或 pref 非 bool → rc=2 且零派发
  I2 方向绝不发明：多屏只派 off；单屏只派 restore(账本 pref_before_close)；
     账本目标不可信时绝不派发
  I3 敌意文件不允许逃逸：rc 必须落在 {0,1,2,4}，异常不得裸抛
  I4 账本自洽：guard 写下的标记必须是完整五元组（含严格 int 的 closed_by_seq），
     绝不写出弱类型账本值
  I5 daemon：只执行「合法 seq 且新于水位」的请求；水位严格单调不回退；
     process_once 返回后 inflight 必然被清理（任何路径）

种子固定打印；复现：python3 tests/test_fuzz.py <seed>
"""
from __future__ import annotations

import json
import os
import pathlib
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

results: list[tuple[bool, str]] = []


def check(cond: bool, msg: str) -> None:
    results.append((bool(cond), msg))


NOW = datetime.now(timezone.utc)


def iso(offset_seconds: float) -> str:
    return (NOW + timedelta(seconds=offset_seconds)).isoformat()


def gen_ledger(rng: random.Random):
    """生成一个敌意/合法混合的账本文件内容（str 或 None=不存在）。"""
    kind = rng.choice(["absent", "valid_marker", "valid_marker_seq", "legacy",
                       "corrupt", "wrong_types", "empty", "array", "contradictory"])
    if kind == "absent":
        return None
    if kind == "corrupt":
        return '{"closed_by_script": tru'
    if kind == "empty":
        return ""
    if kind == "array":
        return "[1,2]"
    if kind == "valid_marker":
        return json.dumps({"closed_by_script": True, "pref_before_close": True})
    if kind == "valid_marker_seq":
        return json.dumps({"closed_by_script": True, "pref_before_close": True,
                           "closed_by_seq": rng.randrange(1, 1 << 62)})
    if kind == "legacy":
        return json.dumps({"closed_by_script": True})
    if kind == "wrong_types":
        return json.dumps({"closed_by_script": rng.choice(["yes", 1]),
                           "pref_before_close": rng.choice(["1", None]),
                           "closed_by_seq": rng.choice(["7", 7.0, True, -5])})
    # contradictory：账本自称是工具关的、目标却是 False 且标记 seq 为 0
    return json.dumps({"closed_by_script": True, "pref_before_close": False,
                       "closed_by_seq": 0})


def gen_request(rng: random.Random):
    kind = rng.choice(["absent", "fresh_off", "fresh_restore", "stale",
                       "future", "malformed", "non_dict", "unknown_action",
                       "bad_seq", "no_at"])
    if kind == "absent":
        return None
    if kind == "malformed":
        return '{"action": "of'
    if kind == "non_dict":
        return "[1,2]"
    base = {"action": {"fresh_off": "off", "stale": "off", "future": "off",
                       "unknown_action": rng.choice(["foo", ""]), "bad_seq": "off",
                       "no_at": "off"}.get(kind, "restore"),
            "seq": rng.randrange(1, 1 << 40)}
    if kind == "bad_seq":
        base["seq"] = rng.choice([10**50, True, 7.0, -1, 0, "9"])
    base["at"] = {"stale": iso(-10**6), "future": iso(10**8),
                  "no_at": None}.get(kind, iso(-5))
    return json.dumps(base)


def gen_result(rng: random.Random):
    kind = rng.choice(["absent", "ok_true", "ok_false", "ok_string",
                       "bad_seq", "future", "ancient", "no_at", "non_dict"])
    if kind == "absent":
        return None
    if kind == "non_dict":
        return '"just a string"'
    base = {"ok": {"ok_true": True, "ok_false": False,
                   "ok_string": rng.choice(["false", "true", 1])}.get(kind, False),
            "msg": "fuzz",
            "seq": rng.choice([7, 7.0, True, "7", 10**50]) if kind == "bad_seq"
            else rng.randrange(1, 1 << 40)}
    base["at"] = {"future": iso(10**8), "ancient": iso(-10**9),
                  "no_at": None}.get(kind, iso(-30))
    return json.dumps(base)


SCREENS_POOL = [None, 0, 1, 2, 3, True, False, "3", 2.0, -1]
PREF_POOL = [None, True, False, "true", 1, 0]


def guard_round(rng: random.Random, mod, n: int, seed: int, real_set):
    """跑一轮随机化的 guard。返回不变式断言用的观测。"""
    screens = rng.choice(SCREENS_POOL)
    pref = rng.choice(PREF_POOL)
    ledger_raw = gen_ledger(rng)
    request_raw = gen_request(rng)
    inflight_raw = gen_request(rng)
    result_raw = gen_result(rng)
    stub_mode = rng.choice(["ok", "ok", "fail", "inflight", "raise"])
    use_real = rng.random() < 0.15
    trusted_screens = isinstance(screens, int) and not isinstance(screens, bool) \
        and screens >= 1
    trusted_pref = isinstance(pref, bool)

    ledger_obj = None
    if isinstance(ledger_raw, str):
        try:
            ledger_obj = json.loads(ledger_raw)
        except Exception:
            ledger_obj = None
    # 与 load_state 的类型门槛对齐（不变式审计 #3-1）：账本要过全部三类
    # 类型检查，restore 方向才谈得上"可信"。seq=0 合法（load_state 不要求
    # >0；guard 自己只产 time_ns，永不撞上）——内容与账本一致的方向是
    # 契约内派发，不算发明方向
    trusted_target = (
        isinstance(ledger_obj, dict)
        and ledger_obj.get("closed_by_script") is True
        and isinstance(ledger_obj.get("pref_before_close"), bool)
        and (ledger_obj.get("closed_by_seq") is None
             or (isinstance(ledger_obj.get("closed_by_seq"), int)
                 and not isinstance(ledger_obj.get("closed_by_seq"), bool))))

    with tempfile.TemporaryDirectory() as td:
        mod.STATE_PATH = str(pathlib.Path(td) / "state.json")
        mod.REQ_DIR = td
        mod.REQ_PATH = str(pathlib.Path(td) / "request.json")
        mod.INFLIGHT_PATH = str(pathlib.Path(td) / "request.inflight")
        mod.RESULT_PATH = str(pathlib.Path(td) / "result.json")
        for raw, path in ((ledger_raw, mod.STATE_PATH),
                          (request_raw, mod.REQ_PATH),
                          (inflight_raw, mod.INFLIGHT_PATH),
                          (result_raw, mod.RESULT_PATH)):
            if raw is not None:
                pathlib.Path(path).write_text(raw, encoding="utf-8")
        mod.screen_count = lambda: screens
        mod.pref_enabled = lambda: pref
        calls: list[bool] = []

        def stub(desired, dry_run=False, seq=None):
            calls.append(desired)
            if stub_mode == "raise":
                raise OSError("fuzz")
            return {"ok": (True, "stub"),
                    "fail": (False, "failed after 5 attempts"),
                    "inflight": (False,
                                 "in-flight (result will be collected)")
                    }[stub_mode]

        mod.set_stage_manager = real_set if use_real else stub
        _orig_to = mod.REQUEST_TIMEOUT
        mod.REQUEST_TIMEOUT = 1 if use_real else _orig_to
        orig_seq = mod._LAST_RESULT_SEQ
        mod._LAST_RESULT_SEQ = None
        crashed = None
        try:
            rc = mod.run(dry_run=False)
        except Exception as e:      # noqa: BLE001 —— fuzz 要抓逃逸异常
            crashed = repr(e)
            rc = None
        finally:
            mod.REQUEST_TIMEOUT = _orig_to
            mod._LAST_RESULT_SEQ = orig_seq
        try:
            saved = json.loads(pathlib.Path(mod.STATE_PATH).read_text(encoding="utf-8")) \
                if pathlib.Path(mod.STATE_PATH).is_file() else {}
        except Exception:
            saved = {}

    # I3：run() 这一层不允许逃逸——rc 只能是 {0,1,2}（4 是 __main__ 包装层
    # 的退出码，fuzzer 直接调 run()，两层不得混淆——不变式审计 #3-3）
    check(crashed is None and rc in (0, 1, 2),
          f"#{n}[{seed}] I3 逃逸：crashed={crashed} rc={rc} "
          f"screens={screens!r} pref={pref!r} ledger={ledger_raw!r:.80}")
    if crashed is not None or rc not in (0, 1, 2):
        return

    # I1：不可信输入 → rc=2 且零派发
    if not trusted_screens or not trusted_pref:
        check(calls == [] and rc == 2,
              f"#{n}[{seed}] I1 不可信输入驱动了 GUI：calls={calls} rc={rc} "
              f"screens={screens!r} pref={pref!r}")
        return

    # I2：方向绝不发明
    for desired in calls:
        if screens > 1:
            check(desired is False,
                  f"#{n}[{seed}] I2 多屏派了非 off：desired={desired}")
        else:
            check(trusted_target and desired is ledger_obj["pref_before_close"],
                  f"#{n}[{seed}] I2 单屏派发了账本之外的方向：desired={desired} "
                  f"ledger={ledger_raw!r:.80}")

    # I4：guard **本轮新写**的标记必须是完整、严格类型的五元组——
    # 预置账本（fixture）里的旧标记原样保留时不受此约束（它不是 guard 写的）。
    # 严格恒等：True/False 不得接受 1/0/1.0（不变式审计 #2-3）
    pre_marker = isinstance(ledger_obj, dict) \
        and ledger_obj.get("closed_by_script") is True
    if saved.get("closed_by_script") is True and not pre_marker:
        check(saved.get("pref_before_close") is (True if pref else False)
              and saved.get("screens_at_close") == screens
              and isinstance(saved.get("closed_at"), str)
              and isinstance(saved.get("closed_by_seq"), int)
              and not isinstance(saved.get("closed_by_seq"), bool)
              and saved.get("closed_by_seq") > 0,
              f"#{n}[{seed}] I4 标记不完整/弱类型：{saved!r:.120}")


def daemon_round(rng: random.Random, sd, n: int, seed: int):
    """跑一轮随机化的 daemon process_once，断言 I5。"""
    kind = rng.choice(["normal", "giant_seq", "bool_seq", "float_seq", "neg_seq",
                       "non_dict", "unknown_action", "stale_vs_watermark",
                       "malformed", "poisoned_wm", "raise_do_set"])
    seq = int(datetime.now(timezone.utc).timestamp() * 1e9) + n
    req = {"action": "off", "seq": seq}
    if kind == "giant_seq":
        req["seq"] = 10 ** rng.randrange(51, 80)
    elif kind == "bool_seq":
        req["seq"] = True
    elif kind == "float_seq":
        req["seq"] = float(seq)
    elif kind == "neg_seq":
        req["seq"] = -rng.randrange(1, 1000)
    elif kind == "stale_vs_watermark":
        req["seq"] = 5
    elif kind == "unknown_action":
        req["action"] = "foo"
    # 毒化水位 + 合法请求：load 侧必须把非法水位当无水位自愈，
    # 否则一次毒化永久饿死一切（红队 #2 的另一半）。
    # raise_do_set 场景必须保证请求"新鲜"（last_seq 不得 ≥ seq）：
    # 否则请求被判 stale 跳过、混入与异常注入无关的路径（fuzz 实测踩过）
    if kind == "poisoned_wm":
        last_seq = 10**60
    elif kind == "raise_do_set":
        last_seq = rng.choice([None, 10, seq - 1])
    else:
        last_seq = rng.choice([None, 10, seq - 1, seq, seq + 1])
    raw = ("[1,2]" if kind == "non_dict"
           else '{"action": "of' if kind == "malformed" else json.dumps(req))

    with tempfile.TemporaryDirectory() as td:
        sd.DIR = td
        sd.REQ_PATH = str(pathlib.Path(td) / "request.json")
        sd.INFLIGHT_PATH = str(pathlib.Path(td) / "request.inflight")
        sd.RESULT_PATH = str(pathlib.Path(td) / "result.json")
        sd.SEQ_PATH = str(pathlib.Path(td) / "last-seq.json")
        sd.LOG_PATH = str(pathlib.Path(td) / "fuzz-daemon.log")
        pathlib.Path(sd.REQ_PATH).write_text(raw, encoding="utf-8")
        if last_seq is not None:
            pathlib.Path(sd.SEQ_PATH).write_text(json.dumps({"seq": last_seq}),
                                                 encoding="utf-8")
        executed: list = []

        def fake_do_set(desired):
            executed.append(desired)
            if kind == "raise_do_set":
                raise OSError("boom")
            return (True, "fuzz")

        sd.do_set_guarded = fake_do_set
        crashed = None
        try:
            sd.process_once()
        except Exception as e:      # noqa: BLE001
            crashed = repr(e)
        wm = json.loads(pathlib.Path(sd.SEQ_PATH).read_text(encoding="utf-8")).get("seq") \
            if pathlib.Path(sd.SEQ_PATH).is_file() else None
        infl_left = pathlib.Path(sd.INFLIGHT_PATH).exists()
        result = json.loads(pathlib.Path(sd.RESULT_PATH).read_text(encoding="utf-8")) \
            if pathlib.Path(sd.RESULT_PATH).is_file() else None
        listing = sorted(os.listdir(td))

    # I5a：任何路径（含执行端抛异常）下 inflight 必须被清理（防幽灵待办）
    check(crashed is None and not infl_left,
          f"#{n}[{seed}] I5 崩溃/残留：crashed={crashed} inflight={infl_left} kind={kind}")
    if crashed is not None:
        return
    seq_v = req.get("seq")
    # 新鲜度必须对"load 侧校验后的有效水位"算（毒化水位自愈为 None 后，
    # 合法请求就该执行——用原始 fixture 水位判会把自愈执行误判成过期）
    eff_last = last_seq if (isinstance(last_seq, int)
                            and not isinstance(last_seq, bool)
                            and 0 < last_seq <= sd.SEQ_MAX) else None
    valid_fresh = (isinstance(seq_v, int) and not isinstance(seq_v, bool)
                   and 0 < seq_v <= sd.SEQ_MAX
                   and (eff_last is None or seq_v > eff_last))
    # I5b：被执行 ⟹ 合法 seq 且新于水位；且方向与请求一致（off→False，
    # 不变式审计 #2-2：I5 原先只看列表非空，daemon 反向执行也能蒙混）
    if executed:
        check(valid_fresh,
              f"#{n}[{seed}] I5 非法/过期 seq 被执行：{seq_v!r} last={last_seq} kind={kind}")
        expected_desired = req.get("restore_to") is True \
            if req.get("action") == "restore" else False
        check(executed == [expected_desired],
              f"#{n}[{seed}] I5 执行方向与请求不符：executed={executed} req={req}")
        check(wm == seq_v,
              f"#{n}[{seed}] I5 执行后水位必须等于该 seq（wm={wm} seq={seq_v}）")
    # I5d：毒化水位 + 合法请求 ⟹ 必须自愈执行（不能永久饿死）
    if kind == "poisoned_wm":
        check(executed == [False] and wm == seq_v,
              f"#{n}[{seed}] I5d 毒化水位未自愈：executed={executed} wm={wm}")
    # I5e：执行端抛异常 ⟹ 如实回报失败结果、inflight 清理；水位推进到本
    # seq 是刻意设计（防同 seq 重放），两种状态都合法（不变式审计后修正：
    # 原断言"不得落水位"与实现语义写反了）
    if kind == "raise_do_set":
        check(result is not None and result.get("ok") is False
              and wm in (None, seq_v),
              f"#{n}[{seed}] I5e 异常注入后必须上报失败且 inflight 清理"
              f"（result={result!r:.80} wm={wm} listing={listing}）")


def main() -> int:
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 20261017
    n_guard = int(sys.argv[2]) if len(sys.argv) > 2 else 4000
    n_daemon = int(sys.argv[3]) if len(sys.argv) > 3 else 1200
    print(f"fuzz seed={seed} guard={n_guard} daemon={n_daemon}")
    rng = random.Random(seed)

    import importlib
    mod = importlib.import_module("stage_manager_guard")
    # 必须在第一轮替换发生**之前**抓住真身：use_real 轮要走的真实派发路径
    real_set = mod.set_stage_manager
    for i in range(n_guard):
        guard_round(rng, mod, i + 1, seed, real_set)
        if not all(ok for ok, _ in results):
            break

    sd = importlib.import_module("sm_daemon")
    for i in range(n_daemon):
        daemon_round(rng, sd, i + 1, seed)
        if not all(ok for ok, _ in results):
            break

    failed = [m for ok, m in results if not ok]
    for ok, m in results:
        print(("PASS  " if ok else "FAIL  ") + m)
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("\nFAILED:")
        for m in failed[:10]:
            print("  - " + m)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
