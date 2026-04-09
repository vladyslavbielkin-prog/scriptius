import Foundation
import Network

/// Minimal WebSocket server using Network.framework (macOS 12+).
/// Supports multiple connected clients; broadcasts messages to all.
final class WebSocketServer {
    private var listener: NWListener?
    private let port: UInt16
    private let queue = DispatchQueue(label: "ws-server", qos: .userInitiated)
    private var connections: [NWConnection] = []
    private let lock = NSLock()

    /// Called when a client sends a text message (JSON command).
    var onCommand: (([String: Any]) -> Void)?

    init(port: UInt16 = 9001) {
        self.port = port
    }

    func start() throws {
        let wsOptions = NWProtocolWebSocket.Options()
        wsOptions.autoReplyPing = true

        let params = NWParameters.tcp
        params.defaultProtocolStack.applicationProtocols.insert(wsOptions, at: 0)

        listener = try NWListener(using: params, on: NWEndpoint.Port(rawValue: port)!)
        listener?.newConnectionHandler = { [weak self] conn in
            self?.handleNewConnection(conn)
        }
        listener?.stateUpdateHandler = { state in
            switch state {
            case .ready:
                fputs("[WS] Server listening on ws://localhost:\(self.port)\n", stderr)
            case .failed(let err):
                fputs("[WS] Server failed: \(err)\n", stderr)
            default:
                break
            }
        }
        listener?.start(queue: queue)
    }

    func stop() {
        listener?.cancel()
        listener = nil
        lock.lock()
        let conns = connections
        connections.removeAll()
        lock.unlock()
        for c in conns { c.cancel() }
    }

    /// Send a JSON message to all connected clients.
    func broadcast(_ message: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: message) else { return }
        let metadata = NWProtocolWebSocket.Metadata(opcode: .text)
        let context = NWConnection.ContentContext(identifier: "ws", metadata: [metadata])

        lock.lock()
        let conns = connections
        lock.unlock()

        for conn in conns {
            conn.send(content: data, contentContext: context, isComplete: true, completion: .contentProcessed({ [weak self, weak conn] error in
                if let error = error, let conn = conn {
                    fputs("[WS] Send error, removing client: \(error.localizedDescription)\n", stderr)
                    self?.removeConnection(conn)
                    conn.cancel()
                }
            }))
        }
    }

    /// Send binary data to all connected clients.
    func broadcastBinary(_ data: Data) {
        let metadata = NWProtocolWebSocket.Metadata(opcode: .binary)
        let context = NWConnection.ContentContext(identifier: "ws-bin", metadata: [metadata])

        lock.lock()
        let conns = connections
        lock.unlock()

        for conn in conns {
            conn.send(content: data, contentContext: context, isComplete: true, completion: .contentProcessed({ [weak self, weak conn] error in
                if let error = error, let conn = conn {
                    self?.removeConnection(conn)
                    conn.cancel()
                }
            }))
        }
    }

    // MARK: - Private

    private func handleNewConnection(_ connection: NWConnection) {
        fputs("[WS] Client connected\n", stderr)

        connection.stateUpdateHandler = { [weak self, weak connection] state in
            guard let self = self, let connection = connection else { return }
            switch state {
            case .ready:
                self.receiveLoop(connection)
            case .failed, .cancelled:
                fputs("[WS] Client disconnected\n", stderr)
                self.removeConnection(connection)
            default:
                break
            }
        }

        lock.lock()
        connections.append(connection)
        lock.unlock()

        connection.start(queue: queue)
    }

    private func receiveLoop(_ connection: NWConnection) {
        connection.receiveMessage { [weak self, weak connection] content, context, isComplete, error in
            guard let self = self, let connection = connection else { return }

            if let error = error {
                let nwError = error as NSError
                // POSIX 57 = "Socket is not connected" — normal client disconnect
                if nwError.domain == "NSPOSIXErrorDomain" && nwError.code == 57 {
                    fputs("[WS] Client disconnected (socket closed)\n", stderr)
                } else {
                    fputs("[WS] Receive error: \(error)\n", stderr)
                }
                self.removeConnection(connection)
                connection.cancel()
                return
            }

            if let data = content, !data.isEmpty {
                // Check if it's a WebSocket text message
                if let wsMetadata = context?.protocolMetadata(definition: NWProtocolWebSocket.definition) as? NWProtocolWebSocket.Metadata,
                   wsMetadata.opcode == .text {
                    if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                        self.onCommand?(json)
                    }
                }
            }

            // Continue receiving
            self.receiveLoop(connection)
        }
    }

    private func removeConnection(_ connection: NWConnection) {
        lock.lock()
        connections.removeAll { $0 === connection }
        lock.unlock()
    }
}
