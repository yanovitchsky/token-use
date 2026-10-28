import AppKit
import Foundation
import SwiftUI

struct ProviderCard: Identifiable {
    let id: String
    let name: String
    let percent: String
    let lines: [String]
    let rows: [(String, String)]
    let total: String?
}

final class UsageModel: ObservableObject {
    @Published var headline = "Loading usage…"
    @Published var cards: [ProviderCard] = []
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var popover: NSPopover!
    private var timer: Timer?
    private let model = UsageModel()
    private var busy = false
    private var globalMonitor: Any?
    private var localMonitor: Any?

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = MenuIcon.image()
            button.imagePosition = .imageLeading
            button.title = " …"
            button.target = self
            button.action = #selector(togglePopover)
        }
        popover = NSPopover()
        popover.behavior = .transient
        popover.animates = true
        popover.contentSize = NSSize(width: 380, height: 460)
        popover.contentViewController = NSHostingController(
            rootView: UsagePanel(model: model) { [weak self] in
                self?.copyReport()
            }
        )
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    @objc private func togglePopover() {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            closePopover()
            return
        }
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        startDismissMonitor()
        refresh()
    }

    private func closePopover() {
        popover.performClose(nil)
        stopDismissMonitor()
    }

    private func startDismissMonitor() {
        stopDismissMonitor()
        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            self?.closePopover()
        }
        localMonitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] event in
            guard let self else { return event }
            if event.window == self.popover.contentViewController?.view.window {
                return event
            }
            if event.window == self.statusItem.button?.window {
                return event
            }
            self.closePopover()
            return event
        }
    }

    private func stopDismissMonitor() {
        if let globalMonitor {
            NSEvent.removeMonitor(globalMonitor)
            self.globalMonitor = nil
        }
        if let localMonitor {
            NSEvent.removeMonitor(localMonitor)
            self.localMonitor = nil
        }
    }

    private func tokuseURL() -> URL? {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        for directory in ["/usr/local/bin", "/opt/homebrew/bin", "\(home)/.local/bin"] {
            let url = URL(fileURLWithPath: directory).appendingPathComponent("tokuse")
            if FileManager.default.isExecutableFile(atPath: url.path) {
                return url
            }
        }
        return nil
    }

    private func refresh() {
        if busy { return }
        guard let binary = tokuseURL() else {
            model.headline = "tokuse is not installed"
            model.cards = []
            statusItem.button?.title = ""
            return
        }
        busy = true
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let result = Self.run(binary, arguments: ["--json"])
            DispatchQueue.main.async {
                guard let self else { return }
                self.busy = false
                if let result {
                    self.apply(json: result)
                } else if self.model.cards.isEmpty {
                    self.model.headline = "Could not load usage"
                }
            }
        }
    }

    private static func run(_ binary: URL, arguments: [String]) -> String? {
        let process = Process()
        process.executableURL = binary
        process.arguments = arguments
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = "/usr/local/bin:/opt/homebrew/bin:\(home)/.local/bin"
        environment["HOME"] = home
        process.environment = environment
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        do {
            try process.run()
        } catch {
            return nil
        }
        let semaphore = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in semaphore.signal() }
        if semaphore.wait(timeout: .now() + 20) == .timedOut {
            process.terminate()
            return nil
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return String(data: data, encoding: .utf8)
    }

    private func apply(json text: String) {
        guard let data = text.data(using: .utf8),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let providers = root["providers"] as? [String: Any] else {
            model.headline = "Could not read usage"
            return
        }
        let order = [("cursor", "Cursor"), ("codex", "Codex"), ("claude", "Claude")]
        var next: [ProviderCard] = []
        var percents: [String] = []
        for (key, name) in order {
            let provider = providers[key] as? [String: Any] ?? [:]
            let card = makeCard(id: key, name: name, provider: provider)
            next.append(card)
            percents.append(card.percent == "—" ? "—" : card.percent)
        }
        model.cards = next
        model.headline = "Usage"
        statusItem.button?.title = " " + percents.joined(separator: "  ")
    }

    private func makeCard(id: String, name: String, provider: [String: Any]) -> ProviderCard {
        let ok = provider["ok"] as? Bool ?? false
        if !ok {
            let error = provider["error"] as? String ?? "Unavailable"
            return ProviderCard(id: id, name: name, percent: "—", lines: [error], rows: [], total: nil)
        }
        let resets = provider["resets"] as? [[String: Any]] ?? []
        let percent = menuPercent(id: id, resets: resets)
        var lines: [String] = []
        for reset in resets {
            let title = reset["name"] as? String ?? "Quota"
            let pct = percentText(reset["used_percent"])
            var bits = ["\(title) \(pct)"]
            if let detail = reset["detail"] as? String, !detail.isEmpty {
                bits.append(detail)
            }
            lines.append(bits.joined(separator: " · "))
        }
        let windows = provider["windows"] as? [String: Any] ?? [:]
        let preferred = ["billing", "today", "7d", "30d"]
        var rows: [(String, String)] = []
        var total: String?
        for key in preferred {
            guard let window = windows[key] as? [String: Any] else { continue }
            if let label = window["label"] as? String, !lines.contains(where: { $0.contains(label) }) {
                lines.insert(label, at: 0)
            }
            let models = window["by_model"] as? [[String: Any]] ?? []
            let sorted = models.sorted { cents($0) > cents($1) }
            for model in sorted.prefix(6) {
                let modelName = model["model"] as? String ?? "unknown"
                rows.append((modelName, amount(model)))
            }
            if rows.count < sorted.count {
                rows.append(("+\(sorted.count - rows.count) more", ""))
            }
            if let totals = window["totals"] as? [String: Any] {
                let input = intValue(totals["input"])
                let output = intValue(totals["output"])
                total = "\(compact(input)) in · \(compact(output)) out"
            }
            if key == "billing" { break }
            if !rows.isEmpty { break }
        }
        if lines.isEmpty { lines = ["No usage yet"] }
        return ProviderCard(id: id, name: name, percent: percent, lines: lines, rows: rows, total: total)
    }

    private func menuPercent(id: String, resets: [[String: Any]]) -> String {
        if id == "cursor" {
            return percentText(resets.first { $0["name"] as? String == "Billing cycle" }?["used_percent"])
        }
        if id == "claude" {
            return percentText(resets.first { $0["name"] as? String == "5h" }?["used_percent"])
        }
        let named = resets.first { $0["name"] as? String == "5h" } ?? resets.first
        return percentText(named?["used_percent"])
    }

    private func percentText(_ value: Any?) -> String {
        guard let number = value as? Double ?? (value as? Int).map(Double.init) ?? (value as? NSNumber).map({ $0.doubleValue }) else {
            return "—"
        }
        return "\(Int(number.rounded()))%"
    }

    private func cents(_ row: [String: Any]) -> Double {
        if let value = row["cents"] as? Double { return value }
        if let value = row["cents"] as? Int { return Double(value) }
        if let value = row["cents"] as? NSNumber { return value.doubleValue }
        return 0
    }

    private func amount(_ row: [String: Any]) -> String {
        let value = cents(row)
        if row["cents"] != nil && !(row["cents"] is NSNull) {
            return String(format: "$%.2f", value / 100)
        }
        let input = intValue(row["input"])
        let output = intValue(row["output"])
        return "\(compact(input)) / \(compact(output))"
    }

    private func intValue(_ value: Any?) -> Int {
        if let value = value as? Int { return value }
        if let value = value as? Double { return Int(value) }
        if let value = value as? NSNumber { return value.intValue }
        return 0
    }

    private func compact(_ value: Int) -> String {
        let number = Double(value)
        if number >= 1_000_000 { return String(format: "%.1fM", number / 1_000_000) }
        if number >= 1_000 { return String(format: "%.1fk", number / 1_000) }
        return "\(value)"
    }

    private func copyReport() {
        guard let binary = tokuseURL(), let text = Self.run(binary, arguments: []) else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}

