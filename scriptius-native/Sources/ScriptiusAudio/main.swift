import Foundation
import AppKit

// Scriptius Audio — Native Audio Capture Agent
// Captures system audio via CoreAudio Tap API (macOS 14.2+)
//
// Two modes:
//   Default (no args):  Chrome Native Messaging (stdin/stdout)
//   --server:           WebSocket server on ws://localhost:9001

if #available(macOS 14.2, *) {
    let serverMode = CommandLine.arguments.contains("--server")
    if serverMode {
        runWebSocketServer()
    } else {
        runNativeMessagingHost()
    }
} else {
    NativeMessaging.send(type: "error", data: ["message": "Requires macOS 14.2 or later"])
    exit(1)
}

// MARK: - WebSocket Server Mode

@available(macOS 14.2, *)
func runWebSocketServer() {
    let tapManager = AudioTapManager()
    let wsServer = WebSocketServer(port: 9001)
    var isCapturing = false

    tapManager.onRMS = { rms in
        wsServer.broadcast(["type": "system_rms", "rms": rms])
    }

    tapManager.onAudioChunk = { pcmData in
        let b64 = pcmData.base64EncodedString()
        wsServer.broadcast(["type": "system_audio", "audio": b64, "samples": pcmData.count / 2])
    }

    wsServer.onCommand = { message in
        guard let command = message["command"] as? String else { return }

        switch command {
        case "start":
            guard !isCapturing else {
                wsServer.broadcast(["type": "error", "message": "Already capturing"])
                return
            }
            do {
                try tapManager.start()
                isCapturing = true
                wsServer.broadcast(["type": "capture_started", "mode": "system_wide"])
                fputs("[Server] Capture started\n", stderr)
            } catch {
                wsServer.broadcast(["type": "error", "message": "\(error)"])
            }

        case "stop":
            tapManager.stop()
            isCapturing = false
            wsServer.broadcast(["type": "capture_stopped"])
            fputs("[Server] Capture stopped\n", stderr)

        case "ping":
            wsServer.broadcast(["type": "pong"])

        default:
            wsServer.broadcast(["type": "error", "message": "Unknown command: \(command)"])
        }
    }

    do {
        try wsServer.start()
    } catch {
        fputs("[Server] Failed to start: \(error)\n", stderr)
        exit(1)
    }

    fputs("[Server] ScriptiusAudio running in WebSocket mode (ws://localhost:9001)\n", stderr)
    fputs("[Server] Press Ctrl+C to stop\n", stderr)

    // Handle SIGINT for clean shutdown
    signal(SIGINT) { _ in
        fputs("\n[Server] Shutting down...\n", stderr)
        exit(0)
    }

    // Keep the process alive
    RunLoop.main.run()
}

// MARK: - Native Messaging Mode (original, for Chrome Extension)

@available(macOS 14.2, *)
func runNativeMessagingHost() {
    let tapManager = AudioTapManager()
    var isCapturing = false

    tapManager.onRMS = { rms in
        NativeMessaging.send(type: "system_rms", data: ["rms": rms])
    }

    tapManager.onAudioChunk = { pcmData in
        let b64 = pcmData.base64EncodedString()
        NativeMessaging.send(type: "system_audio", data: ["audio": b64, "samples": pcmData.count / 2])
    }

    NativeMessaging.send(type: "ready")

    while let message = NativeMessaging.readMessage() {
        guard let command = message["command"] as? String else { continue }

        switch command {
        case "start":
            guard !isCapturing else {
                NativeMessaging.send(type: "error", data: ["message": "Already capturing"])
                continue
            }
            do {
                try tapManager.start()
                isCapturing = true
                NativeMessaging.send(type: "capture_started", data: ["mode": "system_wide"])
            } catch {
                NativeMessaging.send(type: "error", data: ["message": "\(error)"])
            }

        case "stop":
            tapManager.stop()
            isCapturing = false
            NativeMessaging.send(type: "capture_stopped")

        case "ping":
            NativeMessaging.send(type: "pong")

        default:
            NativeMessaging.send(type: "error", data: ["message": "Unknown command: \(command)"])
        }
    }

    tapManager.stop()
    exit(0)
}
