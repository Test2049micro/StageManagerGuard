#!/usr/bin/env python3
"""sm_daemon.py —— 台前调度守卫的执行端（常驻，必须由已登录用户会话启动）

为什么需要它（2026-10-16 实测，勿凭经验推翻）：
  launchd 任务**拿不到辅助功能（AX）权限**：它能启动 System Settings，
  但 System Events 对它一律 `AppleEvent timed out (-1712)`，
  `launchctl asuser` 同样无效。原因是 AX 授权绑定在用户的登录会话上。
  因此「检测显示器数量」可以放在 launchd 里，「点击开关」必须放在用户会话的进程里。

架构：
  launchd（com.local.stage-manager-guard）  ——  只检测显示器数量，不做任何点击。
      多屏且台前调度开着 → 写 REQUEST 文件请求关闭
      单屏且是脚本关的   → 写 REQUEST 文件请求恢复
  本守护进程（由用户会话启动，开机自启）  ——  轮询 REQUEST 文件并真正点击开关，
      完成后写 RESULT 文件回报成败。

  REQUEST / RESULT 用文件而不是信号，是为了让 launchd 与守护进程解耦：
  守护进程重启不会丢请求，且请求内容可审计。

用法：
  python3 sm_daemon.py            # 常驻（供 launchd 开机自启）
  python3 sm_daemon.py --once     # 处理一次待办请求后退出（调试用）
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
# 状态目录与 guard 共用：可用 SMG_HOME 覆盖，默认落在 macOS 标准位置
DIR = os.environ.get(
    "SMG_HOME", os.path.expanduser("~/Library/Application Support/StageManagerGuard"))
REQ_PATH = os.path.join(DIR, "request.json")
RESULT_PATH = os.path.join(DIR, "result.json")
SEQ_PATH = os.path.join(DIR, "last-seq.json")
LOCK_PATH = os.path.join(DIR, "daemon.lock")
LOG_PATH = os.path.join(DIR, "daemon.log")

SETTINGS_PANE_URL = "x-apple.systempreferences:com.apple.Desktop-Settings.extension"
# 无障碍名/窗口标题随系统语言本地化，单字符串精确匹配在非英文系统会静默失效。
# 英文值本机实测；中文值取自 Apple 官方本地化表（一手证据，勿凭记忆改）：
#   /System/Library/ExtensionKit/Extensions/DesktopSettings.appex/Contents/Resources/Localizable.loctable
#   "Stage Manager" -> zh_CN "台前调度" / zh_TW "幕前調度" / zh_HK "幕前管理"
#   "Desktop & Dock" -> zh_CN "桌面与程序坞" / zh_TW|zh_HK "桌面與Dock"
# 中文候选未经中文系统实测；匹配不到时 fail-safe（读不到即不动 GUI）。
WINDOW_CANDIDATES = ["Desktop & Dock", "桌面与程序坞", "桌面與Dock"]
TOGGLE_CANDIDATES = ["Stage Manager", "台前调度", "幕前調度", "幕前管理"]
# 兼容旧代码里对这两个名字的引用
SETTINGS_WINDOW = WINDOW_CANDIDATES[0]
TOGGLE_NAME = TOGGLE_CANDIDATES[0]


def _as_list(items: list) -> str:
    """转成 AppleScript 列表字面量，如 {"a", "b"}。"""
    return "{" + ", ".join('"%s"' % i for i in items) + "}"

POLL_INTERVAL = 1.0
RETRY_LIMIT = 5
RETRY_SLEEP = 2.0
# 点击后 pref（GloballyEnabled）落后 UI 值的实测窗口 ~5s；此处给 2 倍余量
PREF_LAG_WAIT = 10
OPEN_WAIT = 3.0
# 设置面板从 open 到 AX 可读的实测耗时：18–28 秒（2026-10-16 三次采样）。
# 单次 osascript 调用本身要数秒，故轮询次数要给足；总预算约 3 + 20*2 = 43 秒。
PANE_READY_TRIES = 20
CLICK_SETTLE = 1.5
_OSA_TMP_N = 0                # osascript 临时文件序号（保证文件名唯一）

# 窗口名与开关名都按候选集合匹配（系统语言不同则不同），
# 在 AppleScript 内遍历窗口，避免 Python 侧写死某个语言的标题。
_READ_UI = """\
tell application "System Events"
    tell process "System Settings"
        repeat with w in windows
            try
                set allUI to entire contents of w
                repeat with u in allUI
                    try
                        if (class of u as string) is "checkbox" and {names} contains (name of u as string) then
                            return (value of u as string)
                        end if
                    end try
                end repeat
            end try
        end repeat
        return "NOT_FOUND"
    end tell
