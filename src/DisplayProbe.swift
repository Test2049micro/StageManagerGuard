// DisplayProbe.swift —— 显示器计数 + 台前调度 UI 状态探测
//
// 用法：
//   DisplayProbe screens    # JSON: {"screens": N, "detail":[...]}  真实在役显示器数（NSScreen，主口径）
//   DisplayProbe pref       # JSON: {"enabled": bool}               com.apple.windowmanager.GloballyEnabled
//   DisplayProbe windows    # JSON: {"named": N, "big": N}          窗口行为信号（台前调度开启时大窗口会减少）
//
// 为什么 NSScreen 是唯一口径：台前调度/镜像屏/休眠屏都会改变 NSScreen.screens 的实际数量。
// system_profiler 会把物理连接的显示器（含休眠/关闭的）都列出来，与"用户实际可用几屏"不一致，
// 且其 stdout 经管道读取在部分环境下返回空，故不采用、不做交叉验证。
import AppKit
import CoreGraphics
import Foundation

func emit(_ obj: Any) {
    let d = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
    FileHandle.standardOutput.write(d)
}

func screenEntries() -> [[String: Any]] {
    var out: [[String: Any]] = []
    for s in NSScreen.screens {
        var e: [String: Any] = ["name": s.localizedName, "is_main": (s == NSScreen.main), "w": s.frame.width, "h": s.frame.height]
        if let id = s.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber {
            e["id"] = id.uint32Value
        }
        out.append(e)
    }
    return out
}

func prefEnabled() -> Bool {
    let task = Process()
    task.launchPath = "/usr/bin/defaults"
    task.arguments = ["read", "com.apple.windowmanager", "GloballyEnabled"]
    let pipe = Pipe()
    task.standardOutput = pipe
    task.standardError = FileHandle(forWritingAtPath: "/dev/null")
    do {
        try task.run()
        let s = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return s == "1"
    } catch { return false }
}

func windowSignal() -> [String: Any] {
    guard let list = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] else {
        return ["error": "cgwindowlist_unavailable"]
    }
    var named = 0, big = 0
    for w in list {
        if (w[kCGWindowLayer as String] as? Int ?? -1) != 0 { continue }
        guard let b = w[kCGWindowBounds as String] as? [String: Any] else { continue }
        let name = (w[kCGWindowName as String] as? String) ?? ""
        let wd = b["Width"] as? Int ?? 0, h = b["Height"] as? Int ?? 0
        if !name.isEmpty { named += 1 }
        if wd * h > 400_000 { big += 1 }
    }
    return ["named": named, "big": big]
}

let cmd = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "screens"
switch cmd {
case "screens":
    emit(["screens": NSScreen.screens.count, "detail": screenEntries()])
case "pref":
    emit(["enabled": prefEnabled()])
case "windows":
    emit(windowSignal())
default:
    FileHandle.standardError.write("usage: DisplayProbe screens|pref|windows\n".data(using: .utf8)!)
    exit(2)
}
