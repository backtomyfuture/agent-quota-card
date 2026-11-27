import AppKit
import Combine
import CoreGraphics
import Foundation
import SwiftUI

// MARK: - Models

struct QuotaPayload: Codable {
    let generated_at: String
    let providers: [ProviderQuota]
}

struct ProviderQuota: Codable, Identifiable {
    let id: String
    let name: String
    let cli: String
    let status: String
    let subtitle: String?
    let error: String?
    let items: [QuotaItem]
}

struct QuotaItem: Codable, Identifiable {
    var id: String { label }
    let label: String
    let remaining_percent: Double?
    let used_percent: Double?
    let reset_at: String?
    let detail: String?
}

struct CatalogPayload: Codable {
    let providers: [CatalogEntry]
}

struct CatalogEntry: Codable, Identifiable {
    let id: String
    let name: String
    let cli: String
    let vendor: String
    let detected: Bool
}

// MARK: - Store

enum MenuBarStyle: String, CaseIterable {
    case iconOnly = "iconOnly"
    case lowestOnly = "lowestOnly"
    case compactThree = "compactThree"

    var label: String {
        switch self {
        case .iconOnly: return "仅图标"
        case .lowestOnly: return "仅最低项"
        case .compactThree: return "前3项"
        }
    }
}

@MainActor
final class QuotaStore: ObservableObject {
    @Published var providers: [ProviderQuota] = []
    @Published var catalog: [CatalogEntry] = []
    @Published var enabledProviderIDs: [String] = []
    @Published var lastUpdated: Date?
    @Published var isLoading: Bool = false
    @Published var globalError: String?
    @Published var showingSettings: Bool = false
    @Published var menuBarStyle: MenuBarStyle {
        didSet {
            UserDefaults.standard.set(menuBarStyle.rawValue, forKey: "QuotaCard.menuBarStyle")
        }
    }
    @Published var isPinned: Bool {
        didSet {
            UserDefaults.standard.set(isPinned, forKey: "QuotaCard.isPinned")
            onPinChanged?(isPinned)
        }
    }
    @Published var isCollapsed: Bool {
        didSet {
            UserDefaults.standard.set(isCollapsed, forKey: "QuotaCard.isCollapsed")
        }
    }
    @Published var refreshMinutes: Int {
        didSet {
            UserDefaults.standard.set(refreshMinutes, forKey: "QuotaCard.refreshMinutes")
            rescheduleTimer()
        }
    }

    var onPinChanged: ((Bool) -> Void)?
    private var timer: Timer?
    private static let enabledKey = "QuotaCard.enabledProviders"

    init() {
        let savedMinutes = UserDefaults.standard.integer(forKey: "QuotaCard.refreshMinutes")
        self.refreshMinutes = savedMinutes > 0 ? savedMinutes : 15
        if let rawStyle = UserDefaults.standard.string(forKey: "QuotaCard.menuBarStyle"),
           let style = MenuBarStyle(rawValue: rawStyle) {
            self.menuBarStyle = style
        } else {
            self.menuBarStyle = .iconOnly
        }
        if UserDefaults.standard.object(forKey: "QuotaCard.isPinned") != nil {
            self.isPinned = UserDefaults.standard.bool(forKey: "QuotaCard.isPinned")
        } else {
            self.isPinned = true
        }
        self.isCollapsed = UserDefaults.standard.bool(forKey: "QuotaCard.isCollapsed")

        if let savedIDs = UserDefaults.standard.stringArray(forKey: Self.enabledKey), !savedIDs.isEmpty {
            self.enabledProviderIDs = savedIDs
        }

        loadCatalogAndInitialRefresh()
        rescheduleTimer()
    }

