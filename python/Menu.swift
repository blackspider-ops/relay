import AppKit

final class MenuController: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var item: NSStatusItem!
    var stream: Process?
    var buffer = Data()
    var current: [String: Any] = [:]
    var message: String?
    var reconnect: DispatchWorkItem?
    var stopping = false
    var snapshotInFlight = false
    let command = Bundle.main.object(forInfoDictionaryKey: "RelayCommand") as? String ?? "/usr/bin/false"
    let state = Bundle.main.object(forInfoDictionaryKey: "RelayState") as? String ?? ""

    func applicationDidFinishLaunching(_ notification: Notification) {
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: "local.relay.menu")
        if others.contains(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }) { NSApp.terminate(nil); return }
        NSApp.setActivationPolicy(.accessory)
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.title = ""
        item.button?.setAccessibilityLabel("Relay account and session status")
        updateIcon(attention: false)
        render()
        startStream()
    }
    func process(_ args: [String]) -> Process {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: command)
        proc.arguments = args
        var env = ProcessInfo.processInfo.environment
        let node = Bundle.main.object(forInfoDictionaryKey: "RelayNodeDirectory") as? String ?? "/opt/homebrew/bin"
        env["PATH"] = node + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
        env["RELAY_STATE"] = state
        proc.environment = env
        return proc
    }
    func startStream() {
        guard !stopping else { return }
        let proc = process(["status", "--watch", "--json"])
        let pipe = Pipe()
        proc.standardOutput = pipe; proc.standardError = FileHandle.nullDevice
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let bytes = handle.availableData
            guard !bytes.isEmpty else { handle.readabilityHandler = nil; return }
            DispatchQueue.main.async { self?.receive(bytes) }
        }
        proc.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self = self, !self.stopping else { return }
                self.message = "Status connection interrupted. Retrying in 30 seconds."
                self.render()
                let retry = DispatchWorkItem { [weak self] in self?.startStream() }
                self.reconnect = retry
                DispatchQueue.main.asyncAfter(deadline: .now() + 30, execute: retry)
            }
        }
        do { buffer.removeAll(); try proc.run(); stream = proc }
        catch { message = "Relay could not start. Open Terminal and run relay doctor."; render() }
    }
    func receive(_ bytes: Data) {
        buffer.append(bytes)
        while let newline = buffer.firstIndex(of: 10) {
            let line = buffer.prefix(upTo: newline); buffer.removeSubrange(...newline)
            if let value = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] {
                if (value["at"] as? Double ?? 0) >= (current["at"] as? Double ?? 0) { current = value; message = nil; render() }
            }
        }
    }
    func refreshSnapshot() {
        guard !snapshotInFlight, !stopping else { return }
        snapshotInFlight = true
        let proc = process(["status", "--json"])
        let pipe = Pipe(); proc.standardOutput = pipe; proc.standardError = FileHandle.nullDevice
        DispatchQueue.global(qos: .utility).async { [weak self] in
            var snapshot: [String: Any]?
            do {
                try proc.run()
                let timeout = DispatchWorkItem { if proc.isRunning { proc.terminate() } }
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 8, execute: timeout)
                let bytes = pipe.fileHandleForReading.readDataToEndOfFile(); proc.waitUntilExit(); timeout.cancel()
                if proc.terminationStatus == 0 { snapshot = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any] }
            } catch { }
            let result = snapshot
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.snapshotInFlight = false
                if let value = result {
                    let at = value["at"] as? Double ?? 0
                    if at >= (self.current["at"] as? Double ?? 0) { self.current = value; self.render() }
                }
            }
        }
    }
    static func synchronise(_ live: NSMenu, with desired: NSMenu) {
        let incoming = desired.items
        while live.items.count > incoming.count { live.removeItem(at: live.items.count - 1) }
        for (index, fresh) in incoming.enumerated() {
            if index >= live.items.count {
                desired.removeItem(fresh); live.addItem(fresh); continue
            }
            let existing = live.items[index]
            if existing.isSeparatorItem != fresh.isSeparatorItem || existing.identifier != fresh.identifier {
                live.removeItem(at: index); desired.removeItem(fresh); live.insertItem(fresh, at: index); continue
            }
            existing.title = fresh.title; existing.state = fresh.state
            existing.target = fresh.target; existing.action = fresh.action
            existing.keyEquivalent = fresh.keyEquivalent; existing.isEnabled = fresh.isEnabled
            existing.representedObject = fresh.representedObject; existing.toolTip = fresh.toolTip
            if let child = fresh.submenu {
                if let previous = existing.submenu { synchronise(previous, with: child) }
                else { fresh.submenu = nil; existing.submenu = child }
            } else { existing.submenu = nil }
        }
    }
    func updateIcon(attention: Bool) {
        let symbol = attention ? "exclamationmark.circle.fill" : "arrow.triangle.branch"
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: attention ? "Relay needs attention" : "Relay")
        image?.isTemplate = true
        item.button?.image = image
    }
    func text(_ title: String, into menu: NSMenu) {
        let entry = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        entry.isEnabled = false; menu.addItem(entry)
    }
    func action(_ title: String, args: [String], into menu: NSMenu) {
        let entry = NSMenuItem(title: title, action: #selector(runAction(_:)), keyEquivalent: "")
        entry.target = self; entry.representedObject = args
        entry.identifier = NSUserInterfaceItemIdentifier("action:" + args.joined(separator: "|")); menu.addItem(entry)
    }
    struct TimingRow {
        let title: String
        let at: Double?
    }
    static func timestamp(_ value: Any?) -> Double? {
        guard let seconds = value as? Double, seconds.isFinite, seconds > 0 else { return nil }
        return seconds
    }
    static func accountTimingRows(_ account: [String: Any]) -> [TimingRow] {
        var rows: [TimingRow] = []
        if let primary = account["primary"] as? [String: Any], let at = timestamp(primary["resetsAt"]) {
            rows.append(TimingRow(title: "Short reset", at: at))
        }
        if let secondary = account["secondary"] as? [String: Any], let at = timestamp(secondary["resetsAt"]) {
            rows.append(TimingRow(title: "Weekly reset", at: at))
        }
        if let at = timestamp(account["next_check"]) { rows.append(TimingRow(title: "Next allowance check", at: at)) }
        else { rows.append(TimingRow(title: "Allowance checked when needed", at: nil)) }
        return rows
    }
    func date(_ value: Any?) -> String {
        guard let seconds = MenuController.timestamp(value) else { return "Not available" }
        return Date(timeIntervalSince1970: seconds).formatted(date: .abbreviated, time: .shortened)
    }
    func countdown(_ title: String, value: Any, into menu: NSMenu) {
        let entry = NSMenuItem(title: title + ": " + date(value), action: nil, keyEquivalent: "")
        entry.representedObject = ["label": title, "at": value]
        menu.addItem(entry)
    }
    func updateTimes(_ menu: NSMenu) {
        for entry in menu.items {
            if let data = entry.representedObject as? [String: Any], let at = data["at"] as? Double, let label = data["label"] as? String {
                let seconds = max(0, Int(at - Date().timeIntervalSince1970))
                let remaining = seconds < 60 ? "under a minute" : seconds < 3600 ? "\(seconds / 60)m" : seconds < 86400 ? "\(seconds / 3600)h \((seconds % 3600) / 60)m" : "\(seconds / 86400)d \((seconds % 86400) / 3600)h"
                let elapsed = label.contains("reset") ? "reported time passed" : "due now"
                entry.title = label + ": " + (seconds == 0 ? elapsed : "in " + remaining) + " · " + date(at)
            }
            if let child = entry.submenu { updateTimes(child) }
        }
    }
    func render() {
        let sessions = current["sessions"] as? [[String: Any]] ?? []
        let accounts = current["accounts"] as? [[String: Any]] ?? []
        let attention = message != nil || sessions.contains { ($0["state"] as? String) == "needs_attention" }
        updateIcon(attention: attention)
        item.button?.toolTip = "Relay · \(sessions.count) sessions · \(accounts.count) accounts"
        let menu = NSMenu(); menu.delegate = self
        text("Relay · \(sessions.count) sessions", into: menu)
        if let message = message { text(message, into: menu) }
        if accounts.isEmpty { text("Add an account with relay setup in Terminal.", into: menu) }
        for account in accounts {
            let name = account["name"] as? String ?? "Account"
            let primary = account["primary"] as? [String: Any]
            let short = account["short_remaining"] as? String ?? (primary?["usedPercent"] as? NSNumber).map { "\(max(0, 100 - $0.intValue))%" } ?? "?"
            let remaining = short == "?" ? "allowance unknown" : short + " left"
            let secondary = account["secondary"] as? [String: Any]
            let weekly = account["weekly_remaining"] as? String ?? (secondary?["usedPercent"] as? NSNumber).map { "\(max(0, 100 - $0.intValue))%" } ?? "?"
            let label = (account["enabled"] as? Bool ?? true) ? "Short \(short) · Week \(weekly)" : "disabled"
            let submenu = NSMenu()
            if let email = account["email"] as? String { text(email, into: submenu) }
            text("Short window: \(remaining)", into: submenu)
            if weekly != "?" { text("Weekly window: \(weekly) left", into: submenu) }
            for row in MenuController.accountTimingRows(account) {
                if let at = row.at { countdown(row.title, value: at, into: submenu) }
                else { text(row.title, into: submenu) }
            }
            let lastChecked = MenuController.timestamp(account["usage_checked_at"]).map { date($0) } ?? "Not checked yet"
            text("Last checked: \(lastChecked)", into: submenu)
            let entry = NSMenuItem(title: "\(name) · \(label)", action: nil, keyEquivalent: "")
            entry.identifier = NSUserInterfaceItemIdentifier("account:" + (account["id"] as? String ?? name))
            entry.submenu = submenu; menu.addItem(entry)
        }
        if !sessions.isEmpty { menu.addItem(.separator()) }
        for session in sessions {
            let id = session["id"] as? String ?? ""
            let name = session["name"] as? String ?? String(id.prefix(10))
            let status = (session["state"] as? String ?? "unknown").replacingOccurrences(of: "_", with: " ")
            let submenu = NSMenu()
            text("Account: \(session["account_name"] as? String ?? session["account"] as? String ?? "unknown")", into: submenu)
            text("Model: \(session["model"] as? String ?? "unknown")", into: submenu)
            if let next = MenuController.timestamp(session["next_wake"]) { countdown("Next action", value: next, into: submenu) }
            if let reason = session["reason"] as? String { text(reason.replacingOccurrences(of: "_", with: " "), into: submenu) }
            action("Copy reconnect command", args: ["copy", "relay attach " + id], into: submenu)
            action("Pause recovery", args: ["pause", id, "--wait"], into: submenu)
            action("Resume recovery", args: ["unpause", id, "--wait"], into: submenu)
            action("Review and retry…", args: ["confirm-retry", id], into: submenu)
            let entry = NSMenuItem(title: "\(name) · \(status)", action: nil, keyEquivalent: "")
            entry.identifier = NSUserInterfaceItemIdentifier("session:" + id)
            entry.submenu = submenu; menu.addItem(entry)
        }
        menu.addItem(.separator())
        action("Copy dashboard command", args: ["copy", "relay status --watch"], into: menu)
        action("Check allowance now", args: ["refresh"], into: menu)
        let login = NSMenuItem(title: "Open at login", action: #selector(toggleLogin), keyEquivalent: ""); login.target = self
        login.state = FileManager.default.fileExists(atPath: NSHomeDirectory() + "/Library/LaunchAgents/local.relay.menu.plist") ? .on : .off
        menu.addItem(login)
        let quit = NSMenuItem(title: "Quit Relay menu", action: #selector(quitMenu), keyEquivalent: "q"); quit.target = self; menu.addItem(quit)
        updateTimes(menu)
        if let visible = item.menu { MenuController.synchronise(visible, with: menu) }
        else { item.menu = menu }
    }
    func menuWillOpen(_ menu: NSMenu) {
        refreshSnapshot()
        updateTimes(menu)
        for entry in menu.items where entry.title == "Open at login" {
            entry.state = FileManager.default.fileExists(atPath: NSHomeDirectory() + "/Library/LaunchAgents/local.relay.menu.plist") ? .on : .off
        }
    }
    @objc func runAction(_ sender: NSMenuItem) {
        guard var args = sender.representedObject as? [String], !args.isEmpty else { return }
        if args[0] == "copy" { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(args[1], forType: .string); return }
        if args[0] == "confirm-retry" {
            let alert = NSAlert(); alert.messageText = "Continue this task?"; alert.informativeText = "Review any interrupted command or external action in the conversation before continuing."
            alert.addButton(withTitle: "Continue task"); alert.addButton(withTitle: "Cancel")
            NSApp.activate(ignoringOtherApps: true)
            guard alert.runModal() == .alertFirstButtonReturn else { return }
            args = ["retry", args[1], "--wait"]
        }
        let proc = process(args); let pipe = Pipe(); proc.standardOutput = FileHandle.nullDevice; proc.standardError = pipe
        DispatchQueue.global(qos: .utility).async { [weak self] in
            do {
                try proc.run(); let bytes = pipe.fileHandleForReading.readDataToEndOfFile(); proc.waitUntilExit()
                if proc.terminationStatus != 0 { DispatchQueue.main.async { self?.message = String(data: bytes, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "Action failed. Check relay events."; self?.render() } }
            } catch { DispatchQueue.main.async { self?.message = "Could not run that action."; self?.render() } }
        }
    }
    @objc func toggleLogin() {
        let enabled = FileManager.default.fileExists(atPath: NSHomeDirectory() + "/Library/LaunchAgents/local.relay.menu.plist")
        runCommand(enabled ? ["menubar", "--no-startup", "--configure-only"] : ["menubar", "--configure-only"])
    }
    func runCommand(_ args: [String]) { let proc = process(args); proc.standardOutput = FileHandle.nullDevice; proc.standardError = FileHandle.nullDevice; try? proc.run() }
    @objc func quitMenu() { stopping = true; reconnect?.cancel(); stream?.terminate(); NSApp.terminate(nil) }
    func applicationWillTerminate(_ notification: Notification) { stopping = true; stream?.terminate() }
}
if CommandLine.arguments.contains("--self-check") {
    let normal = NSImage(systemSymbolName: "arrow.triangle.branch", accessibilityDescription: "Relay")
    let attention = NSImage(systemSymbolName: "exclamationmark.circle.fill", accessibilityDescription: "Relay needs attention")
    print("Relay menu symbols available: \(normal != nil && attention != nil)")
    let visible = NSMenu(); visible.addItem(NSMenuItem(title: "Short 83%", action: nil, keyEquivalent: ""))
    let existing = visible.items[0]
    let updated = NSMenu(); updated.addItem(NSMenuItem(title: "Short 30%", action: nil, keyEquivalent: ""))
    MenuController.synchronise(visible, with: updated)
    let liveUpdate = visible.items[0] === existing && existing.title == "Short 30%"
    print("Visible menu update preserved: \(liveUpdate)")
    existing.identifier = NSUserInterfaceItemIdentifier("session:old")
    let replacement = NSMenu(); let newItem = NSMenuItem(title: "New task", action: nil, keyEquivalent: "")
    newItem.identifier = NSUserInterfaceItemIdentifier("session:new"); replacement.addItem(newItem)
    MenuController.synchronise(visible, with: replacement)
    let safeIdentity = visible.items[0] !== existing && visible.items[0].identifier == newItem.identifier
    print("Session action identity preserved: \(safeIdentity)")
    let timing = MenuController.accountTimingRows(["next_check": NSNull(), "primary": ["resetsAt": 2000000000.0], "secondary": ["resetsAt": 2000100000.0]])
    let scheduled = MenuController.accountTimingRows(["next_check": 2000200000.0])
    let correctTiming = timing.map { $0.title } == ["Short reset", "Weekly reset", "Allowance checked when needed"] && scheduled.first?.title == "Next allowance check" && scheduled.first?.at == 2000200000.0
    print("Empty retry schedule handled: \(correctTiming)")
    exit(normal != nil && attention != nil && liveUpdate && safeIdentity && correctTiming ? 0 : 1)
} else {
    let app = NSApplication.shared
    let delegate = MenuController(); app.delegate = delegate; app.run()
}
