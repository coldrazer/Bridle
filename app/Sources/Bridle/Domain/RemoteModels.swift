import Foundation

/// Typed views over the engine's /control/remote/* JSON (dictionaries at the
/// boundary, mirroring the rest of the Studio code).
struct RemoteConnectionInfo: Identifiable, Equatable {
    let id: String
    let name: String
    let detail: String
    let fingerprint: String
    let user: String

    var trusted: Bool { !fingerprint.isEmpty }
    var summary: String {
        trusted ? "Trusted · \(user.isEmpty ? "connected" : "as \(user)")" : "Host key not trusted yet"
    }

    init(_ dict: [String: Any]) {
        id = dict["id"] as? String ?? ""
        name = dict["name"] as? String ?? "Connection"
        let alias = dict["host_alias"] as? String ?? ""
        let host = dict["hostname"] as? String ?? ""
        detail = dict["effective_destination"] as? String ?? (alias.isEmpty ? host : alias)
        fingerprint = dict["approved_host_key_fingerprint"] as? String ?? ""
        user = (dict["identity"] as? [String: Any])?["user"] as? String ?? ""
    }
}

struct RemoteWorkspaceInfo: Identifiable, Equatable {
    let id: String
    let connectionID: String
    let root: String
    let displayName: String
    let capabilities: [String]
    let user: String

    var accessLabel: String { capabilities.contains("write") ? "Read and write" : "Read only" }
    var label: String { "Remote: \(displayName) · \(root)" }

    init(_ dict: [String: Any]) {
        id = dict["id"] as? String ?? ""
        connectionID = dict["connection_id"] as? String ?? ""
        root = dict["canonical_root"] as? String ?? ""
        displayName = dict["display_name"] as? String ?? "Workspace"
        capabilities = dict["capabilities"] as? [String] ?? ["read"]
        user = dict["verified_user_identity"] as? String ?? ""
    }
}

struct RemoteFolderEntry: Identifiable, Equatable {
    let name: String
    let directory: Bool
    let symlink: Bool
    let size: Int

    var id: String { name }
    var icon: String { symlink ? "link" : (directory ? "folder" : "doc") }

    init(_ dict: [String: Any]) {
        name = dict["name"] as? String ?? ""
        directory = dict["directory"] as? Bool ?? false
        symlink = dict["symlink"] as? Bool ?? false
        size = dict["size"] as? Int ?? 0
    }
}
