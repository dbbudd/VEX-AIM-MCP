// AIM Wi-Fi Scan: lists the Wi-Fi networks near this Mac, for the VEX AIM control panel's Wi-Fi menu.
// macOS only tells apps the names of Wi-Fi networks if they may use Location Services, and the panel
// (a Python program) can't be given that permission, so this tiny app asks for it once instead.
// No location is ever read or kept: the permission just unlocks the network names.
//
// Usage: open -W -n -g "AIM Wi-Fi Scan.app" --args --out <file.json> [--check]
//   --check  only say whether it's allowed, without asking or scanning
// Writes JSON: {"permission": "allowed" | "not asked yet" | "denied" | "no answer",
//               "networks": [{"ssid", "rssi", "channel", "band", "open"}], "current": <this Mac's network>}
import CoreLocation
import CoreWLAN
import Foundation

let args = CommandLine.arguments
let outPath = args.firstIndex(of: "--out").flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil } ?? "/dev/stdout"
let checkOnly = args.contains("--check")

func finish(_ result: [String: Any]) -> Never {
    if let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]) {
        FileManager.default.createFile(atPath: outPath, contents: data)
    }
    exit(0)
}

func bandName(_ band: CWChannelBand?) -> String {
    switch band {
    case .band2GHz?: return "2.4 GHz"
    case .band5GHz?: return "5 GHz"
    case .band6GHz?: return "6 GHz"
    default: return "?"
    }
}

func scan() -> Never {
    guard let wifi = CWWiFiClient.shared().interface() else { finish(["permission": "allowed", "error": "This Mac has no Wi-Fi."]) }
    do {
        var networks: [[String: Any]] = []
        for network in try wifi.scanForNetworks(withSSID: nil) {
            guard let ssid = network.ssid, !ssid.isEmpty else { continue }
            networks.append(["ssid": ssid, "rssi": network.rssiValue, "channel": network.wlanChannel?.channelNumber ?? 0,
                             "band": bandName(network.wlanChannel?.channelBand), "open": network.supportsSecurity(.none)])
        }
        finish(["permission": "allowed", "networks": networks, "current": wifi.ssid() ?? ""])
    } catch {
        finish(["permission": "allowed", "error": error.localizedDescription])
    }
}

final class Permission: NSObject, CLLocationManagerDelegate {
    let manager = CLLocationManager()

    override init() {
        super.init()
        manager.delegate = self  // macOS calls back at once with the current permission, and again when it changes
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        switch manager.authorizationStatus {
        case .notDetermined:
            if checkOnly { finish(["permission": "not asked yet"]) }
            manager.requestWhenInUseAuthorization()  // macOS asks the person; the answer comes back here
        case .denied, .restricted:
            finish(["permission": "denied"])
        default:
            if checkOnly { finish(["permission": "allowed"]) }
            scan()
        }
    }
}

let permission = Permission()
DispatchQueue.main.asyncAfter(deadline: .now() + 120) { finish(["permission": "no answer"]) }
RunLoop.main.run()
