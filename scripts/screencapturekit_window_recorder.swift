import AppKit
import AVFoundation
import CoreGraphics
import CoreMedia
import Darwin
import Foundation
import ScreenCaptureKit

enum RecorderError: Error, CustomStringConvertible {
    case invalidArguments(String)
    case windowNotFound(UInt32)
    case recordingFailed(String)
    case finishTimedOut

    var description: String {
        switch self {
        case .invalidArguments(let message):
            return message
        case .windowNotFound(let windowID):
            return "找不到窗口 ID: \(windowID)"
        case .recordingFailed(let message):
            return message
        case .finishTimedOut:
            return "等待录制文件完成超时"
        }
    }
}

final class RecordingOutputDelegate: NSObject, SCRecordingOutputDelegate {
    let finished = DispatchSemaphore(value: 0)
    private let lock = NSLock()
    private var failure: Error?

    func recordingOutputDidStartRecording(_ recordingOutput: SCRecordingOutput) {
        // The Python caller waits for the ready file written after startCapture().
    }

    func recordingOutputDidFinishRecording(_ recordingOutput: SCRecordingOutput) {
        finished.signal()
    }

    func recordingOutput(_ recordingOutput: SCRecordingOutput, didFailWithError error: Error) {
        lock.lock()
        failure = error
        lock.unlock()
        finished.signal()
    }

    func recordedFailure() -> Error? {
        lock.lock()
        defer { lock.unlock() }
        return failure
    }
}

final class StreamDelegate: NSObject, SCStreamDelegate {
    var onStop: (() -> Void)?
    private let lock = NSLock()
    private var failure: Error?

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        lock.lock()
        failure = error
        lock.unlock()
        onStop?()
    }

    func recordedFailure() -> Error? {
        lock.lock()
        defer { lock.unlock() }
        return failure
    }
}

struct Arguments {
    let windowID: UInt32
    let outputURL: URL
    let readyURL: URL
    let maxSeconds: Double

    static func parse(_ values: [String]) throws -> Arguments {
        var windowID: UInt32?
        var output: String?
        var ready: String?
        var maxSeconds = 90.0
        var index = 0
        while index < values.count {
            let key = values[index]
            guard index + 1 < values.count else {
                throw RecorderError.invalidArguments("参数 \(key) 缺少值")
            }
            let value = values[index + 1]
            switch key {
            case "--window-id":
                guard let parsed = UInt32(value), parsed > 0 else {
                    throw RecorderError.invalidArguments("无效的 --window-id: \(value)")
                }
                windowID = parsed
            case "--output":
                output = value
            case "--ready-file":
                ready = value
            case "--max-seconds":
                guard let parsed = Double(value), parsed > 0 else {
                    throw RecorderError.invalidArguments("无效的 --max-seconds: \(value)")
                }
                maxSeconds = parsed
            default:
                throw RecorderError.invalidArguments("未知参数: \(key)")
            }
            index += 2
        }
        guard let windowID, let output, let ready else {
            throw RecorderError.invalidArguments("需要 --window-id、--output 和 --ready-file")
        }
        return Arguments(
            windowID: windowID,
            outputURL: URL(fileURLWithPath: output),
            readyURL: URL(fileURLWithPath: ready),
            maxSeconds: maxSeconds
        )
    }
}

func waitForSemaphore(_ semaphore: DispatchSemaphore) async {
    await withCheckedContinuation { continuation in
        DispatchQueue.global(qos: .userInitiated).async {
            semaphore.wait()
            continuation.resume()
        }
    }
}

func waitForSemaphore(
    _ semaphore: DispatchSemaphore,
    timeout: DispatchTime
) async -> DispatchTimeoutResult {
    await withCheckedContinuation { continuation in
        DispatchQueue.global(qos: .userInitiated).async {
            let result = semaphore.wait(timeout: timeout)
            continuation.resume(returning: result)
        }
    }
}

func writeReadyFile(_ arguments: Arguments, width: Int, height: Int) throws {
    let payload: [String: Any] = [
        "status": "started",
        "backend": "screen-capture-kit",
        "windowId": Int(arguments.windowID),
        "showsCursor": false,
        "cursorCaptured": false,
        "width": width,
        "height": height,
        "maxSeconds": arguments.maxSeconds,
        "outputPath": arguments.outputURL.path,
        "pid": Int(getpid()),
    ]
    let data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
    try data.write(to: arguments.readyURL, options: .atomic)
}

