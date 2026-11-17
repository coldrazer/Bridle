import SwiftUI

struct DownloadSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        let repo = model.focusedDownloadRepo ?? ""
        let job = model.download(for: repo)
        VStack(spacing: 16) {
            Text(title(job))
                .font(.title2.weight(.bold))
            Text(Format.shortName(repo))
                .foregroundStyle(.secondary)

            VStack(spacing: 6) {
                HStack {
                    Text(job.map { "\(Format.bytes($0.doneBytes)) of \(Format.bytes($0.totalBytes))" } ?? "Resolving files…")
                        .foregroundStyle(.secondary)
                    Spacer()
                    Text(job.map { "\(Format.percent($0.fraction)) Complete" } ?? "")
                        .fontWeight(.semibold)
                }
                .font(.callout)
                ProgressView(value: job?.fraction ?? 0)
                Text(detail(job))
                    .font(.callout)
                    .foregroundStyle(job?.state == "failed" ? Color.red : Color.secondary)
                    .multilineTextAlignment(.center)
                    .textSelection(.enabled)
            }

            Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 6) {
                GridRow {
                    Text("Source").foregroundStyle(.secondary)
                    Text(repo).textSelection(.enabled)
                }
                GridRow {
                    Text("Location").foregroundStyle(.secondary)
                    Text(job?.destination ?? EngineLocator.modelsDirectory.path)
                        .lineLimit(2)
                        .truncationMode(.middle)
                        .textSelection(.enabled)
                }
            }
            .font(.caption)
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))

            HStack {
                if let job, job.isActive {
                    Button("Cancel") { model.cancelDownload(job) }
                } else if job?.state == "failed" || job?.state == "cancelled" {
                    Button("Retry") { model.startDownload(repo) }
                }
                Spacer()
                if job?.state == "completed" {
                    Button("Load Model") {
                        model.choose(repo)
                        dismiss()
                    }
                    .keyboardShortcut(.defaultAction)
                }
                Button(job?.isActive == true ? "Hide" : "Close") { dismiss() }
            }
        }
        .padding(28)
        .frame(width: 460)
    }

    private func title(_ job: DownloadJob?) -> String {
        switch job?.state {
        case "completed": return "Download Complete"
        case "failed": return "Download Failed"
        case "cancelled": return "Download Cancelled"
        case "verifying": return "Verifying Model"
        default: return "Downloading Model"
        }
    }

    private func detail(_ job: DownloadJob?) -> String {
        guard let job else { return "Contacting Hugging Face…" }
        switch job.state {
        case "queued": return "Waiting for another download to finish…"
        case "resolving": return "Listing files on Hugging Face…"
        case "downloading":
            let rate = Format.rate(job.bytesPerSecond)
            return "Downloading \(job.currentFile)" + (rate.isEmpty ? "" : " · \(rate)")
        case "verifying": return "Checking file sizes and SHA-256 hashes…"
        case "completed": return "Saved and verified."
        case "cancelled": return "Partial files are kept, so a retry resumes where it stopped."
        default: return job.error
        }
    }
}
