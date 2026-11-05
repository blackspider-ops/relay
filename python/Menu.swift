import AppKit

final class MenuController: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var item: NSStatusItem!
    var stream: Process?
    var buffer = Data()
    var current: [String: Any] = [:]
    var message: String?
    var actionNotice: String?
    var actionFailed = false
    var actionRunning = false
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
    @discardableResult func action(_ title: String, args: [String], into menu: NSMenu) -> NSMenuItem {
        let entry = NSMenuItem(title: title, action: #selector(runAction(_:)), keyEquivalent: "")
        entry.target = self; entry.representedObject = args
        entry.isEnabled = !actionRunning || args.first == "copy"
        entry.identifier = NSUserInterfaceItemIdentifier("action:" + args.joined(separator: "|")); menu.addItem(entry)
        return entry
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
        let attention = message != nil || actionFailed || sessions.contains { ($0["state"] as? String) == "needs_attention" }
        updateIcon(attention: attention)
        item.button?.toolTip = "Relay · \(sessions.count) sessions · \(accounts.count) accounts"
        let menu = makeMenu()
        updateTimes(menu)
        if let visible = item.menu { MenuController.synchronise(visible, with: menu) }
        else { item.menu = menu }
    }
    func makeMenu() -> NSMenu {
        let sessions = current["sessions"] as? [[String: Any]] ?? []
        let accounts = current["accounts"] as? [[String: Any]] ?? []
        let liveSessions = sessions.filter { $0["running"] as? Bool ?? true }
        let menu = NSMenu(); menu.delegate = self
        menu.autoenablesItems = false
        text("Relay · \(sessions.count) sessions", into: menu)
        if let message = message { text(message, into: menu) }
        if let notice = actionNotice {
            text(notice, into: menu)
            if !actionRunning { action("Dismiss message", args: ["dismiss"], into: menu) }
        }
        if accounts.isEmpty { text("Add an account with relay setup in Terminal.", into: menu) }
        if liveSessions.isEmpty && !accounts.isEmpty { text("No Relay sessions running. Start with relay run.", into: menu) }
        for account in accounts {
            let name = account["name"] as? String ?? "Account"
            let accountID = account["id"] as? String ?? ""
            let assigned = liveSessions.filter { $0["account"] as? String == accountID }
            let inUse = assigned.isEmpty ? "" : assigned.count == 1 ? " · In use" : " · In use by \(assigned.count) sessions"
            let primary = account["primary"] as? [String: Any]
            let short = account["short_remaining"] as? String ?? (primary?["usedPercent"] as? NSNumber).map { "\(max(0, 100 - $0.intValue))%" } ?? "?"
            let remaining = short == "?" ? "allowance unknown" : short + " left"
            let secondary = account["secondary"] as? [String: Any]
            let weekly = account["weekly_remaining"] as? String ?? (secondary?["usedPercent"] as? NSNumber).map { "\(max(0, 100 - $0.intValue))%" } ?? "?"
            let label = (account["enabled"] as? Bool ?? true) ? "Short \(short) · Week \(weekly)" : "disabled"
            let submenu = NSMenu()
            submenu.autoenablesItems = false
            if let email = account["email"] as? String { text(email, into: submenu) }
            if !assigned.isEmpty {
                for session in assigned {
                    let sessionName = session["name"] as? String ?? String((session["id"] as? String ?? "").prefix(10))
                    text("In use by: \(sessionName)", into: submenu)
                }
                text("To switch: open that session → Switch account.", into: submenu)
            }
            text("Short window: \(remaining)", into: submenu)
            if weekly != "?" { text("Weekly window: \(weekly) left", into: submenu) }
            for row in MenuController.accountTimingRows(account) {
                if let at = row.at { countdown(row.title, value: at, into: submenu) }
                else { text(row.title, into: submenu) }
            }
            let lastChecked = MenuController.timestamp(account["usage_checked_at"]).map { date($0) } ?? "Not checked yet"
            text("Last checked: \(lastChecked)", into: submenu)
            if let id = account["id"] as? String {
                submenu.addItem(.separator())
                let pendingReset = account["reset_pending"] as? Bool ?? false
                let resetCount = (account["reset_credits"] as? NSNumber)?.intValue
                if let count = resetCount { text("Usage resets available: \(count)", into: submenu) }
                else { text("Usage resets: check allowance to see availability.", into: submenu) }
                if pendingReset || (resetCount ?? 0) > 0 {
                    let reset = action(pendingReset ? "Check previous reset…" : "Use available reset…", args: ["confirm-reset", id, name, pendingReset ? "pending" : "new"], into: submenu)
                    reset.isEnabled = !actionRunning && !(account["maintenance"] as? Bool ?? false)
                }
                submenu.addItem(.separator())
                let enabled = account["enabled"] as? Bool ?? true
                action(enabled ? "Disable for future routing" : "Enable for routing", args: ["account", enabled ? "disable" : "enable", id], into: submenu)
                text("Current tasks keep running.", into: submenu)
            }
            let entry = NSMenuItem(title: "\(name)\(inUse) · \(label)", action: nil, keyEquivalent: "")
            entry.state = assigned.isEmpty ? .off : .on
            entry.identifier = NSUserInterfaceItemIdentifier("account:" + (account["id"] as? String ?? name))
            entry.submenu = submenu; menu.addItem(entry)
        }
        if !sessions.isEmpty { menu.addItem(.separator()) }
        for session in sessions {
            let id = session["id"] as? String ?? ""
            let name = session["name"] as? String ?? String(id.prefix(10))
            let status = (session["state"] as? String ?? "unknown").replacingOccurrences(of: "_", with: " ")
            let submenu = NSMenu()
            submenu.autoenablesItems = false
            text("Account: \(session["account_name"] as? String ?? session["account"] as? String ?? "unknown")", into: submenu)
            text("Model: \(session["model"] as? String ?? "unknown")", into: submenu)
            if let next = MenuController.timestamp(session["next_wake"]) { countdown("Next action", value: next, into: submenu) }
            if let reason = session["reason"] as? String { text(reason.replacingOccurrences(of: "_", with: " "), into: submenu) }
            action("Copy reconnect command", args: ["copy", "relay attach " + id], into: submenu)
            if session["running"] as? Bool ?? true {
                let paused = session["paused"] as? Bool ?? false
                action(paused ? "Resume recovery" : "Pause recovery", args: [paused ? "unpause" : "pause", id, "--wait"], into: submenu)
                let choices = NSMenu(); choices.autoenablesItems = false
                for account in accounts {
                    guard let accountID = account["id"] as? String else { continue }
                    let selected = accountID == session["account"] as? String
                    let models = account["models"] as? [String] ?? []
                    let model = session["model"] as? String
                    let modelAllowed = models.isEmpty || model == nil || models.contains(model!)
                    let available = (account["enabled"] as? Bool ?? true) && !(account["maintenance"] as? Bool ?? false) && modelAllowed
                    let name = account["name"] as? String ?? accountID
                    let choice = action(name + (selected ? " · Current" : available ? "" : " · unavailable"), args: ["switch", id, "--account", accountID, "--wait"], into: choices)
                    choice.state = selected ? .on : .off
                    choice.isEnabled = !selected && available && !actionRunning
                }
                text("Switch after current work and approvals finish.", into: choices)
                let switchItem = NSMenuItem(title: "Switch account", action: nil, keyEquivalent: "")
                switchItem.identifier = NSUserInterfaceItemIdentifier("switch:" + id)
                switchItem.submenu = choices; submenu.addItem(switchItem)
                action("Review and retry…", args: ["confirm-retry", id], into: submenu)
            } else {
                text("Worker stopped. Restart to reconnect; review holds are retained.", into: submenu)
                action("Restart session", args: ["restart", id, "--if-stopped"], into: submenu)
            }
            let entry = NSMenuItem(title: "\(name) · \(status)", action: nil, keyEquivalent: "")
            entry.identifier = NSUserInterfaceItemIdentifier("session:" + id)
            entry.submenu = submenu; menu.addItem(entry)
        }
        menu.addItem(.separator())
        action("Copy dashboard command", args: ["copy", "relay status --watch"], into: menu)
        action("Check allowance now", args: ["refresh"], into: menu)
        let login = NSMenuItem(title: "Open at login", action: #selector(toggleLogin), keyEquivalent: ""); login.target = self
        login.isEnabled = !actionRunning
        login.state = FileManager.default.fileExists(atPath: NSHomeDirectory() + "/Library/LaunchAgents/local.relay.menu.plist") ? .on : .off
        menu.addItem(login)
        let quit = NSMenuItem(title: "Quit Relay menu", action: #selector(quitMenu), keyEquivalent: "q"); quit.target = self; menu.addItem(quit)
        return menu
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
        if args[0] == "dismiss" { actionNotice = nil; actionFailed = false; render(); return }
        guard !actionRunning else { return }
        if args[0] == "confirm-retry" {
            let alert = NSAlert(); alert.messageText = "Continue this task?"; alert.informativeText = "Review any interrupted command or external action in the conversation before continuing."
            alert.addButton(withTitle: "Continue task"); alert.addButton(withTitle: "Cancel")
            NSApp.activate(ignoringOtherApps: true)
            guard alert.runModal() == .alertFirstButtonReturn else { return }
            args = ["retry", args[1], "--wait"]
        }
        if args[0] == "confirm-reset" {
            let pending = args[3] == "pending"
            let alert = NSAlert(); alert.messageText = pending ? "Check the previous reset?" : "Use one usage reset?"
            alert.informativeText = args[2] + "\n\n" + (pending ? "Relay will check the same pending request. If it already used a reset, another one will not be spent." : "This spends one earned reset on eligible usage limits for this account. Allowance will be checked afterward. Paused tasks and review holds stay paused.")
            alert.addButton(withTitle: pending ? "Check reset" : "Use reset"); alert.addButton(withTitle: "Cancel")
            NSApp.activate(ignoringOtherApps: true)
            guard alert.runModal() == .alertFirstButtonReturn else { return }
            args = ["account", "reset", args[1], "--yes", "--request-id", UUID().uuidString]
        }
        actionRunning = true; actionFailed = false; actionNotice = "Applying change…"; render()
        let isReset = Array(args.prefix(2)) == ["account", "reset"]
        let proc = process(args); let pipe = Pipe()
        if isReset { proc.standardOutput = pipe } else { proc.standardOutput = FileHandle.nullDevice }
        proc.standardError = pipe
        DispatchQueue.global(qos: .utility).async { [weak self] in
            do {
                try proc.run(); let bytes = pipe.fileHandleForReading.readDataToEndOfFile(); proc.waitUntilExit()
                let failed = proc.terminationStatus != 0
                let detail = String(data: bytes, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                let resetResult = detail.split(separator: "\n").last.map(String.init) ?? "Reset check finished. Check the account's allowance."
                DispatchQueue.main.async {
                    self?.actionRunning = false; self?.actionFailed = failed
                    self?.actionNotice = failed ? (detail.isEmpty ? "Action failed. Check relay events." : detail) : isReset ? resetResult : MenuController.successMessage(args)
                    self?.render(); self?.refreshSnapshot()
                }
            } catch { DispatchQueue.main.async { self?.actionRunning = false; self?.actionFailed = true; self?.actionNotice = "Could not run that action."; self?.render() } }
        }
    }
    static func successMessage(_ args: [String]) -> String {
        switch args.first {
        case "switch": return "Account switched."
        case "pause": return "Recovery paused. Current work can finish."
        case "unpause": return "Recovery resumed."
        case "retry": return "Task allowed to continue."
        case "restart": return "Session restarted."
        case "refresh": return "Allowance check finished. See each account’s last checked time."
        case "account": return args.dropFirst().first == "disable" ? "Account disabled for future routing." : "Account enabled for routing."
        default: return "Change applied."
        }
    }
    @objc func toggleLogin() {
        let enabled = FileManager.default.fileExists(atPath: NSHomeDirectory() + "/Library/LaunchAgents/local.relay.menu.plist")
        let sender = NSMenuItem()
        sender.representedObject = enabled ? ["menubar", "--no-startup", "--configure-only"] : ["menubar", "--configure-only"]
        runAction(sender)
    }
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
    func findAction(_ args: [String], in menu: NSMenu) -> NSMenuItem? {
        for entry in menu.items {
            if entry.representedObject as? [String] == args { return entry }
            if let child = entry.submenu, let found = findAction(args, in: child) { return found }
        }
        return nil
    }
    let controller = MenuController()
    controller.current = ["accounts": [["id": "one", "name": "One"], ["id": "two", "name": "Two"], ["id": "off", "enabled": false]], "sessions": [["id": "live", "account": "one", "running": true], ["id": "stopped", "running": false]]]
    let controls = controller.makeMenu()
    let switchArgs = ["switch", "live", "--account", "two", "--wait"]
    let correctChoices = findAction(switchArgs, in: controls)?.isEnabled == true && findAction(["switch", "live", "--account", "one", "--wait"], in: controls)?.state == .on && findAction(["switch", "live", "--account", "off", "--wait"], in: controls)?.isEnabled == false
    let stoppedControls = findAction(["restart", "stopped", "--if-stopped"], in: controls) != nil && findAction(["pause", "stopped", "--wait"], in: controls) == nil
    let activeMarker = controls.items.first { $0.identifier?.rawValue == "account:one" }
    let currentChoice = findAction(["switch", "live", "--account", "one", "--wait"], in: controls)
    let correctMarker = activeMarker?.state == .on && activeMarker?.title.contains("In use") == true && currentChoice?.title.contains("Current") == true
    let originalSessions = controller.current["sessions"]
    controller.current["sessions"] = [["id": "a", "account": "one", "running": true], ["id": "b", "account": "one", "running": true], ["id": "c", "account": "two", "running": true], ["id": "d", "account": "off", "running": false]]
    let multiple = controller.makeMenu()
    let one = multiple.items.first { $0.identifier?.rawValue == "account:one" }
    let two = multiple.items.first { $0.identifier?.rawValue == "account:two" }
    let off = multiple.items.first { $0.identifier?.rawValue == "account:off" }
    let correctMultiple = one?.title.contains("In use by 2 sessions") == true && two?.state == .on && off?.state == .off
    controller.current["sessions"] = [["id": "stopped", "account": "one", "running": false]]
    let empty = controller.makeMenu()
    let correctEmpty = empty.items.contains { $0.title.contains("No Relay sessions running") } && empty.items.first { $0.identifier?.rawValue == "account:one" }?.state == .off
    controller.current["sessions"] = originalSessions
    let originalAccounts = controller.current["accounts"]
    controller.current["accounts"] = [["id": "one", "name": "One", "reset_credits": 2], ["id": "two", "name": "Two", "reset_credits": 0], ["id": "unknown", "name": "Unknown"], ["id": "pending", "name": "Pending", "reset_pending": true]]
    let resets = controller.makeMenu()
    let correctResets = findAction(["confirm-reset", "one", "One", "new"], in: resets)?.isEnabled == true && findAction(["confirm-reset", "two", "Two", "new"], in: resets) == nil && findAction(["confirm-reset", "unknown", "Unknown", "new"], in: resets) == nil && findAction(["confirm-reset", "pending", "Pending", "pending"], in: resets) != nil
    controller.actionRunning = true
    let resetLocked = findAction(["confirm-reset", "one", "One", "new"], in: controller.makeMenu())?.isEnabled == false
    controller.current["accounts"] = originalAccounts
    controller.actionRunning = true; controller.actionNotice = "Applying change…"
    let pending = controller.makeMenu()
    let correctPending = findAction(switchArgs, in: pending)?.isEnabled == false && findAction(["refresh"], in: pending)?.isEnabled == false && findAction(["copy", "relay attach live"], in: pending)?.isEnabled == true
    controller.actionRunning = false; controller.actionNotice = "Task is busy."
    controller.current["at"] = 2000000001.0
    let retainedResult = controller.makeMenu().items.contains { $0.title == "Task is busy." }
    print("Account choices and stopped controls correct: \(correctChoices && stoppedControls)")
    print("Pending action protected and result retained: \(correctPending && retainedResult)")
    print("In-use accounts and current choice marked: \(correctMarker && correctMultiple && correctEmpty)")
    print("Available and pending reset actions protected: \(correctResets && resetLocked)")
    exit(normal != nil && attention != nil && liveUpdate && safeIdentity && correctTiming && correctChoices && stoppedControls && correctPending && retainedResult && correctMarker && correctMultiple && correctEmpty && correctResets && resetLocked ? 0 : 1)
} else {
    let app = NSApplication.shared
    let delegate = MenuController(); app.delegate = delegate; app.run()
}