func runRecorder(_ arguments: Arguments) async throws {
    try FileManager.default.createDirectory(
        at: arguments.outputURL.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )
    try? FileManager.default.removeItem(at: arguments.outputURL)
    try? FileManager.default.removeItem(at: arguments.readyURL)

    let content = try await SCShareableContent.excludingDesktopWindows(
        false,
        onScreenWindowsOnly: false
    )
    guard let window = content.windows.first(where: { $0.windowID == arguments.windowID }) else {
        throw RecorderError.windowNotFound(arguments.windowID)
    }

    let scale = max(1.0, NSScreen.main?.backingScaleFactor ?? 1.0)
    let width = max(2, Int((window.frame.width * scale).rounded()))
    let height = max(2, Int((window.frame.height * scale).rounded()))

    let configuration = SCStreamConfiguration()
    configuration.width = width
    configuration.height = height
    configuration.minimumFrameInterval = CMTime(value: 1, timescale: 30)
    configuration.queueDepth = 8
    configuration.pixelFormat = kCVPixelFormatType_32BGRA
    configuration.scalesToFit = true
    configuration.showsCursor = false
    configuration.showMouseClicks = false
    configuration.capturesAudio = false

    let filter = SCContentFilter(desktopIndependentWindow: window)
    let recordingConfiguration = SCRecordingOutputConfiguration()
    recordingConfiguration.outputURL = arguments.outputURL
    recordingConfiguration.outputFileType = .mov
    recordingConfiguration.videoCodecType = .h264

    let stopSignal = DispatchSemaphore(value: 0)
    let recordingDelegate = RecordingOutputDelegate()
    let streamDelegate = StreamDelegate()
    streamDelegate.onStop = { stopSignal.signal() }
    let recordingOutput = SCRecordingOutput(
        configuration: recordingConfiguration,
        delegate: recordingDelegate
    )
    let stream = SCStream(filter: filter, configuration: configuration, delegate: streamDelegate)
    try stream.addRecordingOutput(recordingOutput)

    signal(SIGINT, SIG_IGN)
    signal(SIGTERM, SIG_IGN)
    let signalQueue = DispatchQueue(label: "sologsb.screencapturekit.signals")
    let interruptSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: signalQueue)
    interruptSource.setEventHandler { stopSignal.signal() }
    interruptSource.resume()
    let terminateSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: signalQueue)
    terminateSource.setEventHandler { stopSignal.signal() }
    terminateSource.resume()

    let timeoutSource = DispatchSource.makeTimerSource(queue: signalQueue)
    timeoutSource.schedule(deadline: .now() + arguments.maxSeconds)
    timeoutSource.setEventHandler { stopSignal.signal() }
    timeoutSource.resume()

    try await stream.startCapture()
    try writeReadyFile(arguments, width: width, height: height)

    await waitForSemaphore(stopSignal)

    try await stream.stopCapture()
    if await waitForSemaphore(recordingDelegate.finished, timeout: .now() + 20) == .timedOut {
        throw RecorderError.finishTimedOut
    }
    timeoutSource.cancel()
    interruptSource.cancel()
    terminateSource.cancel()

    if let error = streamDelegate.recordedFailure() {
        throw RecorderError.recordingFailed("SCStream 停止: \(error.localizedDescription)")
    }
    if let error = recordingDelegate.recordedFailure() {
        throw RecorderError.recordingFailed("录制输出失败: \(error.localizedDescription)")
    }

    let attributes = try FileManager.default.attributesOfItem(
        atPath: arguments.outputURL.path
    )
    let size = (attributes[.size] as? NSNumber)?.intValue ?? 0
    guard size > 0 else {
        throw RecorderError.recordingFailed("录制文件为空")
    }

    let payload: [String: Any] = [
        "status": "finished",
        "backend": "screen-capture-kit",
        "windowId": Int(arguments.windowID),
        "showsCursor": false,
        "cursorCaptured": false,
        "outputPath": arguments.outputURL.path,
        "sizeBytes": size,
    ]
    let data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

@main
struct ScreenCaptureKitWindowRecorder {
    static func main() async {
        do {
            let arguments = try Arguments.parse(Array(CommandLine.arguments.dropFirst()))
            try await runRecorder(arguments)
        } catch {
            let message = "SCREEN_CAPTURE_KIT_ERROR: \(error)\n"
            FileHandle.standardError.write(message.data(using: .utf8)!)
            exit(1)
        }
    }
}
