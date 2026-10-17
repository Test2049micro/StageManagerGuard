#!/usr/bin/env python3
"""stage_manager_guard.py —— 台前调度按显示器数量自动开关（多屏关、单屏恢复）

规则：
  在役显示器 > 1  → 关掉台前调度（Stage Manager）
  在役显示器 == 1 → 恢复成"被本工具关掉之前"的状态

关键实测结论（macOS 27.0.1，勿凭经验推翻）：
  1) 直接 `defaults write com.apple.windowmanager GloballyEnabled 1` **不生效**——
     偏好值会落盘、WindowManager 进程也不重启，但系统行为（窗口是否被收进 strip）
     完全不改变；`killall -HUP WindowManager` 与真 `killall WindowManager` 同样无效
     （后者已用 AX checkbox 读回坐实：pref=1 而 checkbox=0）。
     唯一可靠通道是点击「系统设置 › 桌面与程序坞」里名为 "Stage Manager" 的 checkbox：
     点击后 UI 值、GloballyEnabled、以及真实窗口行为三者同步改变。

  2) **launchd 任务点不了这个 checkbox**：它能启动 System Settings，
     但 System Events 对它一律 `AppleEvent timed out (-1712)`，`launchctl asuser` 同样无效
     （AX 授权绑定在用户登录会话上）。因此本文件只负责"检测与决策"，
     真正的点击交给用户会话里的常驻进程 `sm_daemon.py`。

  3) ⚠️ 不要把 launchd plist 写成 `ProcessType = Background`：实测该值会让本任务
     挂死在 xpcproxy 阶段、日志全空、进程永不退出（同配置去掉即恢复）。

架构（两进程，文件解耦）：
  stage_manager_guard.py（launchd 跑）—— 探测显示器数量，按规则写 request.json
  sm_daemon.py（用户会话常驻）—— 轮询 request.json 并真正点击开关，写 result.json 回报成败

设计纪律：
- 不报错≠成功：任何"设置"都以读回为准，不看退出码；读不回即报失败，不宣称成功。
- 归属纪律：状态外置落盘，区分"本工具关的"与"用户自己关的"，
  只有"本工具关的"才会被恢复，绝不把用户的主动选择当成待恢复项。
- 判据配测试：阈值与常量集中在本文件顶部，由 tests/test_stage_manager_guard.py 守护。
- 环境快照：面板名/进程名/偏好域都是运行时快照，脚本以读回真值为准。

用法：
  python3 stage_manager_guard.py            # 执行一次（供 launchd 周期/热插拔触发）
  python3 stage_manager_guard.py --dry-run  # 只报告目标状态与实际状态，不下请求
  python3 stage_manager_guard.py --probe    # 只打印当前显示器数与台前调度状态
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
# 状态与日志目录：可用 SMG_HOME 覆盖，默认落在 macOS 标准位置
SMG_HOME = os.environ.get(
    "SMG_HOME", os.path.expanduser("~/Library/Application Support/StageManagerGuard"))
PROBE = os.environ.get(
    "SMG_PROBE", os.path.join(HERE, "..", "bin", "DisplayProbe"))
STATE_PATH = os.path.join(SMG_HOME, "state.json")
LOG_PATH = os.path.join(SMG_HOME, "guard.log")

# ---- 常量（由 tests/test_stage_manager_guard.py 守护，勿随手改）----
PREF_DOMAIN = "com.apple.windowmanager"
PREF_KEY = "GloballyEnabled"
PROBE_TIMEOUT = 15                     # NSScreen 探测实测 ~0.1s；留余量，且必须 < ROUND_BUDGET 才不吞掉重试
PROBE_ATTEMPTS = 2                     # 探测重试次数（单次超时是瞬态）
PROBE_RETRY_SLEEP = 1.0
REQUEST_TIMEOUT = 90                   # 等 sm_daemon 点完开关的上限（含其内部重试）
REQUEST_STALE_AFTER = 180              # inflight 请求超过此时长未完成即回收重发（防静默停滞）

# 单轮总时限（秒）。launchd 每 3 秒唤起一次，任何子进程卡住（实测出现过
# subprocess.run 的 wait 永久阻塞、子进程已消失但父进程等不到——__semwait_signal）
# 都会让轮询停摆数小时。用 alarm 强制收尾，宁可本轮不做事也不能卡死。
_WARNED_NO_PROBE = False   # 未 build 的提示只打一次
# 必须 > 探测最坏耗时 + REQUEST_TIMEOUT：两个探测各最坏 2×15+1=31s（共 62s），
# 结果等待再吃满 90s = 152s；预算若只有 150s，alarm 会在结果等待中途砍轮，
# 把"标记已落盘、结果还没回"的轮次整个丢掉（评审席 P1-1 的算术）。
ROUND_BUDGET = 200


class _RoundTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise _RoundTimeout()


signal.signal(signal.SIGALRM, _alarm_handler)


# ---------- 基础工具 ----------

LOG_MAX_BYTES = 2_000_000   # 代码自持日志的轮转阈值（macOS 无 logrotate）


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    # flush=True：非 TTY 下 print 默认全缓冲，launchd 的 stdout 重定向会丢日志
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        # 轮转：超限滚到 .1（覆盖旧的），封顶约 2×LOG_MAX_BYTES。
        # 只管代码自己 open 的这份；launchd 重定向的 guard.launchd.log 由 launchd 持有
        # fd，rename 后它仍写旧 inode，无法在此轮转（见 README 排障节）。
        try:
            if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
                os.replace(LOG_PATH, LOG_PATH + ".1")
        except OSError:
            pass
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run_probe(sub: str) -> dict:
    """跑 Swift 探测工具，返回 dict。失败返回 {}（调用方负责判空）。

    NSScreen 首次访问要连 WindowServer，在 launchd 环境下偶发极慢（实测出现过 >20s），
    故超时放宽并重试一次：单次超时是瞬态，不能当成"读不到"而中止整轮。
    """
    # 先给出可诊断的提示：未 build 时裸 Errno 2 对新用户毫无指向性。
    # 只警告一次（screen_count 与 pref_enabled 各会调一次 run_probe，否则重复刷屏）。
    global _WARNED_NO_PROBE
    if not os.path.exists(PROBE):
        if not _WARNED_NO_PROBE:
            _WARNED_NO_PROBE = True
            log(f"probe binary missing at {PROBE} — run ./build.sh first "
                f"(swiftc is bundled with macOS Command Line Tools)")
        return {}
    last_err = ""
    for attempt in range(1, PROBE_ATTEMPTS + 1):
        try:
            r = subprocess.run([PROBE, sub], capture_output=True, text=True,
                               timeout=PROBE_TIMEOUT)
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
            last_err = f"rc={r.returncode} stderr={r.stderr.strip()[:120]}"
        except Exception as e:
            last_err = str(e)
        if attempt < PROBE_ATTEMPTS:
            log(f"probe {sub} attempt {attempt} failed ({last_err}); retrying")
            time.sleep(PROBE_RETRY_SLEEP)
    log(f"probe {sub} failed after {PROBE_ATTEMPTS} attempts: {last_err}")
    return {}


def screen_count() -> int | None:
    """在役显示器数（NSScreen 口径）。读不到返回 None，调用方不得当成 1。"""
    d = run_probe("screens")
    n = d.get("screens")
    return int(n) if isinstance(n, int) else None


def pref_enabled() -> bool | None:
    d = run_probe("pref")
    v = d.get("enabled")
    return bool(v) if isinstance(v, bool) else None


# ---------- 状态文件 ----------

def load_state() -> dict:
    """读状态文件。解析失败时保留现场并返回 {}（不静默吞掉损坏文件）。

    为什么不能静默 return {}：状态文件是"该恢复成什么"的唯一依据。
    若它因掉电/半截写入损坏，静默当空会导致两类错误决策——
    单屏时判成"本工具没关过"而漏恢复；多屏时判成"用户自己关的"而覆盖
    记下的 pref_before_close。把坏文件改名留档，至少事后可追。
    """
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        try:
            kept = f"{STATE_PATH}.corrupt-{int(time.time())}"
            os.replace(STATE_PATH, kept)
            log(f"state file corrupt ({e}); kept original at {kept}, starting fresh")
        except OSError:
            log(f"state file corrupt ({e}); and could not preserve it")
        return {}


def save_state(state: dict) -> bool:
    """落盘状态。返回是否成功——调用方据此决定"账本写不进去就不许派发"。"""
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
        return True
    except OSError as e:
        log(f"save_state failed: {e}")
        return False


# ---------- 请求下发（执行权在 sm_daemon.py，见其文件头说明） ----------

REQ_DIR = SMG_HOME
REQ_PATH = os.path.join(REQ_DIR, "request.json")
INFLIGHT_PATH = os.path.join(REQ_DIR, "request.inflight")
RESULT_PATH = os.path.join(REQ_DIR, "result.json")
_SEQ_COUNTER: int | None = None   # 进程内 seq 兜底（time_ns 粒度不足时会重复）
# 最近一次被采信结果对应的请求 seq。跨轮回滚的依据：标记记着自己是由哪条请求
# 记的账（closed_by_seq），结果失败时只有 seq 对得上才回滚——
# 评审席 P1-2：只看"本轮是否写过"盖不住「上轮 in-flight 落账、这轮才收到失败」。
_LAST_RESULT_SEQ: int | None = None


def write_request(action: str, restore_to: bool = False, seq: int | None = None) -> int:
    """写一个待办请求给守护进程。原子写入，避免守护进程读到半截 JSON。

    seq 单调递增：守护进程据此丢弃过期请求，防止"连写两个请求时旧的覆盖新意图"。
    seq 可由调用方传入（guard 先记账后派发时，要用同一个 seq 把标记与请求绑定，
    评审席 P1-2：跨轮回滚要靠"标记记着是哪条请求给的账"）。
    """
    os.makedirs(REQ_DIR, exist_ok=True)
    if seq is None:
        seq = _next_seq()
    payload = {
        "action": action,
        "restore_to": restore_to,
        "at": datetime.now(timezone.utc).isoformat(),
        "seq": seq,
    }
    tmp = REQ_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REQ_PATH)
    return seq


def _next_seq() -> int:
    """取下一个请求序号。

    必须是**全局唯一且严格递增**：守护进程靠它丢弃过期请求。
    两个历史坑（都踩过）：
    1) 早期用两个文件各存一份计数器（state.json 与 daemon 的 last-seq.json），
       两者各走各的，出现"新请求 seq 等于已处理 seq"被误判为过期。
    2) 改用 time_ns() 后，发现连续两次调用可能返回**同一个值**（时钟粒度不足），
       严格递增仍不成立。
    故以进程内计数器兜底，并叠加时间戳保证跨进程重启后依然递增。
    """
    global _SEQ_COUNTER
    base = time.time_ns()
    if _SEQ_COUNTER is not None and base <= _SEQ_COUNTER:
        base = _SEQ_COUNTER + 1
    _SEQ_COUNTER = base
    return base


def _rollback_marker(state: dict) -> None:
    """把一笔关闭账整笔划掉（标记与其四个附属键）。失败/未派发时调用。"""
    state.pop("closed_by_script", None)
    for k in ("closed_at", "screens_at_close", "pref_before_close",
              "closed_by_seq"):
        state.pop(k, None)
    save_state(state)


def wait_result(start_ts: float, timeout: float | None = 0,
                expected_seq: int | None = None) -> tuple[bool, str]:
    """读 result.json，只认**晚于本次请求**写回的结果，避免把上一轮的旧结果当成本轮成败。

    timeout 语义（曾在此踩坑，勿改）：
      None   = 本轮完全不看，直接返回"进行中"
      0      = 非阻塞：只看一眼，有结果就收，没有就返回"进行中"
      N (>0) = 最多等 N 秒
    历史 bug：早期用 `while time.time() < deadline` + `timeout=0`，
    deadline == start_ts 导致循环体一次都不进，连那"看一眼"都没看——
    结果明明是成功的也被当成 in-flight，`closed_by_script` 永不落盘，
    单屏恢复整条链路静默失效。故这里改成"先检查、再决定是否继续等"的顺序。
    """
    if timeout is None:
        return False, "in-flight (result will be collected on next poll)"

    deadline = start_ts + max(timeout, 0)
    global _LAST_RESULT_SEQ
    while True:
        try:
            with open(RESULT_PATH, encoding="utf-8") as f:
                r = json.load(f)
            at = r.get("at")
            # 严格大于请求发起时刻：同一秒内写回的旧结果不予采信
            if at and _iso_ts(at) > start_ts:
                # 只采信"我们要的那条请求"的结果：A 被超时回收后 B 已派发，
                # A 的结果晚到时若不验 seq，就会把 A 的失败记到 B 头上
                # （评审二轮 P1-2）。不匹配就当没看见，继续等到预算耗尽。
                if expected_seq is not None and r.get("seq") != expected_seq:
                    time.sleep(1.0)
                    if time.time() >= deadline:
                        break
                    continue
                _LAST_RESULT_SEQ = r.get("seq")
                return bool(r.get("ok")), str(r.get("msg", ""))
        except Exception:
            pass
        if time.time() >= deadline:
            break          # 非阻塞（timeout=0）在此直接退出，阻塞模式则等满
        time.sleep(1.0)
    return False, "in-flight (result will be collected on next poll)"


def _iso_ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def _pending_request() -> dict | None:
    """读尚未处理完成的请求（request.json 或已被守护进程取走的 inflight 文件）。

    两个文件都要看：守护进程为防止同一条请求被重复处理，会先把 request.json
    改名成 request.inflight 再慢慢点开关。若只看 request.json，会在这段
    "正在点"的窗口里误判为"没有待办"而重复下发，把动作抵消掉。
    """
    for p in (REQ_PATH, INFLIGHT_PATH):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            continue
    return None


def _reclaim_stale_inflight(pending: dict) -> bool:
    """回收超时未处理的 inflight 请求。返回是否已回收（调用方应重发请求）。

    为什么需要：daemon 若在处理中崩溃 / 缺 AX 权限 / 抛了没被兜住的异常，
    request.inflight 会永久驻留，`_pending_request()` 永远返回它，
    guard 每轮都走"已有待办"分支——既不重试也不报错，功能静默停滞。
    """
    at = pending.get("at")
    age = time.time() - (_iso_ts(at) if at else 0)
    if age < REQUEST_STALE_AFTER:
        return False
    log(f"inflight request stale ({age:.0f}s > {REQUEST_STALE_AFTER}s); reclaiming")
    try:
        os.remove(INFLIGHT_PATH)
    except OSError:
        pass
    try:
        os.remove(REQ_PATH)
    except OSError:
        pass
    return True


def set_stage_manager(desired: bool, dry_run: bool = False,
                      seq: int | None = None) -> tuple[bool, str]:
    """请求把台前调度设为 desired。

    本进程（launchd）没有辅助功能权限，点不了开关，只能下发请求；
    真正的点击由用户会话里的 sm_daemon.py 完成，成败以它写回的 result.json 为准。
    seq 由调用方传入时，与已落盘的标记绑定（见 _run_inner off 分支）。
    """
    if dry_run:
        return True, f"dry-run: would request enabled={desired}"

    # 去重：守护进程处理单个请求要数秒（打开设置面板 + 读回校验），
    # 而本任务每 3 秒轮询一次。若已有未消费请求，就不再叠一个——
    # 否则请求会无限堆积，且旧请求可能覆盖新意图。
    pending = _pending_request()
    if pending is not None:
        if _reclaim_stale_inflight(pending):
            pending = None                     # 已回收，本轮重新下发
        else:
            my_action = "restore" if desired else "off"
            started = _iso_ts(pending.get("at", "")) or time.time()
            # 上一轮（或本进程早前）已下发但未收：等到它的预算耗尽为止，
            # 保证 result 一定被某一轮收走并落盘。
            budget = max(0.0, started + REQUEST_TIMEOUT - time.time())
            if pending.get("action") != my_action:
                # 对向动作尚未收尾：等它落地，但绝不把它的结果记到本轮头上——
                # 否则单屏恢复会把"off 成功"误当"恢复成功"，反之亦然。
                # expected_seq 钉死只采信那条请求的结果（弃之不用，只为等它收尾）。
                wait_result(started, timeout=budget,
                            expected_seq=pending.get("seq"))
                return False, "in-flight (opposite action pending)"
            log(f"request already pending (seq={pending.get('seq')}); waiting for its result")
            return wait_result(started, timeout=budget,
                               expected_seq=pending.get("seq"))

    # 先清掉上一轮结果，避免把旧结果当成本轮成败
    try:
        os.remove(RESULT_PATH)
    except OSError:
        pass
    started = time.time()
    seq = write_request("restore" if desired else "off", restore_to=desired, seq=seq)
    log(f"request written: action={'restore' if desired else 'off'} "
        f"desired={desired} seq={seq}")
    # 阻塞等结果：launchd 的 StartInterval 对同一 job 天然串行（上一轮没退出
    # 不会起新实例），所以阻塞不会丢轮；而"下发就走、下轮再收"曾导致
    # closed_by_script 永远无人收集（下轮 pref 已翻转，走 already off 分支跳过收集），
    # 单屏恢复整条链路静默失效。
    return wait_result(started, timeout=REQUEST_TIMEOUT, expected_seq=seq)


# ---------- 主逻辑 ----------

def decide(n_screens: int) -> str:
    """返回目标动作：'off' / 'restore' / 'none'。"""
    return "off" if n_screens > 1 else "restore"


def run(dry_run: bool = False) -> int:
    # 硬性时限兜底：任何子进程卡住都不能让本轮无限期挂起
    signal.alarm(ROUND_BUDGET)
    try:
        return _run_inner(dry_run=dry_run)
    except _RoundTimeout:
        log(f"ABORT: round exceeded {ROUND_BUDGET}s budget — a subprocess hung; giving up this round")
        return 2
    finally:
        signal.alarm(0)


def _run_inner(dry_run: bool = False) -> int:
    n = screen_count()
    if n is None:
        log("ABORT: cannot determine screen count (probe failed) — refusing to guess")
        return 2
    pf = pref_enabled()
    if pf is None:
        # 台前调度当前状态未知时不动 GUI：可能把它从用户想要的态切走
        log("ABORT: cannot read current Stage Manager state (pref probe failed) — refusing to toggle")
        return 2
    action = decide(n)

    state = load_state()
    log(f"screens={n} pref_enabled={pf} action={action}")

    # 对账 sweep：上一条请求的结果可能在两轮之间才落地，甚至从未被任何
    # wait 窗口采信就躺在这里——fresh-dispatch 的"先清旧结果"会把它删掉，
    # 账本里的标记就永远等不到对账（评审二轮 P1-3）。因此开分支前先对：
    # 失败 → 当场回滚标记；成功 → 账实相符，标记留任。
    if not dry_run and state.get("closed_by_seq") is not None:
        try:
            with open(RESULT_PATH, encoding="utf-8") as f:
                _r = json.load(f)
            if _r.get("seq") == state.get("closed_by_seq") and _r.get("ok") is False:
                log(f"reconcile: result for seq={_r.get('seq')} reports failure; "
                    f"rolling back marker")
                _rollback_marker(state)
                state = load_state()
        except Exception:
            pass

    if action == "off":
        # pf == True 表示台前调度当前「开着」，正是要关的对象；
        # pf is False 表示已经是关闭态，无需再动 GUI。
        if pf is False:
            # 注意：这里**不能**清 closed_by_script。
            # 只要仍是多屏，这个标记就必须留着——它是将来回到单屏时
            # 「该恢复成什么」的唯一依据；在此处清掉会让恢复时无据可依。
            # 该标记只由下面的 restore 分支（单屏）清除。
            if state.get("closed_by_script"):
                log("already off (closed by script earlier); keeping restore marker")
            else:
                log("already off (turned off by user); nothing to do")
            return 0
        # 对向（restore）动作还没收尾时，连账本都不动（评审二轮 P1-5）：
        # 此时派发也会被对向 pending 顶回来，先落一笔"没发生的关闭"只会
        # 留下无人对账的假标记。延后整轮决策，下一轮按真实状态重来。
        _peek = _pending_request()
        if _peek is not None and _peek.get("action") != "off":
            log("opposite (restore) request pending; deferring off this round")
            return 0
        # 下发即记账：标记（连同本周期请求的 seq）在请求发出**之前**落盘。
        # 为什么必须写在派发之前而不是"调用返回之后"：等结果的窗口会被 alarm
        # 截断（探测最坏 62s + 等待 90s 曾把预算顶到 152>150），截断路径下
        # "返回之后再写"永远轮不到执行——工具自己关的会被误归属成用户关的
        # （评审席 P1-1）。closed_by_seq 把标记与具体那条请求绑定，结果失败时
        # 按 seq 回滚，跨轮也成立（评审席 P1-2）；seq 对不上的旧标记属于更早
        # 的周期，不误删。已有旧标记时不覆盖：pref_before_close 保留最早值。
        seq = None
        if not dry_run and not state.get("closed_by_script"):
            seq = _next_seq()
            state["closed_by_script"] = True
            state["closed_at"] = datetime.now(timezone.utc).isoformat()
            state["screens_at_close"] = n
            # 记住关之前是什么，恢复时原样奉还
            state["pref_before_close"] = pf
            state["closed_by_seq"] = seq
            if not save_state(state):
                # 账本写不进去就不许派发：恢复依据落不了盘，宁可这轮不动 GUI
                log("cannot persist close marker; refusing to dispatch (fail-safe)")
                return 2
        try:
            ok, msg = set_stage_manager(False, dry_run=dry_run, seq=seq)
        except _RoundTimeout:
            raise    # alarm 砍在结果等待上：请求已出门，标记必须保留（P1-1 语义）
        except Exception:
            # 派发本身失败（write_request 抛错等）：这笔账没发生，立即回滚
            # （评审二轮 P1-1）。进程在"存账"与"派发"两步之间被杀的窗口
            # （ms 级）仍存在，留作已知残余。
            if seq is not None:
                log("dispatch failed before request left; rolling back marker")
                _rollback_marker(state)
            return 2
        log(f"turn OFF: {msg}")
        if not ok:
            # in-flight：请求已下发、守护进程正在点，尚未有结果。不是失败，
            # 标记保留（这正是"下发即记账"的意义）。
            if "in-flight" not in msg:
                # 真失败：只有当失败结果正是"给这个标记记账的那条请求"时才回滚
                # ——没关成的东西不许挂"待恢复"（fail-safe 方向）。比对的是
                # 账本里的 closed_by_seq，不是"本轮是否派发过"：标记常是更早
                # 轮次落的账、结果这轮才收到（评审席 P1-2 的原始场景）。
                if (not dry_run
                        and state.get("closed_by_seq") is not None
                        and state.get("closed_by_seq") == _LAST_RESULT_SEQ):
                    _rollback_marker(state)
                return 1
            return 0
        return 0

    # action == restore：单屏
    if not state.get("closed_by_script"):
        log("single screen and nothing was closed by script; nothing to restore")
        return 0
    prev = state.get("pref_before_close")
    want = bool(prev) if isinstance(prev, bool) else False
    if pf is want:
        pending = _pending_request()
        if pending is not None and not _reclaim_stale_inflight(pending):
            # 还有请求没收尾（典型：插屏瞬间的 off 请求仍在点击队列里）。
            # 此刻清标记，等 daemon 把 off 点成 pf=False 后，就没有任何账本
            # 记得"该恢复成 True"了——单屏停留在关态且无人负责（评审席 P1-3）。
            # 延后清账，下一轮按真实 pref 重新决策。已 stale 的请求在这里
            # 顺手回收（回收逻辑原本只在派发路径跑，门闩路径会让死账本
            # 永远推迟清标记——我席复审发现）。
            log("pending request still in flight; deferring marker clear")
            return 0
        log(f"already restored (pref={pf}); clearing flag")
        state.pop("closed_by_script", None)
        state.pop("closed_by_seq", None)
        state["restored_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return 0
    ok, msg = set_stage_manager(want, dry_run=dry_run)
    log(f"restore to {want}: {msg}")
    if not ok:
        # in-flight：守护进程正在点，本轮不清标记、不报失败，下一轮用真实 pref 判断
        if "in-flight" not in msg:
            return 1
        return 0
    if not dry_run:
        state.pop("closed_by_script", None)
        state.pop("closed_by_seq", None)
        state["restored_at"] = datetime.now(timezone.utc).isoformat()
        state["screens_at_restore"] = n
        save_state(state)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()

    if args.probe:
        n = screen_count()
        pf = pref_enabled()
        st = load_state()
        print(json.dumps({
            "screens": n,
            "pref_enabled": pf,
            "closed_by_script": bool(st.get("closed_by_script")),
        }, ensure_ascii=False))
        # 探测失败要能被脚本/CI 识别：null 值 + exit 非零
        return 0 if (n is not None and pf is not None) else 3

    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        # launchd 的 stderr 指向 /dev/null（见 plist 模板），未捕获的崩溃
        # 若不写进轮转日志就彻底丢失。这里兜底记录 traceback。
        import traceback
        log("FATAL: uncaught exception:\n" + traceback.format_exc())
        sys.exit(4)
