import Foundation

enum Format {
    /// Decimal gigabytes, like the screenshots: "48 GB", "7.4 GB", "512 GB", "1 TB".
    static func bytes(_ value: Int64) -> String {
        let v = Double(value)
        if v >= 1e12 {
            return trim(v / 1e12) + " TB"
        }
        if v >= 1e9 {
            return trim(v / 1e9) + " GB"
        }
        if v >= 1e6 {
            return String(format: "%.0f MB", v / 1e6)
        }
        return String(format: "%.0f KB", v / 1e3)
    }

    /// Memory sizes use binary units so "16 GB" machines read as 16, not 17.2.
    static func memory(_ value: Int64) -> String {
        let gib = Double(value) / 1_073_741_824
        return trim(gib) + " GB"
    }

    static func duration(_ seconds: Int) -> String {
        let h = seconds / 3600, m = (seconds % 3600) / 60, s = seconds % 60
        return String(format: "%02d:%02d:%02d", h, m, s)
    }

    static func rate(_ bytesPerSecond: Double) -> String {
        bytesPerSecond > 0 ? String(format: "%.1f MB/s", bytesPerSecond / 1e6) : ""
    }

    static func percent(_ fraction: Double) -> String {
        String(format: "%.0f%%", max(0, min(1, fraction)) * 100)
    }

    static func shortName(_ repo: String) -> String {
        repo.split(separator: "/", maxSplits: 1).last.map(String.init) ?? repo
    }

    private static func trim(_ v: Double) -> String {
        if v >= 100 || abs(v.rounded() - v) < 0.05 {
            return String(format: "%.0f", v.rounded())
        }
        return String(format: "%.1f", v)
    }
}