    func rescheduleTimer() {
        timer?.invalidate()
        let interval = TimeInterval(max(1, refreshMinutes) * 60)
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.refresh()
            }
        }
    }

    func loadCatalogAndInitialRefresh() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let catResult = Self.runCatalog()
            DispatchQueue.main.async {
                guard let self else { return }
                if case .success(let entries) = catResult {
                    self.catalog = entries
                    if self.enabledProviderIDs.isEmpty {
                        let detected = entries.filter(\.detected).map(\.id)
                        let initial = detected.isEmpty ? ["agy", "droid", "grok", "amp"] : detected
                        self.setEnabledProviders(initial, triggerRefresh: false)
                    }
                } else if self.enabledProviderIDs.isEmpty {
                    self.setEnabledProviders(["agy", "droid", "grok", "amp"], triggerRefresh: false)
                }
                self.refresh()
            }
        }
    }

    func refreshCatalog() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let catResult = Self.runCatalog()
            DispatchQueue.main.async {
                if case .success(let entries) = catResult {
                    self?.catalog = entries
                }
            }
        }
    }

    func isEnabled(_ id: String) -> Bool {
        enabledProviderIDs.contains(id)
    }

    func toggleProvider(_ id: String) {
        var next = enabledProviderIDs
        if let idx = next.firstIndex(of: id) {
            if next.count > 1 {
                next.remove(at: idx)
            }
        } else {
            next.append(id)
        }
        // Keep canonical catalog order when catalog is loaded
        if !catalog.isEmpty {
            let order = Dictionary(uniqueKeysWithValues: catalog.enumerated().map { ($0.element.id, $0.offset) })
            next.sort { (order[$0] ?? 999) < (order[$1] ?? 999) }
        }
        setEnabledProviders(next, triggerRefresh: false)
    }

    func selectDetectedProviders() {
        let detected = catalog.filter(\.detected).map(\.id)
        if !detected.isEmpty {
            setEnabledProviders(detected, triggerRefresh: false)
        }
    }

    func selectAllProviders() {
        let all = catalog.map(\.id)
        if !all.isEmpty {
            setEnabledProviders(all, triggerRefresh: false)
        }
    }

    func selectCoreFourProviders() {
        setEnabledProviders(["agy", "droid", "grok", "amp"], triggerRefresh: false)
    }

    func setEnabledProviders(_ ids: [String], triggerRefresh: Bool) {
        let cleaned = ids.isEmpty ? ["agy", "droid", "grok", "amp"] : ids
        enabledProviderIDs = cleaned
        UserDefaults.standard.set(cleaned, forKey: Self.enabledKey)
        if triggerRefresh {
            refresh()
        }
    }

    func refresh() {
        guard !isLoading else { return }
        isLoading = true
        globalError = nil
        let selected = enabledProviderIDs

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let result = Self.runCollector(selectedIDs: selected)
            DispatchQueue.main.async {
                guard let self else { return }
                self.isLoading = false
                switch result {
                case .success(let payload):
                    self.providers = payload.providers
                    self.lastUpdated = Date()
                case .failure(let err):
                    self.globalError = err.localizedDescription
                }
            }
        }
    }

    private nonisolated static func resolveCollectorPath() -> String? {
        if let bundled = Bundle.main.path(forResource: "quota_collector", ofType: "py"),
           FileManager.default.fileExists(atPath: bundled) {
            return bundled
        }
        let cwdPath = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
            .appendingPathComponent("collector/quota_collector.py").path
        if FileManager.default.fileExists(atPath: cwdPath) {
            return cwdPath
        }
        return nil
    }

    private nonisolated static func resolvePythonPath() -> String {
        let candidates = [
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ]
        for path in candidates where FileManager.default.isExecutableFile(atPath: path) {
            return path
        }
        return "/usr/bin/python3"
    }

    private nonisolated static func buildSubprocessEnv() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let home = env["HOME"] ?? NSHomeDirectory()
        let extraPaths = [
            "\(home)/.local/bin",
            "\(home)/.amp/bin",
            "\(home)/.grok/bin",
            "\(home)/.kimi-code/bin",
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
        let existingPath = env["PATH"] ?? ""
        env["PATH"] = (extraPaths + [existingPath]).filter { !$0.isEmpty }.joined(separator: ":")
        return env
    }

    private nonisolated static func runPython(args: [String]) -> Result<Data, Error> {
        guard let scriptPath = resolveCollectorPath() else {
            return .failure(NSError(domain: "QuotaCard", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "未找到 quota_collector.py",
            ]))
        }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: resolvePythonPath())
        proc.arguments = [scriptPath] + args
        proc.environment = buildSubprocessEnv()

        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        proc.standardOutput = stdoutPipe
        proc.standardError = stderrPipe

        do {
            try proc.run()
            proc.waitUntilExit()
            let outData = stdoutPipe.fileHandleForReading.readDataToEndOfFile()
            let errData = stderrPipe.fileHandleForReading.readDataToEndOfFile()

            guard proc.terminationStatus == 0 else {
                let msg = String(data: errData, encoding: .utf8) ?? "退出码 \(proc.terminationStatus)"
                return .failure(NSError(domain: "QuotaCard", code: Int(proc.terminationStatus), userInfo: [
                    NSLocalizedDescriptionKey: msg.trimmingCharacters(in: .whitespacesAndNewlines),
                ]))
            }
            return .success(outData)
        } catch {
            return .failure(error)
        }
    }

    private nonisolated static func runCatalog() -> Result<[CatalogEntry], Error> {
        switch runPython(args: ["--catalog"]) {
        case .success(let data):
            do {
                let decoded = try JSONDecoder().decode(CatalogPayload.self, from: data)
                return .success(decoded.providers)
            } catch {
                return .failure(error)
            }
        case .failure(let err):
            return .failure(err)
        }
    }

    private nonisolated static func runCollector(selectedIDs: [String]) -> Result<QuotaPayload, Error> {
        var args: [String] = []
        if !selectedIDs.isEmpty {
            args = ["--providers", selectedIDs.joined(separator: ",")]
        }
        switch runPython(args: args) {
        case .success(let data):
            do {
                let decoded = try JSONDecoder().decode(QuotaPayload.self, from: data)
                return .success(decoded)
            } catch {
                return .failure(error)
            }
        case .failure(let err):
            return .failure(err)
        }
    }
}

