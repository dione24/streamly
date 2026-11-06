import AppKit
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    // Closing the window must end the stream: it holds the subscription's only connection.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main
struct StreamlyApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var model: AppModel

    init() {
        // Posters are small but numerous: keep them instead of downloading them again.
        URLCache.shared = URLCache(memoryCapacity: 64 << 20, diskCapacity: 512 << 20)
        #if DEBUG
        _model = State(initialValue: SelfTest.makeModel())
        #else
        _model = State(initialValue: AppModel())
        #endif
    }

    var body: some Scene {
        Window("Streamly", id: "main") {
            RootView()
                .environment(model)
                .preferredColorScheme(.dark)
                .tint(Theme.accent)
                .frame(minWidth: 960, minHeight: 600)
                #if DEBUG
                .task { SelfTest.runIfRequested(model) }
                #endif
        }
        .defaultSize(width: 1280, height: 780)
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandGroup(replacing: .appSettings) {
                Button("Réglages…") { model.showingSettings = true }
                    .keyboardShortcut(",")
            }
            CommandMenu("Lecture") {
                Button("Pause / Reprendre") { model.playback.togglePause() }
                    .keyboardShortcut("p", modifiers: [.command])
                Button("Couper le son") { model.playback.toggleMute() }
                    .keyboardShortcut("m", modifiers: [.command, .shift])
                Divider()
                Button("Chaîne suivante") { model.zap(by: 1) }
                    .keyboardShortcut(.downArrow, modifiers: [.command])
                Button("Chaîne précédente dans la liste") { model.zap(by: -1) }
                    .keyboardShortcut(.upArrow, modifiers: [.command])
                Button("Dernière chaîne regardée") { model.playPrevious() }
                    .keyboardShortcut("b", modifiers: [.command])
                Divider()
                Button("Arrêter la lecture") { model.playback.stop() }
                    .keyboardShortcut(".", modifiers: [.command])
            }
        }
    }
}
