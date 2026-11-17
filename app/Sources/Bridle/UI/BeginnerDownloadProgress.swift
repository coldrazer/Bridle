import SwiftUI

struct BeginnerDownloadProgress: View {
    let repo: String
    let job: DownloadJob?
    let cancel: () -> Void
    let retry: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(title, systemImage: job?.state == "completed" ? "checkmark.circle" : "arrow.down.circle")
                .font(.headline)
            if job?.isActive == true || job == nil {
                if let job, job.totalBytes > 0 {
                    ProgressView(value: min(1, max(0, job.fraction)))
                    Text("\(Format.bytes(job.doneBytes)) of \(Format.bytes(job.totalBytes)) · \(Format.percent(job.fraction))")
                        .font(.callout).monospacedDigit().foregroundStyle(.secondary)
                } else { ProgressView().controlSize(.small) }
                Text("Keep Bridle open while this downloads. You can cancel and retry later.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if job?.state == "failed" {
                Text("The download didn’t finish. Check your internet connection, then try again.")
                    .font(.callout).foregroundStyle(.secondary)
                DisclosureGroup("Show download details") {
                    Text(job?.error ?? "").font(.caption).textSelection(.enabled)
                }
            }
            if job?.state == "cancelled" {
                Text("Your partial download is saved. Retry when you’re ready.").foregroundStyle(.secondary)
            }
            HStack {
                if job?.isActive == true { Button("Cancel Download", action: cancel) }
                if ["failed", "cancelled"].contains(job?.state ?? "") { Button("Retry Download", action: retry) }
            }
        }
        .padding(16).frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 12))
        .accessibilityElement(children: .contain)
    }

    private var title: String {
        switch job?.state {
        case "completed": return "Download complete — preparing your assistant"
        case "cancelled": return "Download paused"
        case "failed": return "Download needs attention"
        case "verifying": return "Checking your download"
        case "queued": return "Your download is in line"
        default: return "Downloading your assistant"
        }
    }
}