end tell
"""

_CLICK_UI = """\
tell application "System Events"
    tell process "System Settings"
        repeat with w in windows
            try
                set allUI to entire contents of w
                repeat with u in allUI
                    try
                        if (class of u as string) is "checkbox" and {names} contains (name of u as string) then
                            click u
                            return "CLICKED"
                        end if
                    end try
                end repeat
            end try
        end repeat
        return "NOT_FOUND"
    end tell
end tell
"""


LOG_MAX_BYTES = 2_000_000   # 代码自持日志的轮转阈值（macOS 无 logrotate）


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        # 轮转：超限滚到 .1。只管代码自己 open 的这份；
        # launchd 重定向的 daemon.launchd.log 由 launchd 持有 fd，rename 无效，
        # 故 plist 模板把 launchd 的 stdout/stderr 指向 /dev/null（见 resources/）。
        try:
            if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
                os.replace(LOG_PATH, LOG_PATH + ".1")
        except OSError:
            pass
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# osascript stderr 里的 AX 权限拒绝签名（小写比对）。注意与 -1712 超时区分：
# 超时多数是 AX 惰性或非用户会话启动，权限拒绝是另一回事，混在一起会误导排查。
AX_DENY_SIGNATURES = (
    "assistive access",
    "not authorized to send apple events",
    "not allowed to send apple events",
)


def ax_deny_hint(err: str):
    """识别辅助功能权限拒绝并把 osascript 天书报错翻译成可操作指引。

    新人第一个坑：没授 AX 权限时只会看到 "Not authorized to send Apple
    events (-1743)"，不知道要去系统设置开权限。非权限错误返回 None 不打扰。
    """
    low = (err or "").lower()
    for sig in AX_DENY_SIGNATURES:
        if sig in low:
            return ("辅助功能（AX）权限被拒：打开 系统设置 → 隐私与安全性 → 辅助功能，"
                    "给运行本守护进程的程序（终端/iTerm 等）打开开关后重试。"
                    f"原始报错: {err[:200]}")
    return None


_AX_HINTED = [False]    # 权限提示每次进程生命周期只报一次，避免重试时刷屏


def osa(script: str, timeout: int = 30):
    """执行 AppleScript。

    走临时文件而非 `osascript -e <多行脚本>`：`-e` 对多行脚本的报错行号会失真
    （会把错误指到不存在的行），排查时被误导过。写成文件执行报错行号才准。
    文件名带 pid + 计数器保证唯一，避免并发调用互相读到半截文件。
    """
    global _OSA_TMP_N
    _OSA_TMP_N += 1
    tmp = os.path.join(DIR, f"_osa_{os.getpid()}_{_OSA_TMP_N}.applescript")
    try:
        os.makedirs(DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(script)
        r = subprocess.run(["osascript", tmp],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 and not _AX_HINTED[0]:
            hint = ax_deny_hint(r.stderr or "")
            if hint:
                _AX_HINTED[0] = True
                log(hint)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as e:
        return -1, "", str(e)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def pref_enabled():
    try:
        r = subprocess.run(["defaults", "read", "com.apple.windowmanager", "GloballyEnabled"],
                           capture_output=True, text=True, timeout=15)
        s = (r.stdout or "").strip()
        if s == "1":
            return True
        if s == "0":
            return False
        return None
    except Exception:
        return None


def await_pref_adopted(desired: bool, wait: float = PREF_LAG_WAIT) -> bool:
    """等 pref（GloballyEnabled）跟上 UI 值。

    点击后 pref 落后 UI 约 5 秒（实测）。与其每轮烧一次 8–11 秒的 osascript
    重试去"再确认"，不如每秒读一次便宜的 defaults 等它跟上；等待有界
    （wait 上限），永不跟上时返回 False 交回外层重试逻辑。
    """
    deadline = time.time() + wait
    while True:
        # 先查截止再读：把超出窗口的读数压到最坏一次（读本身有 15s 超时上限，
        # 无法中断——评审二轮 P2-7 指出"硬上限"说不过去，改为由外层
        # RETRY_LIMIT 预算兜底，这里只负责不再每轮都白等满窗口）。
        if time.time() >= deadline:
            return False
        if pref_enabled() == desired:
            return True
        time.sleep(1.0)


def open_pane() -> bool:
    """打开「桌面与程序坞」并等到 UI 树真的可读。

    历史坑（2026-10-16 都踩过）：
    1) 只查一次窗口名不够：`open` 返回成功后 Settings 进程虽起，AX 树要再过几秒才可取。
    2) `open` 之后到 AX 就绪可能要 10 秒以上（进程冷启动 + 面板渲染），
       每轮只等 1 秒会连续误判"打不开"。
    3) 每次处理完都 `close_pane()` 会把 Settings 关掉，下一次又要重新冷启动，
       反复开闭放大竞态、拖慢整体链路。故成功后保留窗口，由空闲时再关。
    """
    osa(f'open location "{SETTINGS_PANE_URL}"', timeout=25)
    # 首轮多等：Settings 冷启动 + 面板渲染实测 18–28 秒（见 PANE_READY_TRIES 注释）
    time.sleep(OPEN_WAIT)
    osa('tell application "System Settings" to activate', timeout=25)
    if read_ui() != "NOT_FOUND":
        return True
    for i in range(PANE_READY_TRIES):
        time.sleep(2.0)
        if read_ui() != "NOT_FOUND":
            return True
        sentinel_shot()
    return False


def close_pane() -> None:
    osa('tell application "System Settings" to quit', timeout=20)


def sentinel_shot() -> None:
    try:
        subprocess.run(["screencapture", "-x", "-o", "/tmp/sm-daemon-sentinel.png"],
                       capture_output=True, timeout=20)
    except Exception:
        pass


def read_ui() -> str:
    rc, out, _ = osa(_READ_UI.format(names=_as_list(TOGGLE_CANDIDATES)))
    return out if out in ("0", "1") else "NOT_FOUND"


def click_ui() -> str:
    rc, out, _ = osa(_CLICK_UI.format(names=_as_list(TOGGLE_CANDIDATES)))
    return out if out in ("CLICKED", "NOT_FOUND") else f"ERR:{rc}:{out}"


# 合并成一次 osascript：读当前值 → 需要则点击 → 读回。
# 为什么必须合并：实测单次 `osascript` 冷启动就要 10–22 秒（AppleEvent 建连），
# 拆成"读一次、点一次、读回一次"会把单轮拖到 45–100 秒，用户体感极差。
# 合并后一轮只有一次建连开销。
#
# ⚠️ AppleScript 变量名不要用 `after`：它是保留字，用了会报
#    "Expected expression but found 'to'"，且报错行号是假的（恒为 541）——
#    2026-10-16 在这个坑上绕了很久，一度误判成 osascript -e 的参数解析问题。
#    这里用 `newval`。
# 另注：用标准 tell 块而非 `tell A to tell B` 单行形式，内嵌 delay 更稳。
_TOGGLE_ONE_SHOT = """\
tell application "System Events"
    tell process "System Settings"
        repeat with w in windows
            try
                set allUI to entire contents of w
                repeat with u in allUI
                    try
                        if (class of u as string) is "checkbox" and {names} contains (name of u as string) then
                            set cur to (value of u as string)
                            if cur is "{want}" then
                                return "ALREADY|" & cur
                            end if
                            click u
                            delay {settle}
                            set newval to (value of u as string)
                            return "CLICKED|" & cur & "|" & newval
                        end if
                    end try
                end repeat
            end try
        end repeat
        return "NOT_FOUND"
    end tell
