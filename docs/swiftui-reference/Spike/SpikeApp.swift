import SwiftUI
import UserNotifications

/// Screen-id navigation contract (iosforge providers/base.py SCREEN_NAV_CONTRACT).
enum ScreenID: String, CaseIterable {
    case onboarding, home, detail, settings, paywall
}

@MainActor
final class Router: ObservableObject {
    @Published var path: [ScreenID] = []
    @Published var sheet: ScreenID?
    @Published var onboardingDone = false
    @Published var unknownID: String?
    /// true when launched through the contract: skip onboarding + system prompts.
    @Published var headless = false

    /// Deep-link straight to `id`, rebuilding the navigation stack with fixtures.
    func open(_ raw: String) {
        headless = true
        onboardingDone = true
        sheet = nil
        path = []
        unknownID = nil
        guard let id = ScreenID(rawValue: raw) else { unknownID = raw; return }
        switch id {
        case .onboarding: onboardingDone = false
        case .home: break
        case .detail: path = [.detail]
        case .settings: path = [.settings]
        case .paywall: sheet = .paywall
        }
    }

    static func screenID(from url: URL) -> String? {
        guard url.scheme == "iosforge", url.host == "screen" else { return nil }
        return url.pathComponents.dropFirst().first
    }
}

@main
struct SpikeApp: App {
    @StateObject private var router = Router()

    init() {
        // Launch argument: `-screen-id <id>` lands in the UserDefaults argument domain.
        if let raw = UserDefaults.standard.string(forKey: "screen-id") {
            let r = Router(); r.open(raw); _router = StateObject(wrappedValue: r)
        }
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(router)
                .onOpenURL { url in
                    if let raw = Router.screenID(from: url) { router.open(raw) }
                }
        }
    }
}

struct RootView: View {
    @EnvironmentObject var router: Router

    var body: some View {
        Group {
            if let bad = router.unknownID {
                Text("UNKNOWN SCREEN-ID: \(bad)").font(.title).foregroundStyle(.red)
            } else if !router.onboardingDone {
                OnboardingView()
            } else {
                NavigationStack(path: $router.path) {
                    HomeView()
                        .navigationDestination(for: ScreenID.self) { id in
                            switch id {
                            case .detail: DetailView(item: Fixtures.item)
                            case .settings: SettingsView()
                            default: EmptyView()
                            }
                        }
                }
                .sheet(item: $router.sheet) { _ in PaywallView() }
            }
        }
        .task {
            // Normal launch asks for notifications; the contract must suppress it.
            guard !router.headless else { return }
            _ = try? await UNUserNotificationCenter.current()
                .requestAuthorization(options: [.alert, .badge])
        }
    }
}

extension ScreenID: Identifiable { var id: String { rawValue } }

enum Fixtures {
    static let item = Item(title: "Focus session", minutes: 45)
}
struct Item { let title: String; let minutes: Int }

struct OnboardingView: View {
    @EnvironmentObject var router: Router
    var body: some View {
        VStack(spacing: 24) {
            Text("Welcome").font(.largeTitle.bold())
            Button("Continue") { router.onboardingDone = true }.buttonStyle(.borderedProminent)
        }
    }
}

struct HomeView: View {
    var body: some View {
        List {
            NavigationLink("Open detail", value: ScreenID.detail)
            NavigationLink("Settings", value: ScreenID.settings)
        }
        .navigationTitle("Home")
    }
}

struct DetailView: View {
    let item: Item
    var body: some View {
        VStack(spacing: 12) {
            Text(item.title).font(.title)
            Text("\(item.minutes) min").foregroundStyle(.secondary)
        }
        .navigationTitle("Detail")
    }
}

struct SettingsView: View {
    var body: some View {
        Form { Toggle("Block apps", isOn: .constant(true)) }.navigationTitle("Settings")
    }
}

struct PaywallView: View {
    var body: some View {
        VStack(spacing: 16) {
            Text("Go Premium").font(.largeTitle.bold())
            Text("Unlock everything")
        }
        .presentationDetents([.large])
    }
}
