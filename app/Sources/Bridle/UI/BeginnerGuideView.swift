import SwiftUI

struct BeginnerGuideView: View {
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("A quick guide to Bridle").font(.title.bold())
            guide("Choose what you need", symbol: "sparkles",
                  text: "Everyday answers questions. Writing helps with drafts. Coding explains and writes code. Photo can discuss an image you attach.")
            guide("Download once", symbol: "arrow.down.circle",
                  text: "Setup and new assistants need internet access and space on your Mac. Keep Bridle open until the download finishes. You can retry interrupted downloads.")
            guide("Chat on your Mac", symbol: "lock.shield",
                  text: "Once your assistant is ready, your conversation runs locally, including when you’re offline. Bridle can also serve other apps if you enable sharing in Settings.")
            guide("Make it a conversation", symbol: "bubble.left.and.bubble.right",
                  text: "Tell the assistant what you need and add context. Ask it to make an answer shorter, explain a step, or try another approach. Check important answers yourself.")
            guide("Try a photo", symbol: "photo",
                  text: "Choose Photo, then use the plus button beside your message to attach an image. Downloading Photo support may take an extra setup step.")
            guide("Explore when you’re ready", symbol: "slider.horizontal.3",
                  text: "Advanced Workspace has the full model browser, sharing controls, performance tools, and developer options. You can return to Beginner mode at any time.")
            HStack { Spacer(); Button("Got It") { dismiss() }.keyboardShortcut(.defaultAction) }
        }.padding(28).frame(width: 540)
    }

    private func guide(_ title: String, symbol: String, text: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: symbol).font(.title2).frame(width: 28).foregroundStyle(.tint)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.headline)
                Text(text).font(.callout).foregroundStyle(.secondary)
            }
        }
    }
}