// MARK: - Visual Helpers

struct VisualEffectView: NSViewRepresentable {
    let material: NSVisualEffectView.Material
    let blendingMode: NSVisualEffectView.BlendingMode

    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = material
        view.blendingMode = blendingMode
        view.state = .active
        return view
    }

    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {
        nsView.material = material
        nsView.blendingMode = blendingMode
    }
}

final class DragRegionNSView: NSView {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
        window?.performDrag(with: event)
    }
}

struct WindowDragArea: NSViewRepresentable {
    func makeNSView(context: Context) -> DragRegionNSView {
        DragRegionNSView()
    }

    func updateNSView(_ nsView: DragRegionNSView, context: Context) {}
}

final class ClickableHostingView<Content: View>: NSHostingView<Content> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
        super.mouseDown(with: event)
    }
}

func colorForRemaining(_ pct: Double) -> Color {
    if pct <= 15 { return .red }
    if pct <= 40 { return .orange }
    return .green
}

func accentForCLI(_ id: String) -> Color {
    switch id {
    case "agy": return Color(red: 0.35, green: 0.62, blue: 1.0)
    case "droid": return Color(red: 0.95, green: 0.56, blue: 0.24)
    case "grok": return Color(red: 0.72, green: 0.52, blue: 1.0)
    case "amp": return Color(red: 0.26, green: 0.80, blue: 0.58)
    case "codex": return Color(red: 0.16, green: 0.74, blue: 0.70)
    case "claude": return Color(red: 0.88, green: 0.48, blue: 0.34)
    case "copilot": return Color(red: 0.55, green: 0.65, blue: 0.98)
    case "kimi": return Color(red: 0.30, green: 0.72, blue: 0.96)
    case "gemini": return Color(red: 0.42, green: 0.58, blue: 0.98)
    case "cursor": return Color(red: 0.62, green: 0.68, blue: 0.76)
    case "freebuff": return Color(red: 0.24, green: 0.82, blue: 0.46)
    case "kiro": return Color(red: 0.60, green: 0.44, blue: 0.98)
    case "glm": return Color(red: 0.28, green: 0.68, blue: 0.94)
    case "deepseek": return Color(red: 0.30, green: 0.52, blue: 1.0)
    case "openrouter": return Color(red: 0.88, green: 0.42, blue: 0.76)
    default: return .accentColor
    }
}

func formatReset(_ isoString: String?) -> String? {
    guard let isoString, !isoString.isEmpty else { return nil }
    let fmtFrac = ISO8601DateFormatter()
    fmtFrac.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let fmtBasic = ISO8601DateFormatter()
    fmtBasic.formatOptions = [.withInternetDateTime]

    guard let date = fmtFrac.date(from: isoString) ?? fmtBasic.date(from: isoString) else {
        return nil
    }
    let seconds = Int(date.timeIntervalSinceNow)
    if seconds <= 0 {
        return "即将重置"
    }
    let hours = seconds / 3600
    let mins = (seconds % 3600) / 60
    if hours >= 48 {
        let df = DateFormatter()
        df.dateFormat = "MM/dd HH:mm"
        return df.string(from: date)
    }
    if hours > 0 {
        return "\(hours)h\(String(format: "%02d", mins))m"
    }
    return "\(max(1, mins))m"
}

// MARK: - Views

struct QuotaCardView: View {
    @ObservedObject var store: QuotaStore
    var onHide: () -> Void

    private static let timeFormatter: DateFormatter = {
        let df = DateFormatter()
        df.dateFormat = "HH:mm:ss"
        return df
    }()

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            headerView

