import SwiftUI
import UIKit

@main
struct PendantDirectApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        WindowGroup {
            ContentView().environmentObject(PendantManager.shared)
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        // Create the central manager at launch so iOS can hand back restored
        // Bluetooth state when it relaunches us in the background.
        MainActor.assumeIsolated { _ = PendantManager.shared }
        FeedLog.sink = { line in Task { @MainActor in PendantManager.shared.log(line) } }
        return true
    }

    func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        MainActor.assumeIsolated { PendantManager.shared.uploader.backgroundCompletion = completionHandler }
    }
}