private enum MenuIcon {
    static func image() -> NSImage {
        let size = NSSize(width: 18, height: 18)
        let image = NSImage(size: size, flipped: false) { _ in
            NSColor.black.setFill()
            let bars: [(CGFloat, CGFloat)] = [(2.2, 7), (7.4, 12.5), (12.6, 5.5)]
            for (x, height) in bars {
                let rect = NSRect(x: x, y: 2.2, width: 3.2, height: height)
                NSBezierPath(roundedRect: rect, xRadius: 1.1, yRadius: 1.1).fill()
            }
            return true
        }
        image.isTemplate = true
        return image
    }
}

private struct UsagePanel: View {
    @ObservedObject var model: UsageModel
    let copy: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(model.headline)
                    .font(.headline)
                Spacer()
                Button("Copy", action: copy)
                    .controlSize(.small)
            }
            .padding(.horizontal, 16)
            .padding(.top, 14)
            .padding(.bottom, 8)

            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if model.cards.isEmpty {
                        Text(model.headline == "Usage" ? "No usage yet" : model.headline)
                            .foregroundStyle(.secondary)
                            .padding(.top, 8)
                    }
                    ForEach(model.cards) { card in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack(alignment: .firstTextBaseline) {
                                Text(card.name)
                                    .font(.headline)
                                Spacer()
                                Text(card.percent)
                                    .font(.title3.monospacedDigit().weight(.semibold))
                            }
                            ForEach(card.lines, id: \.self) { line in
                                Text(line)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            if !card.rows.isEmpty {
                                VStack(spacing: 3) {
                                    ForEach(Array(card.rows.enumerated()), id: \.offset) { _, row in
                                        HStack {
                                            Text(row.0)
                                                .lineLimit(1)
                                            Spacer(minLength: 12)
                                            Text(row.1)
                                                .monospacedDigit()
                                                .foregroundStyle(.secondary)
                                        }
                                        .font(.callout)
                                    }
                                }
                                .padding(.top, 4)
                            }
                            if let total = card.total {
                                Text(total)
                                    .font(.caption.monospacedDigit())
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .padding(12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color(nsColor: .controlBackgroundColor))
                        .clipShape(RoundedRectangle(cornerRadius: 10))
                    }
                }
                .padding(16)
            }
        }
        .frame(width: 380, height: 460)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
