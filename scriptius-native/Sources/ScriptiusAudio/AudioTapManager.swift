import Foundation
import CoreAudio
import AudioToolbox

@available(macOS 14.2, *)
final class AudioTapManager {

    private var tapObjectID: AudioObjectID = kAudioObjectUnknown
    private var aggregateDeviceID: AudioDeviceID = kAudioObjectUnknown
    private var ioProcID: AudioDeviceIOProcID? = nil
    private var isRunning = false
    private var ioProcFired = false

    private let rmsLock = NSLock()
    private var sampleAccumulator: Float64 = 0
    private var sampleCount: Int = 0
    private var rmsTimer: DispatchSourceTimer?

    // PCM buffer for streaming raw audio
    private let pcmLock = NSLock()
    private var pcmBuffer: [Int16] = []
    private var deviceSampleRate: Float64 = 48000
    private var rmsTickCounter: Int = 0

    var onRMS: ((Double) -> Void)?
    var onAudioChunk: ((Data) -> Void)?

    func start() throws {
        // 1. Get all CoreAudio process object IDs (NOT PIDs).
        let processObjectIDs = allAudioProcessObjectIDs()
        fputs("[ScriptiusAudio] process object count: \(processObjectIDs.count)\n", stderr)

        let tapDesc = CATapDescription(stereoMixdownOfProcesses: processObjectIDs)
        tapDesc.name = "ScriptiusAudioTap"
        tapDesc.muteBehavior = .unmuted

        var tapID: AudioObjectID = kAudioObjectUnknown
        let tapStatus = AudioHardwareCreateProcessTap(tapDesc, &tapID)
        guard tapStatus == noErr else {
            fputs("[ScriptiusAudio] tap creation failed: \(tapStatus)\n", stderr)
            throw AudioTapError.tapCreationFailed(tapStatus)
        }
        self.tapObjectID = tapID
        fputs("[ScriptiusAudio] tap created, tapID=\(tapID)\n", stderr)

        // 2. Create aggregate device: tap (audio source) + output device (clock)
        let tapUID = tapDesc.uuid.uuidString
        let taps: [[String: Any]] = [[
            kAudioSubTapUIDKey as String: tapUID,
            kAudioSubTapDriftCompensationKey as String: true
        ]]

        var aggregateDesc: [String: Any] = [
            kAudioAggregateDeviceNameKey as String: "ScriptiusAggregate",
            kAudioAggregateDeviceUIDKey as String: "com.scriptius.aggregate.\(UUID().uuidString)",
            kAudioAggregateDeviceIsPrivateKey as String: true,
            kAudioAggregateDeviceTapListKey as String: taps,
            kAudioAggregateDeviceTapAutoStartKey as String: true
        ]
        if let outputUID = defaultOutputDeviceUID() {
            aggregateDesc[kAudioAggregateDeviceSubDeviceListKey as String] = [
                [kAudioSubDeviceUIDKey as String: outputUID]
            ]
            // Explicitly name the clock master to prevent CoreAudio from
            // picking an arbitrary sub-device, which can cause timing drift.
            aggregateDesc[kAudioAggregateDeviceMainSubDeviceKey as String] = outputUID
            fputs("[ScriptiusAudio] using output device as clock: \(outputUID)\n", stderr)
        }

        var aggDeviceID: AudioDeviceID = kAudioObjectUnknown
        let aggStatus = AudioHardwareCreateAggregateDevice(aggregateDesc as CFDictionary, &aggDeviceID)
        guard aggStatus == noErr else {
            AudioHardwareDestroyProcessTap(tapID)
            fputs("[ScriptiusAudio] aggregate device creation failed: \(aggStatus)\n", stderr)
            throw AudioTapError.aggregateDeviceFailed(aggStatus)
        }
        self.aggregateDeviceID = aggDeviceID
        fputs("[ScriptiusAudio] aggregate device created, aggDeviceID=\(aggDeviceID)\n", stderr)

        // Query the actual sample rate of the aggregate device
        var nominalRate: Float64 = 48000
        var rateSize = UInt32(MemoryLayout<Float64>.size)
        var rateAddr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyNominalSampleRate,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        if AudioObjectGetPropertyData(aggDeviceID, &rateAddr, 0, nil, &rateSize, &nominalRate) == noErr {
            self.deviceSampleRate = nominalRate
        }
        fputs("[ScriptiusAudio] device sample rate: \(self.deviceSampleRate) Hz\n", stderr)

        // 3. Register IO proc directly on the aggregate device.
        //    inInputData delivers the tap audio on every IO cycle — bypasses
        //    AVAudioEngine/AUHAL which cannot expose tap channels when the
        //    sub-device is output-only (no hardware microphone input).
        var procID: AudioDeviceIOProcID? = nil
        let blockStatus = AudioDeviceCreateIOProcIDWithBlock(&procID, aggDeviceID, nil) {
            [weak self] (inNow, inInputData, inInputTime, outOutputData, inOutputTime) in
            guard let self = self else { return }

            // One-shot diagnostic: confirm the IO proc fires and how many buffers arrive.
            if !self.ioProcFired {
                self.ioProcFired = true
                let count = UnsafeMutableAudioBufferListPointer(
                    UnsafeMutablePointer(mutating: inInputData)).count
                fputs("[ScriptiusAudio] IO proc fired, buffer count: \(count)\n", stderr)
            }

            let ablPointer = UnsafeMutableAudioBufferListPointer(
                UnsafeMutablePointer(mutating: inInputData))
            var sum: Float64 = 0
            var samples = 0
            for audioBuffer in ablPointer {
                guard let mData = audioBuffer.mData,
                      audioBuffer.mNumberChannels > 0,
                      audioBuffer.mDataByteSize > 0 else { continue }
                let ch = Int(audioBuffer.mNumberChannels)
                let frameCount = Int(audioBuffer.mDataByteSize) / (MemoryLayout<Float32>.size * ch)
                guard frameCount > 0 else { continue }
                let ptr = mData.bindMemory(to: Float32.self, capacity: frameCount * ch)
                for i in 0..<(frameCount * ch) {
                    let s = Float64(ptr[i])
                    sum += s * s
                }
                samples += frameCount * ch
            }
            if samples > 0 {
                self.rmsLock.lock()
                self.sampleAccumulator += sum
                self.sampleCount += samples
                self.rmsLock.unlock()
            }

            // ── Downsample to 16kHz mono Int16 for streaming ──────────
            let ratio = Int(max(1, self.deviceSampleRate / 16000))
            var pcmSamples: [Int16] = []
            for audioBuffer in ablPointer {
                guard let mData = audioBuffer.mData,
                      audioBuffer.mNumberChannels > 0,
                      audioBuffer.mDataByteSize > 0 else { continue }
                let ch = Int(audioBuffer.mNumberChannels)
                let frameCount = Int(audioBuffer.mDataByteSize) / (MemoryLayout<Float32>.size * ch)
                guard frameCount > 0 else { continue }
                let ptr = mData.bindMemory(to: Float32.self, capacity: frameCount * ch)
                // Take every Nth frame, average channels to mono
                var frameIdx = 0
                while frameIdx < frameCount {
                    var mono: Float32 = 0
                    for c in 0..<ch {
                        mono += ptr[frameIdx * ch + c]
                    }
                    mono /= Float32(ch)
                    // Clamp and convert to Int16
                    let clamped = max(-1.0, min(1.0, mono))
                    let sample = Int16(clamped * 32767)
                    pcmSamples.append(sample)
                    frameIdx += ratio
                }
            }
            if !pcmSamples.isEmpty {
                self.pcmLock.lock()
                self.pcmBuffer.append(contentsOf: pcmSamples)
                self.pcmLock.unlock()
            }
        }
        guard blockStatus == noErr, let procID = procID else {
            cleanupResources(tapID: tapID, aggDeviceID: aggDeviceID)
            fputs("[ScriptiusAudio] AudioDeviceCreateIOProcIDWithBlock failed: \(blockStatus)\n", stderr)
            throw AudioTapError.startFailed(blockStatus)
        }
        self.ioProcID = procID

        let startStatus = AudioDeviceStart(aggDeviceID, procID)
        guard startStatus == noErr else {
            AudioDeviceDestroyIOProcID(aggDeviceID, procID)
            self.ioProcID = nil
            cleanupResources(tapID: tapID, aggDeviceID: aggDeviceID)
            fputs("[ScriptiusAudio] AudioDeviceStart failed: \(startStatus)\n", stderr)
            throw AudioTapError.startFailed(startStatus)
        }
        fputs("[ScriptiusAudio] AudioDeviceStart OK, aggDeviceID=\(aggDeviceID)\n", stderr)
        self.isRunning = true

        // 4. Timer: send audio chunks every 100ms, RMS every 500ms (every 5th tick)
        let timer = DispatchSource.makeTimerSource(queue: .global(qos: .userInteractive))
        timer.schedule(deadline: .now() + .milliseconds(100), repeating: .milliseconds(100))
        timer.setEventHandler { [weak self] in
            guard let self = self else { return }
            // Always drain and send PCM audio
            self.drainAndSendPCM()
            // Report RMS every 5th tick (500ms)
            self.rmsTickCounter += 1
            if self.rmsTickCounter >= 5 {
                self.rmsTickCounter = 0
                self.reportRMS()
            }
        }
        timer.resume()
        self.rmsTimer = timer
    }

