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
SETTINGS_WINDOW = "Desktop & Dock"
TOGGLE_NAME = "Stage Manager"

POLL_INTERVAL = 1.0
RETRY_LIMIT = 5
RETRY_SLEEP = 2.0
OPEN_WAIT = 3.0
# 设置面板从 open 到 AX 可读的实测耗时：18–28 秒（2026-10-16 三次采样）。
# 单次 osascript 调用本身要数秒，故轮询次数要给足；总预算约 3 + 20*2 = 43 秒。
PANE_READY_TRIES = 20
CLICK_SETTLE = 1.5
_OSA_TMP_N = 0                # osascript 临时文件序号（保证文件名唯一）

_READ_UI = """\
tell application "System Events" to tell process "System Settings"
    set allUI to entire contents of window "{win}"
    repeat with u in allUI
        try
            if (class of u as string) is "checkbox" and (name of u as string) is "{name}" then
                return (value of u as string)
            end if
        end try
    end repeat
    return "NOT_FOUND"
end tell
"""

_CLICK_UI = """\
tell application "System Events" to tell process "System Settings"
    set allUI to entire contents of window "{win}"
    repeat with u in allUI
        try
            if (class of u as string) is "checkbox" and (name of u as string) is "{name}" then
                click u
                return "CLICKED"
            end if
        end try
    end repeat
    return "NOT_FOUND"
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
    rc, out, _ = osa(_READ_UI.format(win=SETTINGS_WINDOW, name=TOGGLE_NAME))
    return out if out in ("0", "1") else "NOT_FOUND"


def click_ui() -> str:
    rc, out, _ = osa(_CLICK_UI.format(win=SETTINGS_WINDOW, name=TOGGLE_NAME))
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
        set allUI to entire contents of window "{win}"
        repeat with u in allUI
            try
                if (class of u as string) is "checkbox" and (name of u as string) is "{name}" then
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
        return "NOT_FOUND"
    end tell
end tell
"""


def toggle_one_shot(want: str, settle: float) -> tuple[str, str]:
    """一次 osascript 内完成读/点/读回。返回 (状态, 详情)。"""
    rc, out, err = osa(_TOGGLE_ONE_SHOT.format(
        win=SETTINGS_WINDOW, name=TOGGLE_NAME, want=want, settle=settle), timeout=40)
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
                # 短暂不一致。故这里仍要复核 pref，不一致就继续尝试而非谎报成功。
                pf = pref_enabled()
                if pf == desired:
                    return True, f"already {want}"
                log(f"attempt {attempt}: ui already {want} but pref={pf} (not yet adopted)")
                time.sleep(RETRY_SLEEP)
                continue
            # CLICKED|cur|newval —— newval 是点完读回的 UI 值
            newval = detail.split("|")[-1]
            pf_after = pref_enabled()
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
            "at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = RESULT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, RESULT_PATH)
    except OSError as e:
        log(f"write_result failed: {e}")


def load_last_seq():
    try:
        with open(SEQ_PATH, encoding="utf-8") as f:
            return json.load(f).get("seq")
    except Exception:
        return None


def save_last_seq(seq: int) -> None:
    try:
        tmp = SEQ_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
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

    action = req.get("action")
    if action not in ("off", "restore"):
        log(f"ignoring unknown request action={action!r}")
        return True

    # 请求里带 seq（单调递增）：守护进程只处理比上次更新的 seq，
    # 这样 guard 连续写多个请求时不会拿旧请求覆盖新意图。
    seq = req.get("seq")
    last_seq = load_last_seq()
    if isinstance(seq, int) and isinstance(last_seq, int) and seq <= last_seq:
        log(f"skipping stale request seq={seq} (last={last_seq}) action={action}")
        return True

    desired = (action == "restore") and bool(req.get("restore_to", False))
    log(f"handling request seq={seq} action={action} desired_enabled={desired}")
    ok, msg = do_set_guarded(desired)
    log(f"request {action} -> ok={ok} {msg}")
    if isinstance(seq, int):
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
