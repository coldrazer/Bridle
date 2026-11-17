import Foundation
#if canImport(Sparkle)
import Sparkle
#endif

/// Automatic updates via Sparkle. Active only in release builds whose Info.plist carries an
/// SUFeedURL and SUPublicEDKey (scripts/release.sh sets both); development builds hide it.
final class Updater: NSObject, ObservableObject {
    static let shared = Updater()

    #if canImport(Sparkle)
    private var controller: SPUStandardUpdaterController?
    #endif

    var isAvailable: Bool {
        #if canImport(Sparkle)
        return controller != nil
        #else
        return false
        #endif
    }

    var automaticallyChecks: Bool {
        get {
            #if canImport(Sparkle)
            return controller?.updater.automaticallyChecksForUpdates ?? false
            #else
            return false
            #endif
        }
        set {
            #if canImport(Sparkle)
            controller?.updater.automaticallyChecksForUpdates = newValue
            #endif
        }
    }

    override init() {
        super.init()
        #if canImport(Sparkle)
        let info = Bundle.main.infoDictionary ?? [:]
        if let feed = info["SUFeedURL"] as? String, !feed.isEmpty,
           let key = info["SUPublicEDKey"] as? String, !key.isEmpty {
            controller = SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: nil, userDriverDelegate: nil)
        }
        #endif
    }

    func checkForUpdates() {
        #if canImport(Sparkle)
        controller?.checkForUpdates(nil)
        #endif
    }
}
