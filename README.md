# StageManagerGuard

**多显示器时自动关闭 macOS 台前调度（Stage Manager），拔掉外接屏后自动恢复原状。**

它是唯一「记得你原本设置」的 Stage Manager 开关：多屏自动关，单屏恢复成
**你关掉之前的状态**——包括「你本来就没开」的情况，它不会自作主张给你打开。

## 为什么需要它

外接显示器时，Stage Manager 的应用条带会吃掉屏幕边缘、干扰窗口拖拽；
回到单屏时又往往想把它开回来。手动去系统设置里点开关，一天要来回点两次。

## 工作原理（以及为什么这么绕）

macOS 27 实测（macOS 13+ 同理，未逐一验证）：

1. **`defaults write com.apple.windowmanager GloballyEnabled` 不生效。**
   偏好值会落盘、WindowManager 也不重启，但系统行为完全不变；
   `killall -HUP WindowManager` 与真 `killall WindowManager` 同样无效
   （已用系统设置里的 checkbox 读回坐实）。
2. **唯一可靠通道是点击「系统设置 › 桌面与程序坞」里的 Stage Manager 开关。**
   点击后 UI 值、偏好值、真实窗口行为三者同步改变。
3. **launchd 任务点不了它。** launchd 起的进程拿不到辅助功能（AX）权限
   （System Events 一律 `AppleEvent timed out`），AX 授权绑定在用户登录会话上。

因此架构是两进程：

```
┌─────────────────────────────┐      request.json      ┌──────────────────────────────┐
│ guard（launchd，每 3s）      │ ────────────────────▶ │ daemon（用户会话常驻）          │
│ 数显示器（NSScreen）→ 决策    │ ◀──────────────────── │ 真正点击系统设置里的开关         │
│ 只检测、不点 GUI             │      result.json      │ 轮询请求 → 点击 → 读回校验       │
└─────────────────────────────┘                       └──────────────────────────────┘
```

- 显示器数用 `NSScreen` 口径（镜像屏/休眠屏会改变实际可用屏数，`system_profiler` 会虚报）。
- 每次点击都有**读回校验**（UI 值 + 偏好值双确认），不报错≠成功。
- 状态文件区分「本工具关的」与「用户自己关的」：只有前者会被恢复。
- 请求带单调递增 seq + inflight 改名占位 + 超时租约回收，防重复执行与静默停滞。

## 安装

要求：macOS 13+（Stage Manager 引入版本），Command Line Tools（`xcode-select --install`）。

```bash
git clone https://github.com/Test2049micro/StageManagerGuard.git
cd stage-manager-guard
./install.sh
```

然后**手动完成唯一一步授权**（macOS 按二进制授权，只能本人操作）：

> 系统设置 › 隐私与安全性 › 辅助功能 → 勾选 `install.sh` 结束时打印的那个 python3 路径

未授权时 daemon 会持续报 `AppleEvent timed out`，功能不生效（但不会做错事）。

## 卸载

```bash
./uninstall.sh
```

## 已知限制（诚实清单）

- **可靠性未达生产级，请按此预期使用。** AX 点击通道在开发期累计失败 34 次
  （绝大多数是「打不开设置面板」：面板冷启动需 18–28 秒，且 AX 树有间歇性惰性）。
  修复若干竞态后的有效样本仍很少（成功 1 / 失败 3），**不足以支撑任何成功率结论**。
  失败时会在下一轮自动重试，且失败方向是安全的（保持原状、不会做错事），
  但请不要把它当作「装上就一定能用」的工具——它更像一个经过仔细防御的自动化脚本。
- **未经真实多显示器端到端验收。** 开发后期外接显示器已物理下线，
  「多屏 → 自动关闭」主路径只在打桩（stub screen_count=3）下验证了决策与链路，
  真实插拔场景的体验尚未坐实。若你试用，欢迎带日志开 issue。
- 点击过程中会短暂弹出系统设置窗口（空闲 2 分钟后自动关闭）。
- 若 Apple 未来重命名该开关或移动面板，读回校验会失败并**保持原状不乱动**
  （失败方向安全：不会把用户没开的功能打开）。
- 多屏判定以 `NSScreen` 实际在役数为准；镜像模式下两屏计为两屏。
- **非英文系统**：系统设置里的开关名与面板标题随语言本地化（如简体中文「台前调度」
  「桌面与程序坞」）。已内置中/英候选名匹配（中文值取自 Apple 官方 loctable），
  但**中文系统未经实机验证**；其它语言需自行往 `TOGGLE_CANDIDATES` / `WINDOW_CANDIDATES`
  补译名（可直接从 `DesktopSettings.appex/Contents/Resources/Localizable.loctable` 取）。

## 如何确认它在正常工作

```bash
# 1) 两个任务都在
launchctl print gui/$(id -u)/com.local.stage-manager-guard | grep -E 'state|last exit'
launchctl print gui/$(id -u)/com.local.stage-manager-daemon | grep -E 'state'

# 2) 探测与决策正常
python3 src/stage_manager_guard.py --probe
# 输出如 {"screens": 2, "pref_enabled": true, "closed_by_script": false}

# 3) 日志（代码自写、自动轮转，上限约 2MB×2）
#    ~/Library/Application Support/StageManagerGuard/guard.log
#    ~/Library/Application Support/StageManagerGuard/daemon.log
```

常见故障：

| 症状 | 原因 | 处置 |
|---|---|---|
| daemon.log 反复 `AppleEvent timed out` | 未给辅助功能权限 | 系统设置 › 隐私与安全性 › 辅助功能，勾选 install.sh 打印的 python3 |
| daemon.log 反复 `cannot open settings pane` | AX 树间歇性惰性 | 会自动重试；若持续数小时，重启 daemon：`launchctl kickstart -k gui/$(id -u)/com.local.stage-manager-daemon` |
| 崩溃但 launchd 日志为空 | plist 的 stdout 指向 /dev/null | 崩溃 traceback 会写进 guard.log（`FATAL:` 开头） |
| 显示器变了但没动作 | guard 探测失败会拒绝猜测 | 看 guard.log 是否有 `ABORT`；确认 `./bin/DisplayProbe screens` 能跑通 |

## 开发

```bash
make build    # 编译 DisplayProbe（macOS 自带 swiftc）
make test     # 跑回归测试（无第三方依赖）
```

## 许可

MIT