            if store.showingSettings {
                settingsView
            } else if let err = store.globalError {
                Text("采集失败: \(err)")
                    .font(.caption)
                    .foregroundColor(.red)
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.red.opacity(0.12))
                    .cornerRadius(8)
            } else if store.providers.isEmpty {
                HStack(spacing: 8) {
                    ProgressView()
                        .controlSize(.small)
                    Text("正在拉取 AI 智能体配额…")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .center)
                .padding(.vertical, 16)
            } else if store.isCollapsed {
                collapsedSummaryView
            } else if store.providers.count > 4 {
                ScrollView(.vertical, showsIndicators: true) {
                    VStack(spacing: 8) {
                        ForEach(store.providers) { provider in
                            ProviderCardRow(provider: provider)
                        }
                    }
                    .padding(.trailing, 2)
                }
                .frame(maxHeight: 540)
            } else {
                VStack(spacing: 8) {
                    ForEach(store.providers) { provider in
                        ProviderCardRow(provider: provider)
                    }
                }
            }
        }
        .padding(14)
        .frame(width: 356)
        .background(
            ZStack {
                VisualEffectView(material: .hudWindow, blendingMode: .behindWindow)
                WindowDragArea()
            }
            .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(Color.white.opacity(0.16), lineWidth: 1)
        )
        .shadow(color: Color.black.opacity(0.32), radius: 18, x: 0, y: 8)
    }

    private var headerView: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(store.isLoading ? Color.orange : Color.green)
                .frame(width: 8, height: 8)

            Text(store.showingSettings ? "监控配置" : "AI 限额看板")
                .font(.system(size: 13, weight: .semibold))

            if !store.showingSettings, let updated = store.lastUpdated {
                Text(Self.timeFormatter.string(from: updated))
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundColor(.secondary)
            }

            Spacer()

            Button {
                let choices = [5, 10, 15, 30, 60]
                if let idx = choices.firstIndex(of: store.refreshMinutes) {
                    store.refreshMinutes = choices[(idx + 1) % choices.count]
                } else {
                    store.refreshMinutes = 15
                }
            } label: {
                Text("\(store.refreshMinutes)m")
                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                    .foregroundColor(.secondary)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 3)
                    .background(Color.primary.opacity(0.08))
                    .cornerRadius(4)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("点击切换自动刷新间隔（5 / 10 / 15 / 30 / 60 分钟）")

            Button {
                withAnimation(.easeInOut(duration: 0.16)) {
                    if store.showingSettings {
                        store.showingSettings = false
                        store.refresh()
                    } else {
                        store.refreshCatalog()
                        store.showingSettings = true
                    }
                }
            } label: {
                Image(systemName: store.showingSettings ? "checkmark.circle.fill" : "slider.horizontal.3")
                    .font(.system(size: 11.5, weight: .medium))
                    .frame(width: 20, height: 20)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundColor(store.showingSettings ? .accentColor : .secondary)
            .help(store.showingSettings ? "保存并刷新" : "选择监控的智能体")

            if !store.showingSettings {
                Button {
                    store.refresh()
                } label: {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 11, weight: .medium))
                        .frame(width: 20, height: 20)
                        .contentShape(Rectangle())
                        .rotationEffect(.degrees(store.isLoading ? 360 : 0))
                        .animation(
                            store.isLoading
                                ? .linear(duration: 0.8).repeatForever(autoreverses: false)
                                : .default,
                            value: store.isLoading
                        )
                }
                .buttonStyle(.plain)
                .foregroundColor(.secondary)
                .help("立即刷新")
            }

            Button {
                store.isPinned.toggle()
            } label: {
                Image(systemName: store.isPinned ? "pin.fill" : "pin.slash")
                    .font(.system(size: 11, weight: .medium))
                    .frame(width: 20, height: 20)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundColor(store.isPinned ? .accentColor : .secondary)
            .help(store.isPinned ? "当前：始终置顶（点击取消置顶）" : "当前：普通层级（点击置顶）")

            if !store.showingSettings {
                Button {
                    withAnimation(.easeInOut(duration: 0.16)) {
                        store.isCollapsed.toggle()
                    }
                } label: {
                    Image(systemName: store.isCollapsed ? "chevron.down" : "chevron.up")
                        .font(.system(size: 11, weight: .medium))
                        .frame(width: 20, height: 20)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .foregroundColor(.secondary)
                .help(store.isCollapsed ? "展开详情" : "折叠为摘要")
            }

            Button(action: onHide) {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .medium))
                    .frame(width: 20, height: 20)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundColor(.secondary)
            .help("最小化到菜单栏")
        }
    }

    private var settingsView: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Button("自动检测本机") {
                    store.selectDetectedProviders()
                }
                .buttonStyle(.bordered)
                .controlSize(.small)

                Button("仅核心4个") {
                    store.selectCoreFourProviders()
                }
                .buttonStyle(.bordered)
                .controlSize(.small)

                Button("全选") {
                    store.selectAllProviders()
                }
                .buttonStyle(.bordered)
                .controlSize(.small)

                Spacer()

                Text("已选 \(store.enabledProviderIDs.count)")
                    .font(.system(size: 10.5, design: .monospaced))
                    .foregroundColor(.secondary)
            }

            ScrollView(.vertical, showsIndicators: true) {
                VStack(spacing: 5) {
                    ForEach(store.catalog) { entry in
                        let enabled = store.isEnabled(entry.id)
                        Button {
                            store.toggleProvider(entry.id)
                        } label: {
                            HStack(spacing: 8) {
                                RoundedRectangle(cornerRadius: 2)
                                    .fill(accentForCLI(entry.id))
                                    .frame(width: 3, height: 18)

                                VStack(alignment: .leading, spacing: 1) {
                                    HStack(spacing: 5) {
                                        Text(entry.name)
                                            .font(.system(size: 11.5, weight: .medium))
                                            .foregroundColor(.primary)
                                        Text(entry.cli)
                                            .font(.system(size: 9.5, design: .monospaced))
                                            .foregroundColor(accentForCLI(entry.id))
                                            .padding(.horizontal, 4)
                                            .padding(.vertical, 1)
                                            .background(accentForCLI(entry.id).opacity(0.14))
                                            .cornerRadius(3)
                                    }
                                    Text(entry.vendor)
                                        .font(.system(size: 9.5))
                                        .foregroundColor(.secondary)
                                }

                                Spacer()

                                Text(entry.detected ? "已检出" : "未配置")
                                    .font(.system(size: 9.5))
                                    .foregroundColor(entry.detected ? .green : .secondary.opacity(0.75))
                                    .padding(.horizontal, 5)
                                    .padding(.vertical, 2)
                                    .background((entry.detected ? Color.green : Color.secondary).opacity(0.12))
                                    .cornerRadius(4)

                                Image(systemName: enabled ? "checkmark.square.fill" : "square")
                                    .font(.system(size: 14))
                                    .foregroundColor(enabled ? .accentColor : .secondary)
                            }
                            .padding(.horizontal, 9)
                            .padding(.vertical, 6)
                            .background(Color.primary.opacity(enabled ? 0.075 : 0.03))
                            .cornerRadius(8)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.trailing, 2)
            }
            .frame(maxHeight: 340)

            HStack(spacing: 6) {
                Text("状态栏")
                    .font(.system(size: 11))
                    .foregroundColor(.secondary)
                Spacer()
                ForEach(MenuBarStyle.allCases, id: \.rawValue) { style in
                    Button(style.label) {
                        store.menuBarStyle = style
                    }
                    .buttonStyle(.bordered)
                    .tint(store.menuBarStyle == style ? .accentColor : .secondary)
                    .controlSize(.small)
                }
            }

            Button {
                withAnimation(.easeInOut(duration: 0.16)) {
                    store.showingSettings = false
                }
                store.refresh()
            } label: {
                Text("应用并立即刷新")
                    .font(.system(size: 12, weight: .medium))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 6)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.regular)
        }
    }

    private var collapsedSummaryView: some View {
        let columns = Array(repeating: GridItem(.flexible(), spacing: 6), count: min(4, max(1, store.providers.count)))
        return LazyVGrid(columns: columns, spacing: 6) {
            ForEach(store.providers) { provider in
                let primaryPct = provider.items.compactMap(\.remaining_percent).min()
                VStack(alignment: .leading, spacing: 3) {
                    Text(provider.cli.uppercased())
                        .font(.system(size: 10, weight: .bold, design: .monospaced))
                        .foregroundColor(accentForCLI(provider.id))
                        .lineLimit(1)
                    if let pct = primaryPct {
                        Text("\(Int(pct.rounded()))%")
                            .font(.system(size: 13.5, weight: .semibold, design: .monospaced))
                            .foregroundColor(colorForRemaining(pct))
                    } else if provider.status == "error" {
                        Text("ERR")
                            .font(.system(size: 11.5, weight: .semibold, design: .monospaced))
                            .foregroundColor(.red)
                    } else {
                        Text("--")
                            .font(.system(size: 13, design: .monospaced))
                            .foregroundColor(.secondary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
                .background(Color.primary.opacity(0.06))
                .cornerRadius(8)
            }
        }
    }
}

struct ProviderCardRow: View {
    let provider: ProviderQuota

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                RoundedRectangle(cornerRadius: 2)
                    .fill(accentForCLI(provider.id))
                    .frame(width: 3, height: 13)

                Text(provider.name)
                    .font(.system(size: 12, weight: .semibold))

                Text(provider.cli)
                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                    .foregroundColor(accentForCLI(provider.id))
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1)
                    .background(accentForCLI(provider.id).opacity(0.15))
                    .cornerRadius(4)

                Spacer()

                if let sub = provider.subtitle, !sub.isEmpty {
                    Text(sub)
                        .font(.system(size: 10))
                        .foregroundColor(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
            }

            if provider.status != "ok" {
                Text(provider.error ?? "获取失败")
                    .font(.system(size: 11))
                    .foregroundColor(.red)
            } else if provider.items.isEmpty {
                Text("暂无限额数据")
                    .font(.system(size: 11))
                    .foregroundColor(.secondary)
            } else {
                VStack(spacing: 5) {
                    ForEach(provider.items) { item in
                        QuotaItemRow(item: item)
                    }
                }
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(Color.primary.opacity(0.055))
        .cornerRadius(10)
    }
}

struct QuotaItemRow: View {
    let item: QuotaItem

    var body: some View {
        if let rem = item.remaining_percent {
            VStack(spacing: 3) {
                HStack(spacing: 6) {
                    Text(item.label)
                        .font(.system(size: 11))
                        .foregroundColor(.primary.opacity(0.9))
                        .lineLimit(1)

                    Spacer()

                    if let resetStr = formatReset(item.reset_at) {
                        Text("重置 \(resetStr)")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundColor(.secondary)
                    } else if let detail = item.detail, !detail.isEmpty {
                        Text(detail)
                            .font(.system(size: 10))
                            .foregroundColor(.secondary)
                    }

                    Text(String(format: "%.0f%%", rem))
                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                        .foregroundColor(colorForRemaining(rem))
                        .frame(width: 38, alignment: .trailing)
                }

                GeometryReader { geo in
                    let clamped = max(0, min(100, rem)) / 100.0
                    ZStack(alignment: .leading) {
                        Capsule()
                            .fill(Color.primary.opacity(0.10))
                        Capsule()
                            .fill(colorForRemaining(rem).opacity(0.88))
                            .frame(width: max(3, geo.size.width * clamped))
                    }
                }
                .frame(height: 5)
            }
        } else {
            HStack {
                Text(item.label)
                    .font(.system(size: 10.5))
                    .foregroundColor(.secondary)
                Spacer()
                Text(item.detail ?? "-")
                    .font(.system(size: 10.5, design: .monospaced))
                    .foregroundColor(.secondary)
            }
        }
    }
}

// MARK: - AppDelegate & Floating Panel

final class FloatingPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var panel: FloatingPanel!
    private var statusItem: NSStatusItem!
    private let store = QuotaStore()
    private var cancellables = Set<AnyCancellable>()
    private var lastBroughtToFrontAt: Date = .distantPast

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        setupPanel()
        setupStatusItem()

        store.onPinChanged = { [weak self] pinned in
            self?.applyWindowLevel(pinned: pinned)
        }
        applyWindowLevel(pinned: store.isPinned)

        store.$providers
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in
                self?.updateMenuBarTitle()
                self?.resizePanelToFit()
            }
            .store(in: &cancellables)

        store.$menuBarStyle
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in
                self?.updateMenuBarTitle()
            }
            .store(in: &cancellables)

        store.$isCollapsed
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                    self?.resizePanelToFit()
                }
            }
            .store(in: &cancellables)

        store.$showingSettings
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                    self?.resizePanelToFit()
                }
            }
            .store(in: &cancellables)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        bringPanelToFront()
        return true
    }

    private func setupPanel() {
        let rootView = QuotaCardView(store: store) { [weak self] in
            self?.panel.orderOut(nil)
        }
        let hosting = ClickableHostingView(rootView: rootView)
        hosting.translatesAutoresizingMaskIntoConstraints = false

        let p = FloatingPanel(
            contentRect: NSRect(x: 0, y: 0, width: 356, height: 420),
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = false
        p.hidesOnDeactivate = false
        p.isMovableByWindowBackground = false
        p.becomesKeyOnlyIfNeeded = false
        p.acceptsMouseMovedEvents = true
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        p.contentView = hosting

        if let savedX = UserDefaults.standard.object(forKey: "QuotaCard.posX") as? Double,
           let savedTopY = UserDefaults.standard.object(forKey: "QuotaCard.topY") as? Double {
            p.setFrameTopLeftPoint(NSPoint(x: savedX, y: savedTopY))
        } else if let screen = NSScreen.main?.visibleFrame {
            p.setFrameTopLeftPoint(NSPoint(x: screen.maxX - 376, y: screen.maxY - 18))
        }

        NotificationCenter.default.addObserver(
            forName: NSWindow.didMoveNotification,
            object: p,
            queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let frame = self?.panel.frame else { return }
                UserDefaults.standard.set(Double(frame.minX), forKey: "QuotaCard.posX")
                UserDefaults.standard.set(Double(frame.maxY), forKey: "QuotaCard.topY")
            }
        }

        p.orderFront(nil)
        self.panel = p
        resizePanelToFit()
    }

    private func applyWindowLevel(pinned: Bool) {
        panel.level = pinned ? .floating : .normal
        if panel.isVisible {
            lastBroughtToFrontAt = Date()
            NSApp.activate(ignoringOtherApps: true)
            panel.orderFrontRegardless()
            panel.makeKeyAndOrderFront(nil)
        }
    }

    private func resizePanelToFit() {
        guard let content = panel.contentView else { return }
        content.layoutSubtreeIfNeeded()
        let fitting = content.fittingSize
        guard fitting.height > 40 else { return }
        var frame = panel.frame
        let topY = frame.maxY
        frame.size = NSSize(width: 356, height: fitting.height)
        frame.origin.y = topY - fitting.height
        panel.setFrame(frame, display: true)
    }

    private func setupStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "gauge.with.dots.needle.67percent", accessibilityDescription: "AI 限额看板")
            button.imagePosition = .imageOnly
            button.title = ""
            button.toolTip = "AI 限额看板（点击显隐卡片，右键菜单）"
            button.target = self
            button.action = #selector(onStatusBarClick(_:))
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
    }

    private func updateMenuBarTitle() {
        guard let button = statusItem.button else { return }

        if store.providers.isEmpty {
            button.toolTip = "AI 限额看板（点击显隐卡片，右键菜单）"
        } else {
            let tipLines = store.providers.map { p -> String in
                if let minPct = p.items.compactMap(\.remaining_percent).min() {
                    return "\(p.name): 剩余 \(Int(minPct.rounded()))%"
                } else if let detail = p.items.first?.detail {
                    return "\(p.name): \(detail)"
                } else if p.status == "error" {
                    return "\(p.name): 获取失败"
                }
                return "\(p.name): --"
            }
            button.toolTip = tipLines.joined(separator: "\n")
        }

        switch store.menuBarStyle {
        case .iconOnly:
            button.imagePosition = .imageOnly
            button.title = ""
        case .lowestOnly:
            let ranked = store.providers.compactMap { p -> (String, Double)? in
                guard let m = p.items.compactMap(\.remaining_percent).min() else { return nil }
                return (p.cli, m)
            }.sorted { $0.1 < $1.1 }
            if let lowest = ranked.first {
                button.imagePosition = .imageLeading
                button.title = " \(lowest.0) \(Int(lowest.1.rounded()))%"
            } else {
                button.imagePosition = .imageOnly
                button.title = ""
            }
        case .compactThree:
            let ranked = store.providers.compactMap { p -> (String, Double)? in
                guard let m = p.items.compactMap(\.remaining_percent).min() else { return nil }
                return (p.cli, m)
            }
            if ranked.isEmpty {
                button.imagePosition = .imageOnly
                button.title = ""
            } else {
                let text = ranked.prefix(3).map { "\($0.0) \(Int($0.1.rounded()))%" }.joined(separator: " ")
                button.imagePosition = .imageLeading
                button.title = " " + text
            }
        }
    }

    @objc private func onStatusBarClick(_ sender: Any?) {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp {
            showContextMenu()
        } else {
            togglePanel()
        }
    }

    private func isPanelCovered() -> Bool {
        guard panel.isVisible else { return true }
        if store.isPinned || panel.isKeyWindow || NSApp.isActive {
            return false
        }
        if Date().timeIntervalSince(lastBroughtToFrontAt) < 4.0 {
            return false
        }
        let wid = CGWindowID(panel.windowNumber)
        guard let above = CGWindowListCopyWindowInfo([.optionOnScreenAboveWindow, .excludeDesktopElements], wid) as? [[String: Any]] else {
            return false
        }
        let panelFrame = panel.frame
        guard let screenHeight = NSScreen.screens.first?.frame.height else { return false }
        let panelCGRect = CGRect(
            x: panelFrame.minX,
            y: screenHeight - panelFrame.maxY,
            width: panelFrame.width,
            height: panelFrame.height
        )
        let myPid = ProcessInfo.processInfo.processIdentifier
        for info in above {
            let layer = info[kCGWindowLayer as String] as? Int ?? 0
            if layer != 0 { continue }
            let pid = info[kCGWindowOwnerPID as String] as? Int32 ?? 0
            if pid == myPid { continue }
            let owner = info[kCGWindowOwnerName as String] as? String ?? ""
            if owner == "WindowManager" || owner == "Dock" { continue }
            if let dict = info[kCGWindowBounds as String] as? NSDictionary,
               let rect = CGRect(dictionaryRepresentation: dict),
               rect.width > 40, rect.height > 40,
               rect.intersects(panelCGRect) {
                return true
            }
        }
        return false
    }

    private func bringPanelToFront() {
        resizePanelToFit()
        lastBroughtToFrontAt = Date()
        NSApp.activate(ignoringOtherApps: true)
        panel.orderFrontRegardless()
        panel.makeKeyAndOrderFront(nil)
    }

    private func togglePanel() {
        if panel.isVisible && !isPanelCovered() {
            panel.orderOut(nil)
        } else {
            bringPanelToFront()
        }
    }

    private func showContextMenu() {
        let shouldHide = panel.isVisible && !isPanelCovered()
        let menu = NSMenu()
        menu.addItem(NSMenuItem(
            title: shouldHide ? "隐藏桌面卡片" : "显示桌面卡片",
            action: #selector(menuToggle),
            keyEquivalent: ""
        ))
        menu.addItem(NSMenuItem(title: "选择监控的智能体...", action: #selector(menuOpenSettings), keyEquivalent: ","))
        menu.addItem(NSMenuItem(title: "立即刷新", action: #selector(menuRefresh), keyEquivalent: "r"))

        let intervalMenu = NSMenu()
        for mins in [5, 10, 15, 30, 60] {
            let item = NSMenuItem(title: "每 \(mins) 分钟", action: #selector(menuSetInterval(_:)), keyEquivalent: "")
            item.tag = mins
            item.state = store.refreshMinutes == mins ? .on : .off
            item.target = self
            intervalMenu.addItem(item)
        }
        let intervalParent = NSMenuItem(title: "刷新频率", action: nil, keyEquivalent: "")
        intervalParent.submenu = intervalMenu
        menu.addItem(intervalParent)

        let styleMenu = NSMenu()
        for (idx, style) in MenuBarStyle.allCases.enumerated() {
            let item = NSMenuItem(title: style.label, action: #selector(menuSetBarStyle(_:)), keyEquivalent: "")
            item.tag = idx
            item.state = store.menuBarStyle == style ? .on : .off
            item.target = self
            styleMenu.addItem(item)
        }
        let styleParent = NSMenuItem(title: "状态栏显示", action: nil, keyEquivalent: "")
        styleParent.submenu = styleMenu
        menu.addItem(styleParent)

        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "重置位置到右上角", action: #selector(menuResetPos), keyEquivalent: ""))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "退出 AI 限额看板", action: #selector(menuQuit), keyEquivalent: "q"))

        for item in menu.items where item.action != nil {
            item.target = self
        }
        statusItem.menu = menu
        statusItem.button?.performClick(nil)
        statusItem.menu = nil
    }

    @objc private func menuToggle() { togglePanel() }

    @objc private func menuOpenSettings() {
        store.refreshCatalog()
        store.showingSettings = true
        bringPanelToFront()
    }

    @objc private func menuRefresh() { store.refresh() }

    @objc private func menuSetInterval(_ sender: NSMenuItem) {
        if sender.tag > 0 {
            store.refreshMinutes = sender.tag
        }
    }

    @objc private func menuSetBarStyle(_ sender: NSMenuItem) {
        let styles = MenuBarStyle.allCases
        if sender.tag >= 0 && sender.tag < styles.count {
            store.menuBarStyle = styles[sender.tag]
        }
    }

    @objc private func menuResetPos() {
        if let screen = NSScreen.main?.visibleFrame {
            resizePanelToFit()
            let origin = NSPoint(x: screen.maxX - 376, y: screen.maxY - panel.frame.height - 20)
            panel.setFrameOrigin(origin)
            bringPanelToFront()
        }
    }

    @objc private func menuQuit() {
        NSApp.terminate(nil)
    }
}

// MARK: - Entry

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.run()
}
