import Foundation

/// Thin client for /control/remote/* on top of AppModel's control transport.
struct RemoteClient {
    let request: (String, String, [String: Any]?) async throws -> [String: Any]

    init(model: AppModel) {
        request = { method, path, body in try await model.featureRequest(method, path, body: body) }
    }

    private static func query(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: .alphanumerics.union(CharacterSet(charactersIn: "-._~"))) ?? value
    }

    func connections() async throws -> [RemoteConnectionInfo] {
        let response = try await request("GET", "/control/remote/connections", nil)
        return (response["connections"] as? [[String: Any]] ?? []).map(RemoteConnectionInfo.init)
    }

    func createConnection(_ body: [String: Any]) async throws -> RemoteConnectionInfo {
        RemoteConnectionInfo(try await request("POST", "/control/remote/connections", body))
    }

    func test(connectionID: String) async throws -> [String: Any] {
        try await request("POST", "/control/remote/connections/\(connectionID)/test", [:])
    }

    func trust(connectionID: String, attemptID: String, fingerprint: String) async throws -> [String: Any] {
        try await request("POST", "/control/remote/connections/\(connectionID)/trust",
                          ["attempt_id": attemptID, "fingerprint": fingerprint])
    }

    func disconnect(connectionID: String) async throws {
        _ = try await request("POST", "/control/remote/connections/\(connectionID)/disconnect", [:])
    }

    func forget(connectionID: String) async throws {
        _ = try await request("DELETE", "/control/remote/connections/\(connectionID)", nil)
    }

    func browse(connectionID: String, path: String?, offset: Int = 0, showHidden: Bool = false) async throws
        -> (path: String, parent: String?, entries: [RemoteFolderEntry], hasMore: Bool, total: Int) {
        var route = "/control/remote/connections/\(connectionID)/folders?offset=\(offset)&show_hidden=\(showHidden)"
        if let path { route += "&path=\(Self.query(path))" }
        let response = try await request("GET", route, nil)
        return (response["path"] as? String ?? "", response["parent"] as? String,
                (response["entries"] as? [[String: Any]] ?? []).map(RemoteFolderEntry.init),
                response["has_more"] as? Bool ?? false, response["total"] as? Int ?? 0)
    }

    func workspaces() async throws -> [RemoteWorkspaceInfo] {
        let response = try await request("GET", "/control/remote/workspaces", nil)
        return (response["workspaces"] as? [[String: Any]] ?? []).map(RemoteWorkspaceInfo.init)
    }

    func createWorkspace(connectionID: String, path: String, capabilities: [String]) async throws -> RemoteWorkspaceInfo {
        RemoteWorkspaceInfo(try await request("POST", "/control/remote/workspaces",
                                              ["connection_id": connectionID, "path": path,
                                               "capabilities": capabilities]))
    }

    func deleteWorkspace(id: String) async throws {
        _ = try await request("DELETE", "/control/remote/workspaces/\(id)", nil)
    }

    func readFile(workspaceID: String, path: String) async throws -> [String: Any] {
        try await request("POST", "/control/remote/workspaces/\(workspaceID)/read", ["path": path])
    }

    func previewWrite(workspaceID: String, path: String, content: String, taskID: String = "") async throws -> [String: Any] {
        var body: [String: Any] = ["path": path, "content": content]
        if !taskID.isEmpty { body["task_id"] = taskID }
        return try await request("POST", "/control/remote/workspaces/\(workspaceID)/writes/preview", body)
    }

    func applyWrite(workspaceID: String, approval: String, content: String) async throws -> [String: Any] {
        try await request("POST", "/control/remote/workspaces/\(workspaceID)/writes/apply",
                          ["approval": approval, "content": content])
    }
}