    func stop() {
        rmsTimer?.cancel()
        rmsTimer = nil
        if let procID = ioProcID {
            AudioDeviceStop(aggregateDeviceID, procID)
            AudioDeviceDestroyIOProcID(aggregateDeviceID, procID)
            ioProcID = nil
        }
        ioProcFired = false
        cleanupResources(tapID: tapObjectID, aggDeviceID: aggregateDeviceID)
        tapObjectID = kAudioObjectUnknown
        aggregateDeviceID = kAudioObjectUnknown
        isRunning = false
    }

    // MARK: - Helpers

    /// Returns all CoreAudio process object IDs registered on the system.
    /// These are NOT Unix PIDs — they are AudioObjectIDs obtained from
    /// kAudioHardwarePropertyProcessObjectList (available macOS 14.2+).
    private func allAudioProcessObjectIDs() -> [AudioObjectID] {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyProcessObjectList,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(
            AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size
        ) == noErr, size > 0 else { return [] }

        let count = Int(size) / MemoryLayout<AudioObjectID>.size
        var ids = [AudioObjectID](repeating: 0, count: count)
        guard AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids
        ) == noErr else { return [] }
        return ids
    }

    private func defaultOutputDeviceUID() -> String? {
        var deviceID = AudioDeviceID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        guard AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &deviceID
        ) == noErr, deviceID != kAudioObjectUnknown else { return nil }

        var uid: Unmanaged<CFString>? = nil
        var uidSize = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        var uidAddr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceUID,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        guard AudioObjectGetPropertyData(
            deviceID, &uidAddr, 0, nil, &uidSize, &uid
        ) == noErr, let uid = uid else { return nil }
        return uid.takeRetainedValue() as String
    }

    private func cleanupResources(tapID: AudioObjectID, aggDeviceID: AudioDeviceID) {
        if aggDeviceID != kAudioObjectUnknown {
            AudioHardwareDestroyAggregateDevice(aggDeviceID)
        }
        if tapID != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tapID)
        }
    }

    private func drainAndSendPCM() {
        pcmLock.lock()
        let samples = pcmBuffer
        pcmBuffer.removeAll(keepingCapacity: true)
        pcmLock.unlock()

        guard !samples.isEmpty else { return }

        // Convert [Int16] → Data (little-endian, native on x86/ARM)
        let data = samples.withUnsafeBufferPointer { ptr in
            Data(buffer: ptr)
        }
        onAudioChunk?(data)
    }

    private func reportRMS() {
        rmsLock.lock()
        let acc = sampleAccumulator
        let count = sampleCount
        sampleAccumulator = 0
        sampleCount = 0
        rmsLock.unlock()
        let rms = count > 0 ? sqrt(acc / Double(count)) : 0.0
        onRMS?(rms)
    }

    deinit {
        if isRunning { stop() }
    }
}

enum AudioTapError: Error, CustomStringConvertible {
    case tapCreationFailed(OSStatus)
    case aggregateDeviceFailed(OSStatus)
    case startFailed(OSStatus)

    var description: String {
        switch self {
        case .tapCreationFailed(let s):   return "Failed to create process tap (OSStatus \(s))"
        case .aggregateDeviceFailed(let s): return "Failed to create aggregate device (OSStatus \(s))"
        case .startFailed(let s):         return "Failed to start audio capture (OSStatus \(s))"
        }
    }
}
