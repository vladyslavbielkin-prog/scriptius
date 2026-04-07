import Foundation

/// Chrome Native Messaging protocol: 4-byte little-endian length prefix + JSON payload
enum NativeMessaging {

    /// Read one message from stdin. Returns nil on EOF.
    static func readMessage() -> [String: Any]? {
        // Read 4-byte length prefix (little-endian UInt32)
        var lengthBytes = [UInt8](repeating: 0, count: 4)
        let bytesRead = fread(&lengthBytes, 1, 4, stdin)
        guard bytesRead == 4 else { return nil } // EOF or error

        let length = UInt32(lengthBytes[0])
            | (UInt32(lengthBytes[1]) << 8)
            | (UInt32(lengthBytes[2]) << 16)
            | (UInt32(lengthBytes[3]) << 24)

        guard length > 0, length < 1_048_576 else { return nil } // max 1MB

        // Read JSON payload
        var jsonBytes = [UInt8](repeating: 0, count: Int(length))
        let jsonRead = fread(&jsonBytes, 1, Int(length), stdin)
        guard jsonRead == Int(length) else { return nil }

        let data = Data(jsonBytes)
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        return json
    }

    /// Write one message to stdout.
    static func writeMessage(_ message: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: message) else { return }

        let length = UInt32(data.count)
        var lengthBytes: [UInt8] = [
            UInt8(length & 0xFF),
            UInt8((length >> 8) & 0xFF),
            UInt8((length >> 16) & 0xFF),
            UInt8((length >> 24) & 0xFF)
        ]

        fwrite(&lengthBytes, 1, 4, stdout)
        _ = data.withUnsafeBytes { ptr in
            fwrite(ptr.baseAddress, 1, data.count, stdout)
        }
        fflush(stdout)
    }

    /// Send a typed message (convenience).
    static func send(type: String, data: [String: Any] = [:]) {
        var msg = data
        msg["type"] = type
        writeMessage(msg)
    }
}
