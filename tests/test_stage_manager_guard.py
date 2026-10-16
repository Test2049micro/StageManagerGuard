#!/usr/bin/env python3
"""test_stage_manager_guard.py —— 台前调度守卫的判据回归测试

守护的不变式（每条都必须可证伪，防真空断言）：
 1. 判据方向：多屏(>1)必须关，单屏(==1)必须恢复 —— 方向写反等于功能完全失效。
 2. 归属纪律：只有 closed_by_script=True 才恢复；用户自己关的不得被"恢复"成开启。
 3. 恢复值必须是关之前记住的值，不得硬编码成 True（否则会把用户不想要的台前调度打开）。
 4. 计数读不到时必须中止退出码 2，不得当成单屏（否则会误触发"恢复"）。
 5. 写 pref 不生效这条实测结论必须留在源码注释里（防后人"顺手优化"成 defaults write）。
 6. 面板名 / 开关名 / 偏好域必须与源码常量一致（改名后不换实现 = 静默失效）。
 7. 读回校验：成功判据必须同时包含 UI 读回与 pref 读回，不许只看退出码。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src" / "stage_manager_guard.py"

results: list[tuple[bool, str]] = []


def check(cond: bool, msg: str) -> None:
    results.append((bool(cond), msg))


def main() -> int:
    if not SRC.exists():
        print(f"FAIL: {SRC} missing")
        return 1
    src = SRC.read_text(encoding="utf-8")

    # 1) 判据方向：用 import 直接测 decide()，而非 grep 字符串（字符串会被重构骗过）
    sys.path.insert(0, str(HERE.parent / "src"))
    import importlib
    mod = importlib.import_module("stage_manager_guard")

    check(mod.decide(2) == "off", "decide(2) 必须是 off（多屏关）")
    check(mod.decide(3) == "off", "decide(3) 必须是 off（多屏关）")
    check(mod.decide(1) == "restore", "decide(1) 必须是 restore（单屏恢复）")
    # 边界：0 屏是异常态，不得判成 restore 之外的动作
    check(mod.decide(0) == "restore", "decide(0) 归 restore（由计数读不到的中止逻辑兜底）")

    # 2)+3) 归属与恢复值：用临时状态文件驱动 run()，验证只恢复"我关的"且原值奉还
    import json
    import tempfile

    def run_with_state(state_obj: dict, screens: int, pref: bool | None):
        with tempfile.TemporaryDirectory() as td:
            mod.STATE_PATH = str(pathlib.Path(td) / "state.json")
            if state_obj is not None:
                pathlib.Path(mod.STATE_PATH).write_text(
                    json.dumps(state_obj), encoding="utf-8")
            # 打桩：不让测试真的去动 GUI / 真显示器
            mod.screen_count = lambda: screens
            mod.pref_enabled = lambda: pref
            calls: list[bool] = []
            mod.set_stage_manager = lambda desired, dry_run=False: (
                calls.append(desired) or (True, "stub"))
            rc = mod.run(dry_run=False)
            saved = json.loads(pathlib.Path(mod.STATE_PATH).read_text(encoding="utf-8")) \
                if pathlib.Path(mod.STATE_PATH).exists() else {}
            return rc, calls, saved

    # 2a) 用户自己关的（closed_by_script 缺失）→ 单屏时不得调用 set_stage_manager
    rc, calls, saved = run_with_state({}, screens=1, pref=False)
    check(rc == 0, "单屏+无脚本关闭记录应正常返回 0")
    check(calls == [], f"单屏+用户自关时不得触发恢复，实际调用={calls}")

    # 2b) 脚本关的 + 关之前是 True → 恢复成 True
    rc, calls, saved = run_with_state(
        {"closed_by_script": True, "pref_before_close": True}, screens=1, pref=False)
    check(rc == 0, "脚本关的+关前为True应返回 0")
    check(calls == [True], f"应恢复成 True，实际调用={calls}")
    check("closed_by_script" not in saved, "恢复成功后必须清除 closed_by_script")

    # 2c) 脚本关的 + 关之前是 False → 恢复成 False（不是无脑打开）
    rc, calls, saved = run_with_state(
        {"closed_by_script": True, "pref_before_close": False}, screens=1, pref=True)
    check(calls == [False], f"关前为False必须恢复成False，实际调用={calls}")

    # 2d) 多屏 + 当前开着 → 必须关，并记下关之前的值为恢复依据
    rc, calls, saved = run_with_state({}, screens=3, pref=True)
    check(rc == 0, "多屏+开着应返回 0")
    check(calls == [False], f"多屏必须关，实际调用={calls}")
    check(saved.get("closed_by_script") is True, "多屏关闭后必须置 closed_by_script")
    check(saved.get("pref_before_close") is True, "必须记住关之前的真实值")

    # 2e) 多屏 + 已经是关的（用户自关）→ 不得再动 GUI
    rc, calls, saved = run_with_state({}, screens=3, pref=False)
    check(calls == [], f"多屏但已是关闭态时不得再点 GUI，实际调用={calls}")

    # 2f) 关键回归：多屏 + 已关 + 已有 closed_by_script 标记 → 必须保留该标记。
    #     它将来回单屏时是"恢复成什么"的唯一依据，在"已是关闭态"分支被清掉
    #     会让恢复无据可依（2026-10-16 实测踩过）。
    rc, calls, saved = run_with_state(
        {"closed_by_script": True, "pref_before_close": True}, screens=3, pref=False)
    check(calls == [], f"多屏且已关时不得再点 GUI，实际调用={calls}")
    check(saved.get("closed_by_script") is True,
          "多屏且已关时必须保留 closed_by_script（供将来单屏恢复）")
    check(saved.get("pref_before_close") is True,
          "多屏且已关时必须保留 pref_before_close（恢复值的唯一依据）")

    # 4) 计数读不到必须中止（退出码 2），不得当单屏
    with tempfile.TemporaryDirectory() as td:
        mod.STATE_PATH = str(pathlib.Path(td) / "state.json")
        mod.screen_count = lambda: None          # 探测失败
        mod.pref_enabled = lambda: False
        mod.set_stage_manager = lambda d, dry_run=False: (_ for _ in ()).throw(
            AssertionError("探测失败时绝不允许动 GUI"))
        rc2 = mod.run(dry_run=False)
    check(rc2 == 2, f"计数读不到必须返回 2，实际={rc2}")

    # 4b) 台前调度当前状态读不到也必须中止，且绝不允许动 GUI
    with tempfile.TemporaryDirectory() as td:
        mod.STATE_PATH = str(pathlib.Path(td) / "state.json")
        mod.screen_count = lambda: 3
        mod.pref_enabled = lambda: None
        mod.set_stage_manager = lambda d, dry_run=False: (_ for _ in ()).throw(
            AssertionError("状态未知时绝不允许动 GUI"))
        rc3 = mod.run(dry_run=False)
    check(rc3 == 2, f"当前状态读不到必须返回 2，实际={rc3}")

    # 4c) 打桩 set_stage_manager 返回失败时必须返回非 0（不许谎报成功）
    with tempfile.TemporaryDirectory() as td:
        mod.STATE_PATH = str(pathlib.Path(td) / "state.json")
        mod.screen_count = lambda: 3
        mod.pref_enabled = lambda: True
        mod.set_stage_manager = lambda d, dry_run=False: (False, "stub failed")
        rc4 = mod.run(dry_run=False)
        saved4 = json.loads(pathlib.Path(mod.STATE_PATH).read_text(encoding="utf-8")) \
            if pathlib.Path(mod.STATE_PATH).exists() else {}
    check(rc4 == 1, f"关闭失败必须返回 1，实际={rc4}")
    check(not saved4.get("closed_by_script"),
          "关闭失败时不得置 closed_by_script（否则单屏时会去恢复一个没关成的状态）")

    # 5) 实测结论必须留在注释里（防后人改回 defaults write，或改成自己点 GUI）
    check("defaults write" in src and "不生效" in src,
          "必须保留「写 pref 不生效」的实测结论注释")
    check("GloballyEnabled" in src, "必须保留 GloballyEnabled 偏好键名")
    check("AppleEvent timed out" in src,
          "必须保留「launchd 拿不到 AX 权限」的实测结论（否则会有人试图自己点开关）")
    check("ProcessType" in src and "Background" in src,
          "必须保留 ProcessType=Background 会挂死的告警")

    # 6) 架构纪律：guard 不得自己点 GUI，必须把执行权交给 sm_daemon
    check(hasattr(mod, "write_request") and hasattr(mod, "wait_result"),
          "guard 必须具备下发请求与等待回报的能力")
    check("osascript" not in src.split("# ---------- 请求下发")[-1],
          "guard 的下发段不得再出现 osascript（点击权已移交 sm_daemon）")
    check("request.json" in src and "result.json" in src,
          "必须用 request/result 文件与守护进程解耦")
    # 请求必须是原子写入（守护进程不能读到半截 JSON）
    check(".tmp" in src and "os.replace" in src,
          "请求文件必须原子写入（先写 .tmp 再 replace）")

    # 6b) daemon 端：真正的点击逻辑必须在那里，且有读回校验
    daemon = HERE.parent / "src" / "sm_daemon.py"
    check(daemon.exists(), "stage-manager/sm_daemon.py 必须存在（执行端）")
    if daemon.exists():
        dsrc = daemon.read_text(encoding="utf-8")
        check("Stage Manager" in dsrc, "daemon 必须保留开关无障碍名称（英文）")
        # 本地化：无障碍名与窗口标题随系统语言变化，单字符串精确匹配会在
        # 非英文系统静默失效。必须用候选集合匹配，且中文值要有官方来源
        # （loctable 一手证据，见源码注释）。
        check("TOGGLE_CANDIDATES" in dsrc and "WINDOW_CANDIDATES" in dsrc,
              "daemon 必须用候选名集合匹配（防非英文系统静默失效）")
        for zh in ("台前调度", "幕前調度", "幕前管理"):
            check(zh in dsrc, f"开关候选必须含中文译名 {zh}")
        check("桌面与程序坞" in dsrc, "窗口候选必须含 zh_CN 面板名")
        check('contains (name of u as string)' in dsrc,
              "AppleScript 必须用 contains 做候选包含判断，而非 is 精确等于")
        # 不得残留按单一写死名取窗口的写法（那会在非英文系统找不到窗口）
        check('entire contents of window "{win}"' not in dsrc,
              "不得再用写死窗口名取 UI 树（须遍历 windows 匹配候选）")
        check("repeat with w in windows" in dsrc,
              "必须遍历 windows 按候选名匹配窗口")
        check("click u" in dsrc, "daemon 必须真的执行点击（执行端不能退化成只读）")
        check("pref_enabled" in dsrc, "daemon 必须读回 pref 做双重确认")
        check("result.json" in dsrc, "daemon 必须写回 result.json 供 guard 判成败")
        check("--once" in dsrc, "daemon 必须支持 --once 便于调试与回归")
        # open_pane 不能只查一次窗口名：实测 open 成功后 AX 树要再过几秒才可取
        check("PANE_READY_TRIES" in dsrc, "daemon 必须带面板就绪重试（单次检查会误判打不开）")
        check("flock" in dsrc, "daemon 必须用 flock 保证单实例（两个实例会互相抵消）")

    # 7) 读回校验：成功判据必须同时看 UI 与 pref（现在这条判据在 daemon 里）
    check("newval == want and pf_after == desired" in dsrc,
          "读回校验必须同时比对 UI 值与 pref 值")
    # 7a) ALREADY 分支也必须复核 pref：UI 已是目标值 ≠ 系统已采纳
    check("ui already" in dsrc and "not yet adopted" in dsrc,
          "ALREADY 分支必须复核 pref，不能只看 UI 值就宣称成功")
    # 7b) AppleScript 变量名不得用保留字 `after`：会报
    #     "Expected expression but found 'to'"，且报错行号是假的（恒为 541）
    check("set after to" not in dsrc,
          "AppleScript 变量名不得用保留字 after（报错行号会骗人）")

    # 7c) 不能每次处理完都关 Settings 窗口：反复开闭会让下一次重新冷启动，
    #     实测把单轮从 ~5 秒拖到 45 秒以上，还放大 AX 竞态导致"打不开"
    check("_maybe_idle_close" in dsrc and "IDLE_CLOSE_AFTER" in dsrc,
          "daemon 必须改为空闲关窗，不得每次处理完都 close")

    # 8b) seq 必须全局唯一且单调递增（早期用两个文件各存计数器，会各走各的，
    #     出现"新请求 seq 等于已处理 seq"被误判过期，2026-10-16 踩过）
    s1 = mod._next_seq()
    s2 = mod._next_seq()
    check(isinstance(s1, int) and isinstance(s2, int) and s2 > s1,
          f"seq 必须严格递增，实际 {s1} -> {s2}")
    check(not hasattr(mod, "last_request_seq") or "last_request_seq" not in mod.load_state(),
          "不得再用 state 文件持久化 seq 计数器（双计数器会打架）")

    # 8c) daemon 的过期请求判据必须是"严格大于"：seq 相等时应处理而非跳过
    dsrc2 = daemon.read_text(encoding="utf-8") if daemon.exists() else ""
    check("seq <= last_seq" in dsrc2,
          "daemon 必须丢弃 seq 不大于已处理值 的请求（<= 而非 <）")

    # 8d) wait_result 必须是真行为验证（不是字符串检查）。
    #     历史教训：上一版这里写的是 `check("timeout=0" in src)`，
    #     而 timeout=0 恰恰是让循环体一次都不进的那个值——测试把 bug 当成了守护目标。
    #     现在用真实文件驱动：结果已写回 → 必须收到 ok=True。
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        mod.RESULT_PATH = str(pathlib.Path(td) / "result.json")
        # 场景 A：成功结果已写回（时间戳晚于请求发起）→ 必须收到
        pathlib.Path(mod.RESULT_PATH).write_text(json.dumps(
            {"ok": True, "msg": "set to 0 (ui=0 pref=False)",
             "at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
        ok_a, msg_a = mod.wait_result(time.time() - 5, timeout=0)
        check(ok_a is True, f"结果已写回时 timeout=0 必须收到成功，实际 ok={ok_a} msg={msg_a!r}")
        check("set to 0" in msg_a, "收到的 msg 必须是守护进程回报的原文")

        # 场景 B：结果是上一轮的（早于请求发起）→ 必须不采信
        pathlib.Path(mod.RESULT_PATH).write_text(json.dumps(
            {"ok": True, "msg": "STALE", "at": "2000-01-01T00:00:00+00:00"}), encoding="utf-8")
        ok_b, msg_b = mod.wait_result(time.time(), timeout=0)
        check(ok_b is False and "STALE" not in msg_b,
              f"旧结果不得被当成本轮成败，实际 ok={ok_b} msg={msg_b!r}")

        # 场景 C：还没有结果 → 返回 in-flight，且不得阻塞超过 timeout
        try:
            os.remove(mod.RESULT_PATH)
        except OSError:
            pass
        t0 = time.time()
        ok_c, msg_c = mod.wait_result(time.time(), timeout=0)
        elapsed = time.time() - t0
        check(ok_c is False and "in-flight" in msg_c, "无结果时必须返回 in-flight")
        check(elapsed < 2.0, f"timeout=0 必须立即返回，实际耗时 {elapsed:.2f}s")

        # 场景 D：daemon 回报失败 → 必须如实返回 False（不许吞掉）
        pathlib.Path(mod.RESULT_PATH).write_text(json.dumps(
            {"ok": False, "msg": "cannot open settings pane",
             "at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
        ok_d, msg_d = mod.wait_result(time.time() - 5, timeout=0)
        check(ok_d is False and "cannot open" in msg_d,
              f"daemon 报失败必须如实上抛，实际 ok={ok_d} msg={msg_d!r}")

    # 8d2) 非阻塞不得退化成"不看"：这是上面 A 场景存在的原因
    check("if timeout is None" in src and "while True" in src,
          "wait_result 必须先看结果再判断是否继续等（早期 while time<deadline 会一次都不看）")

    # 8i) 下发后必须阻塞收结果：launchd 对同一 StartInterval job 天然串行，
    #     "下发就走、下轮再收"曾导致 closed_by_script 永远无人收集
    #     （下轮 pref 已翻转走 already-off 分支跳过收集）→ 单屏恢复静默失效
    check("timeout=REQUEST_TIMEOUT" in src,
          "set_stage_manager 必须阻塞等回结果（timeout=REQUEST_TIMEOUT），不得下发就走")
    # 8j) 单轮预算必须大于请求超时，否则 alarm 砍断正常的结果等待
    check(mod.ROUND_BUDGET > mod.REQUEST_TIMEOUT,
          f"ROUND_BUDGET({mod.ROUND_BUDGET}) 必须 > REQUEST_TIMEOUT({mod.REQUEST_TIMEOUT})")

    # 8k) 状态文件损坏必须留档 + 不静默吞掉（真行为验证，非字符串检查）。
    #     状态文件是"该恢复成什么"的唯一依据；静默当空会导致：
    #     单屏时漏恢复、多屏时覆盖记下的 pref_before_close。
    with tempfile.TemporaryDirectory() as td:
        orig_state = mod.STATE_PATH
        mod.STATE_PATH = str(pathlib.Path(td) / "state.json")
        # 正常读取
        pathlib.Path(mod.STATE_PATH).write_text('{"closed_by_script": true}', encoding="utf-8")
        check(mod.load_state().get("closed_by_script") is True, "正常状态文件必须能读出")
        # 损坏：应留档 .corrupt-*，并返回空 dict
        pathlib.Path(mod.STATE_PATH).write_text('{"closed_by_scri', encoding="utf-8")
        got = mod.load_state()
        check(got == {}, f"损坏状态必须返回空 dict，实际 {got!r}")
        kept = list(pathlib.Path(td).glob("state.json.corrupt-*"))
        check(len(kept) == 1, f"损坏状态必须改名留档（找到 {len(kept)} 个）")
        check("closed_by_scri" in kept[0].read_text(encoding="utf-8"),
              "留档文件必须保留原始损坏内容（供事后追因）")
        # 文件不存在：不得留档（不是损坏）
        mod.STATE_PATH = str(pathlib.Path(td) / "nope.json")
        check(mod.load_state() == {}, "文件不存在时返回空 dict")
        check(not list(pathlib.Path(td).glob("nope.json.corrupt-*")),
              "文件不存在时不得产生 .corrupt 留档")
        mod.STATE_PATH = orig_state

    # 8m) 所有子进程调用必须用 subprocess.run，不得用 Popen。
    #     实证（2026-10-16 独立实验）：alarm 打断 subprocess.run 时，其 except 分支
    #     会先 kill 子进程再抛异常，不留孤儿；而裸 Popen + wait() 会留孤儿
    #     （评审据此认为本代码有 M5 孤儿问题，经实测不成立）。
    #     这条断言把"用 run 不用 Popen"固化为不可回退的约束。
    for f, name in ((src, "guard"), (dsrc, "daemon")):
        check("subprocess.Popen" not in f,
              f"{name} 不得用 subprocess.Popen（会留孤儿；须用 subprocess.run）")
        check("subprocess.run" in f, f"{name} 必须用 subprocess.run 起子进程")

    # 8n) 首装友好性：未 build 时必须给可诊断提示（不是裸 Errno 2），
    #     且 --probe 探测失败要返回非零（否则 CI/脚本无法判定）
    check("run ./build.sh first" in src,
          "探测工具缺失时必须提示 build.sh（新用户看不懂裸 Errno 2）")
    check("_WARNED_NO_PROBE" in src, "缺失提示必须去重（两处调用否则刷屏）")
    check("else 3" in src or "return 0 if (n is not None and pf is not None) else 3" in src,
          "--probe 探测失败必须返回非零退出码")

    # 8o) plist 模板：占位符必须被 install.sh 全部替换（隔离渲染验证其可渲染性）
    import subprocess as _sp
    tpl_dir = HERE.parent / "resources"
    for label in ("com.local.stage-manager-guard", "com.local.stage-manager-daemon"):
        tpl = tpl_dir / f"{label}.plist.template"
        check(tpl.exists(), f"{label}.plist.template 必须存在")
        if tpl.exists():
            with tempfile.TemporaryDirectory() as td:
                out = pathlib.Path(td) / "r.plist"
                txt = tpl.read_text(encoding="utf-8")
                for ph, val in (("@BINDIR@", "/opt/x"), ("@PYTHON@", "/usr/bin/python3"),
                                ("@SMG_HOME@", "/tmp/smg"), ("@USER@", "tester")):
                    txt = txt.replace(ph, val)
                out.write_text(txt, encoding="utf-8")
                r = _sp.run(["plutil", "-lint", str(out)], capture_output=True, text=True)
                check(r.returncode == 0, f"{label} 渲染后必须 plutil lint 通过")
                check("@BINDIR@" not in txt and "@SMG_HOME@" not in txt,
                      f"{label} 不得残留占位符")

    # 8l) daemon 的 ALREADY 分支也必须更新空闲计时：否则反复处理"已是目标态"
    #     的请求时计时停在旧值，窗口被误关、下次又要 18–28 秒冷启动
    already_seg = dsrc.split('if state == "ALREADY"')[1].split("# CLICKED")[0]
    check("_LAST_ACTIVITY[0] = time.time()" in already_seg,
          "ALREADY 分支必须更新 _LAST_ACTIVITY（防空闲关窗误判）")

    # 8h) inflight 必须有租约回收：daemon 崩溃/无权限时 inflight 会永久驻留，
    #     导致 guard 每轮都走"已有待办"分支，既不重试也不报错，功能静默停滞
    check("_reclaim_stale_inflight" in src and "REQUEST_STALE_AFTER" in src,
          "guard 必须回收超时 inflight 请求，否则 daemon 挂掉后整条链路静默停摆")
    # 8f) 去重必须同时看 request.json 与 request.inflight：守护进程会把请求
    #     改名占位后再慢慢点，只看一个文件会在"正在点"的窗口里重复下发、把动作抵消
    check("INFLIGHT_PATH" in src and "for p in (REQ_PATH, INFLIGHT_PATH)" in src,
          "guard 去重必须同时看 request.json 与 request.inflight")
    # 8g) daemon 必须先改名占位再处理：读完→处理数十秒→最后删 会让主循环
    #     每秒重复读到同一请求并重复执行（实测同一 seq 连续处理两次）
    check("os.replace(REQ_PATH, INFLIGHT_PATH)" in dsrc,
          "daemon 必须先把请求改名占位再处理，防止重复执行")
    # 8e) 单轮必须有硬性时限：子进程卡住（subprocess wait 永久阻塞）会让轮询停摆数小时
    check("ROUND_BUDGET" in src and "SIGALRM" in src,
          "guard 必须有单轮硬性时限（alarm 兜底），否则子进程卡死会停摆数小时")
    # in-flight 不得被当成失败：失败会误报、且不敢写 closed_by_script
    check(src.count('"in-flight" not in msg') == 2,
          "off 与 restore 两条分支都必须把 in-flight 与真失败区分开")
    # 请求堆积必须被拦住
    check("_pending_request" in src, "guard 必须检测未消费请求，避免请求无限堆积")

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
