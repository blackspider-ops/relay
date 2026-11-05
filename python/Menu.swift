import AppKit

final class MenuController: NSObject, NSApplicationDelegate {
    var item: NSStatusItem!
    var timer: Timer?
    var busy = false
    let command = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : (Bundle.main.object(forInfoDictionaryKey: "RelayCommand") as? String ?? "/usr/bin/false")
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "Relay"
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in self?.refresh() }
    }
    @objc func refresh() {
        if busy { return }; busy = true
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self = self else { return }
            let process = Process(); process.executableURL = URL(fileURLWithPath: self.command)
            process.arguments = ["status", "--json"]
            let pipe = Pipe(); process.standardOutput = pipe; process.standardError = FileHandle.nullDevice
            var value: [String: Any] = [:]
            do {
                try process.run()
                let bytes = pipe.fileHandleForReading.readDataToEndOfFile(); process.waitUntilExit()
                value = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any] ?? [:]
            } catch { }
            let result = value
            DispatchQueue.main.async { self.busy = false; self.render(result) }
        }
    }
    func render(_ value: [String: Any]) {
        let sessions = value["sessions"] as? [[String: Any]] ?? []
        let accounts = value["accounts"] as? [[String: Any]] ?? []
        item.button?.title = "Relay \(sessions.count)"
        let menu = NSMenu()
        let heading = NSMenuItem(title: "\(sessions.count) sessions · \(accounts.count) accounts", action: nil, keyEquivalent: "")
        menu.addItem(heading); menu.addItem(.separator())
        for account in accounts {
            let name = account["name"] as? String ?? "Account"
            let primary = account["primary"] as? [String: Any]
            let used = (primary?["usedPercent"] as? NSNumber)?.intValue
            let remaining = used.map { "\(max(0,100-$0))% left" } ?? "usage unknown"
            let enabled = account["enabled"] as? Bool ?? true
            menu.addItem(NSMenuItem(title: "\(name): \(enabled ? remaining : "disabled")", action: nil, keyEquivalent: ""))
        }
        for session in sessions {
            let name = session["name"] as? String ?? String((session["id"] as? String ?? "").prefix(10))
            let state = session["state"] as? String ?? "unknown"
            menu.addItem(NSMenuItem(title: "\(name) — \(state)", action: nil, keyEquivalent: ""))
        }
        menu.addItem(.separator())
        let copy = NSMenuItem(title: "Copy dashboard command", action: #selector(copyDashboard), keyEquivalent: ""); copy.target=self; menu.addItem(copy)
        let update = NSMenuItem(title: "Refresh allowance", action: #selector(refreshAllowance), keyEquivalent: ""); update.target=self; menu.addItem(update)
        let reload = NSMenuItem(title: "Refresh menu", action: #selector(refresh), keyEquivalent: ""); reload.target=self; menu.addItem(reload)
        let quit = NSMenuItem(title: "Quit menu", action: #selector(quitMenu), keyEquivalent: "q"); quit.target=self; menu.addItem(quit)
        item.menu=menu
    }
    @objc func copyDashboard() {
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString("relay status --watch", forType: .string)
    }
    @objc func refreshAllowance() {
        let process=Process(); process.executableURL=URL(fileURLWithPath:command);process.arguments=["refresh"]
        process.standardOutput=FileHandle.nullDevice;process.standardError=FileHandle.nullDevice
        try? process.run()
    }
    @objc func quitMenu() { NSApp.terminate(nil) }
}
let app=NSApplication.shared
let delegate=MenuController(); app.delegate=delegate; app.run()