end tell
"""


def toggle_one_shot(want: str, settle: float) -> tuple[str, str]:
    """一次 osascript 内完成读/点/读回。返回 (状态, 详情)。"""
    rc, out, err = osa(_TOGGLE_ONE_SHOT.format(
        names=_as_list(TOGGLE_CANDIDATES), want=want, settle=settle), timeout=40)
    if out.startswith(("ALREADY|", "CLICKED|")):
        return out.split("|")[0], out
    return "NOT_FOUND", f"rc={rc} err={err[:120]}"


def restart_settings_app() -> None:
    """彻底重启 System Settings：连读不到 checkbox 时，重启往往比干等更有效。

    实测（2026-10-16）：AX 惰性下连拍哨兵截图也连续 5 次取不到 checkbox，
    重启 Settings 后立刻可取。这里只在"连续两次读不到"时才重启，避免每次抖动都重启。
    """
    log("restarting System Settings to recover from AX laziness")
    close_pane()
    time.sleep(2.0)


_LAST_ACTIVITY = [0.0]      # 最后一次真正操作开关的时间（用于空闲关窗）
IDLE_CLOSE_AFTER = 120      # 空闲多久后关掉设置窗口（秒）


def _maybe_idle_close() -> None:
    """空闲超过阈值就关掉设置窗口，避免长期挂在屏幕上。

    注意：不每次处理完都关。反复开闭 Settings 会让下一次 open 重新冷启动，
    实测把单轮从 ~5 秒拖到 45 秒以上，且放大 AX 竞态导致"打不开"。
    """
    if _LAST_ACTIVITY[0] and (time.time() - _LAST_ACTIVITY[0]) > IDLE_CLOSE_AFTER:
        close_pane()
        _LAST_ACTIVITY[0] = 0.0


def do_set(desired: bool) -> tuple[bool, str]:
    if not open_pane():
        return False, "cannot open settings pane"
    try:
        want = "1" if desired else "0"
        not_found_streak = 0
        for attempt in range(1, RETRY_LIMIT + 1):
            sentinel_shot()
            state, detail = toggle_one_shot(want, CLICK_SETTLE)
            if state == "NOT_FOUND":
                not_found_streak += 1
                log(f"attempt {attempt}: toggle not found (AX laziness), streak={not_found_streak} [{detail}]")
                if not_found_streak >= 2 and attempt < RETRY_LIMIT:
                    restart_settings_app()
                    if not open_pane():
                        return False, "cannot reopen settings pane after restart"
                time.sleep(RETRY_SLEEP)
                continue
            not_found_streak = 0
            if state == "ALREADY":
                # UI 已是目标值不等于系统已采纳：实测出现过 ui=1 而 pref=False 的
                # 短暂不一致。故这里仍要复核 pref，不一致就先等它跟上（见
                # await_pref_adopted），等不到才走下一轮完整重试。
                pf = pref_enabled()
                if pf == desired or await_pref_adopted(desired):
                    # 与 CLICKED 分支一样要记活动时间：否则反复处理"已是目标态"的
                    # 请求时 _LAST_ACTIVITY 停在旧值，120 秒后窗口被误关，
                    # 下一次又要 18–28 秒冷启动。
                    _LAST_ACTIVITY[0] = time.time()
                    return True, f"already {want}"
                log(f"attempt {attempt}: ui already {want} but pref not adopted "
                    f"after {PREF_LAG_WAIT}s wait")
                time.sleep(RETRY_SLEEP)
                continue
            # CLICKED|cur|newval —— newval 是点完读回的 UI 值
            newval = detail.split("|")[-1]
            pf_after = pref_enabled()
            if newval == want and pf_after != desired:
                # pref 滞后是常态（~5s）：轮询等它跟上，别急着判失败
                if await_pref_adopted(desired):
                    pf_after = desired
            if newval == want and pf_after == desired:
                _LAST_ACTIVITY[0] = time.time()      # 记下活动时间，供空闲关窗
                return True, f"set to {want} (ui={newval} pref={pf_after})"
            log(f"attempt {attempt}: readback ui={newval} pref={pf_after} want={want}")
            time.sleep(RETRY_SLEEP)
        return False, f"failed after {RETRY_LIMIT} attempts"
    finally:
        # 不在这里关窗：见 _maybe_idle_close 的说明（反复开闭会拖慢并放大竞态）。
        # 仅在彻底失败时才关，让下一轮从干净状态重新打开。
        pass


def do_set_guarded(desired: bool) -> tuple[bool, str]:
    """do_set 的包装：失败时负责收尾（关窗），成功时保留窗口交给空闲逻辑。"""
    ok, msg = do_set(desired)
    if not ok:
        close_pane()
    else:
        _LAST_ACTIVITY[0] = time.time()
    return ok, msg


def write_result(ok: bool, msg: str, req: dict) -> None:
    try:
        os.makedirs(DIR, exist_ok=True)
        payload = {
            "ok": ok,
            "msg": msg,
            "requested": req.get("action"),
            "seq": req.get("seq"),
            "at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = RESULT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, RESULT_PATH)
    except OSError as e:
        log(f"write_result failed: {e}")


def load_last_seq():
    """读水位。非法值（被敌意文件毒化成巨数等）直接当无水位自愈——
    否则一次毒化会让所有后续请求被永久跳过（fuzz 层 I5c 实测）。"""
    try:
        with open(SEQ_PATH, encoding="utf-8") as f:
            seq = json.load(f).get("seq")
    except Exception:
        return None
    return seq if _valid_seq(seq) else None


# 正常 seq = time_ns 量级（~1.8e18），永远小于 2^63。超过它的必是伪造：
# 落进水位会让所有后续正常请求被永久跳过（红队 #2 的巨数毒化）
# 正常 seq = time_ns 量级（~1.8e18），上界留 ~70 年余量。2^63-1 太宽——
# 2^62 就足以饿死一切正常请求（fuzz 层 I5c 实测），故收紧到 time_ns 尺度
SEQ_MAX = 4_000_000_000_000_000_000


def _valid_seq(v) -> bool:
    """请求 seq 必须是严格的正整数：bool（True==1）、浮点（7.0==7）、
    负数/零、超过上界的巨数一律拒收（红队 #2/#14）。"""
    return isinstance(v, int) and not isinstance(v, bool) and 0 < v <= SEQ_MAX


def save_last_seq(seq: int) -> None:
    try:
        tmp = SEQ_PATH + ".tmp"
        # O_NOFOLLOW：tmp 不许是符号链接（红队 #8 的 daemon 侧同款）
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"seq": seq}, f)
        os.replace(tmp, SEQ_PATH)
    except OSError:
        pass


INFLIGHT_PATH = os.path.join(DIR, "request.inflight")


def process_once() -> bool:
    """处理一个待办请求。返回是否处理过。

    ⚠️ 必须先把 request.json **改名占位**再处理：早期是"读完 → 处理（数十秒）→ 最后删"，
    这期间主循环（每秒一轮）会再次读到同一个请求并重复处理
    （实测同一 seq 被连续处理两次，两个开关动作互相抵消）。改名后文件即消失，天然防重入。
    """
    try:
        os.replace(REQ_PATH, INFLIGHT_PATH)
    except OSError:
        return False          # 没有待办请求
    try:
        with open(INFLIGHT_PATH, encoding="utf-8") as f:
            req = json.load(f)
    except Exception:
        try:
            os.remove(INFLIGHT_PATH)
        except OSError:
            pass
        return True
    if not isinstance(req, dict):
        # 数组/字符串等非对象请求：json.load 能吃下去，随后的 req.get 会
        # AttributeError 崩掉主循环且 inflight 永久残留（红队 #10）。
        # 回报失败并收尾，绝不带着它继续。
        log(f"rejecting malformed request (not an object): {req!r}"[:120])
        write_result(False, "malformed request (not an object)", {})
        try:
            os.remove(INFLIGHT_PATH)
        except OSError:
            pass
        return True

    action = req.get("action")
    if action not in ("off", "restore"):
        log(f"ignoring unknown request action={action!r}")
        try:
            os.remove(INFLIGHT_PATH)
        except OSError:
            pass
        return True

    # 请求里带 seq（单调递增）：守护进程只处理比上次更新的 seq，
    # 这样 guard 连续写多个请求时不会拿旧请求覆盖新意图。
    seq = req.get("seq")
    if not _valid_seq(seq):
        # 非法 seq（bool/浮点/负数/巨数）：不执行、不污染水位，回报失败并收尾
        log(f"rejecting request with malformed seq={seq!r} action={action}")
        write_result(False, f"malformed seq {seq!r}", req)
        try:
            os.remove(INFLIGHT_PATH)
        except OSError:
            pass
        return True
    last_seq = load_last_seq()
    if isinstance(last_seq, int) and seq <= last_seq:
        log(f"skipping stale request seq={seq} (last={last_seq}) action={action}")
        # 跳过也要清 inflight：留着它会让 guard 的 pending 判定永远看到
        # 一笔"待办"（红队 #2 的残留部分）
        try:
            os.remove(INFLIGHT_PATH)
        except OSError:
            pass
        return True

    # restore_to 必须严格是 True 才当真：bool("false") 是真值，字符串
    # "false" 不得驱动一次反向操作（红队 #1 的 daemon 侧防线）
    desired = (action == "restore") and req.get("restore_to") is True
    log(f"handling request seq={seq} action={action} desired_enabled={desired}")
    # 执行端任何异常（do_set_guarded 内部的子进程错误等）都不得让 inflight
    # 残留——残留会让 guard 的 pending 判定永远看到一笔假待办（不变式审计
    # #2-6）。异常如实记为失败结果。
    try:
        ok, msg = do_set_guarded(desired)
    except Exception as e:      # noqa: BLE001 —— 执行层兜底，失败如实上报
        log(f"do_set_guarded raised ({type(e).__name__}); reporting failure")
        ok, msg = False, f"executor error: {type(e).__name__}"
    log(f"request {action} -> ok={ok} {msg}")
    save_last_seq(seq)
    write_result(ok, msg, req)
    try:
        os.remove(INFLIGHT_PATH)
    except OSError:
        pass
    return True


class SingleInstance:
    """单实例文件锁。

    两个 daemon 同时点开关会互相抵消（一个开一个关），实测直接导致
    读回校验失败。用 flock 保证同一时刻只有一个执行者。
    """

    def __init__(self, path: str):
        self.path = path
        self.fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            raise RuntimeError("another sm_daemon is already running")
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return self

    def __exit__(self, *exc):
        if self.fh:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_UN)
            finally:
                self.fh.close()
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    try:
        with SingleInstance(LOCK_PATH):
            if args.once:
                process_once()
                return 0
            log("sm_daemon started (resident executor; needs AX permission from user session)")
            while True:
                try:
                    process_once()
                    _maybe_idle_close()
                except Exception as e:
                    log(f"loop error: {e}")
                time.sleep(POLL_INTERVAL)
    except RuntimeError as e:
        log(f"exit: {e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
