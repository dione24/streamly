import Foundation
import Libmpv
import QuartzCore

public struct MediaTrack: Identifiable, Hashable, Sendable {
    public enum Kind: String, Sendable { case audio, sub, video }

    public let id: Int
    public let kind: Kind
    public let title: String
    public let selected: Bool
}

public struct MediaInfo: Equatable, Sendable {
    public var width: Int?
    public var height: Int?
    public var videoCodec: String?
    public var audioCodec: String?
    public var hardwareDecoder: String?

    public init(width: Int? = nil, height: Int? = nil, videoCodec: String? = nil,
                audioCodec: String? = nil, hardwareDecoder: String? = nil) {
        self.width = width
        self.height = height
        self.videoCodec = videoCodec
        self.audioCodec = audioCodec
        self.hardwareDecoder = hardwareDecoder
    }
}

public struct PlayerSnapshot: Sendable {
    public let media: MediaInfo
    public let tracks: [MediaTrack]
    public let position: Double?
    public let duration: Double?
}

/// Pixels as mpv rendered them, in BGR0 order.
public struct RawFrame: Sendable {
    public let width: Int
    public let height: Int
    public let stride: Int
    public let data: Data
}

public enum PlayerEvent: Sendable {
    case startedLoading
    case playing
    case ended(reason: EndReason, error: String?)
    case buffering(Bool)
    case paused(Bool)
    case muted(Bool)
    case mediaChanged
}

public enum EndReason: Sendable {
    /// The stream failed: a live channel should be reconnected.
    case streamStopped
    /// mpv reached the end of the data. For a movie that is usually the end of the film, but a
    /// dropped connection looks the same, so the caller compares the position with the duration.
    case finished
    /// Our own doing: `loadfile` replaced it or `stop` was called.
    case replaced
}

/// Thin wrapper over libmpv. Without a layer it decodes with no display (`vo=null`), which is what
/// the command-line check uses. Events arrive on the main queue.
///
/// Nothing here may block the main thread on mpv. While mpv rebuilds its video output, MoltenVK
/// waits for the main thread to touch the layer, and mpv's core waits for the output: a synchronous
/// property read from the main thread at that moment freezes the app for good. Everything goes
/// through `control`, and results come back asynchronously.
public final class MPVPlayer: @unchecked Sendable {
    /// Host layer for the video; nil means headless.
    public let layer: MPVMetalLayer?
    public var onEvent: ((PlayerEvent) -> Void)?

    private let core: Core
    private let control = DispatchQueue(label: "streamly.mpv.control", qos: .userInitiated)
    private let rangeStreams = RangeStreamRegistry()

    public init(headless: Bool = false, enableAudio: Bool = true) {
        let layer = headless ? nil : MPVMetalLayer()
        self.layer = layer

        guard let handle = mpv_create() else {
            fatalError("libmpv could not allocate a player")
        }
        self.core = Core(handle: handle)

        func set(_ name: String, _ value: String) {
            let status = mpv_set_option_string(handle, name, value)
            if status < 0 {
                NSLog("Streamly: mpv option %@ refused: %s", name, mpv_error_string(status))
            }
        }

        set("config", "no")
        set("load-scripts", "no")
        set("ytdl", "no")
        set("idle", "yes")
        set("keep-open", "no")
        set("input-default-bindings", "no")
        set("input-vo-keyboard", "no")
        set("input-media-keys", "no")
        set("osc", "no")
        set("osd-level", "0")
        set("audio-client-name", "Streamly")
        set("subs-fallback", "no")

        // Live IPTV: start fast, keep a modest buffer, and fail quickly enough for the plan to act.
        set("network-timeout", "15")
        set("cache", "yes")
        set("cache-secs", "20")
        set("demuxer-readahead-secs", "8")
        set("demuxer-max-bytes", "64MiB")
        set("demuxer-max-back-bytes", "32MiB")
        set("demuxer-lavf-analyzeduration", "2")
        set("hwdec", "videotoolbox")

        if let layer {
            var pointer = Int64(Int(bitPattern: Unmanaged.passUnretained(layer).toOpaque()))
            let status = mpv_set_option(handle, "wid", MPV_FORMAT_INT64, &pointer)
            if status < 0 { NSLog("Streamly: mpv refused the video layer") }
            set("vo", "gpu-next")
            set("gpu-api", "vulkan")
            set("gpu-context", "moltenvk")
        } else {
            set("vo", "null")
        }
        if !enableAudio { set("ao", "null") }

        let initialized = mpv_initialize(handle)
        if initialized < 0 {
            fatalError("libmpv failed to start: \(String(cString: mpv_error_string(initialized)))")
        }

        rangeStreams.install(on: handle)

        core.observe("paused-for-cache", MPV_FORMAT_FLAG)
        core.observe("pause", MPV_FORMAT_FLAG)
        core.observe("mute", MPV_FORMAT_FLAG)
        core.observe("video-params/w", MPV_FORMAT_INT64)
        core.observe("audio-codec-name", MPV_FORMAT_STRING)
        core.observe("track-list/count", MPV_FORMAT_INT64)
        core.onEvent = { [weak self] event in
            self?.onEvent?(event)
        }
        core.start()
    }

    deinit {
        core.shutdown()
    }

    // MARK: Control

    /// Replaces the current stream: mpv closes the old connection before opening the new one, which
    /// matters on single-connection subscriptions.
    public func play(url: String, userAgent: String, startAt: Double? = nil, onDemand: Bool = false) {
        let target: String
        if onDemand, let parsed = URL(string: url), parsed.scheme?.lowercased().hasPrefix("http") == true {
            target = rangeStreams.register(url: parsed, userAgent: userAgent)
        } else {
            target = url
        }
        control.async { [core] in
            core.setString("user-agent", userAgent)
            // A global option rather than a `loadfile` argument, whose position changed between mpv
            // versions: a refused command fails silently and nothing ever starts.
            core.setString("start", startAt.map { $0 > 0 ? String(Int($0)) : "none" } ?? "none")
            core.command(["loadfile", target, "replace"])
        }
    }

    public func seek(to position: Double) {
        control.async { [core] in core.command(["seek", String(max(position, 0)), "absolute"]) }
    }

    public func seek(by offset: Double) {
        control.async { [core] in core.command(["seek", String(offset), "relative"]) }
    }

    public func stop() {
        control.async { [core] in core.command(["stop"]) }
    }

    public func togglePause() {
        control.async { [core] in core.command(["cycle", "pause"]) }
    }

    public func toggleMute() {
        control.async { [core] in core.command(["cycle", "mute"]) }
    }

    /// 0 to 100.
    public func setVolume(_ value: Double) {
        let clamped = min(max(value, 0), 100)
        control.async { [core] in core.setDouble("volume", clamped) }
    }

    public func select(track: MediaTrack) {
        let name = track.kind == .audio ? "aid" : (track.kind == .sub ? "sid" : "vid")
        control.async { [core] in core.setString(name, String(track.id)) }
    }

    public func disableSubtitles() {
        control.async { [core] in core.setString("sid", "no") }
    }

    // MARK: State

    public func snapshot() async -> PlayerSnapshot {
        await onControl { core in
            PlayerSnapshot(
                media: MediaInfo(
                    width: core.int("video-params/w"),
                    height: core.int("video-params/h"),
                    videoCodec: core.string("video-codec"),
                    audioCodec: core.string("audio-codec-name"),
                    hardwareDecoder: core.string("hwdec-current")),
                tracks: Self.readTracks(core),
                position: core.double("time-pos"),
                duration: core.double("duration"))
        }
    }

    /// Any mpv property as text, for diagnostics.
    public func property(_ name: String) async -> String? {
        await onControl { $0.string(name) }
    }

    /// `window` is what the renderer really shows, `video` the decoded frame.
    public func rawFrame(mode: String = "window") async -> RawFrame? {
        await onControl { $0.rawFrame(mode: mode) }
    }

    private func onControl<Value: Sendable>(_ work: @escaping @Sendable (Core) -> Value) async -> Value {
        await withCheckedContinuation { continuation in
            control.async { [core] in continuation.resume(returning: work(core)) }
        }
    }

    private static func readTracks(_ core: Core) -> [MediaTrack] {
        let count = core.int("track-list/count") ?? 0
        return (0..<count).compactMap { index in
            let prefix = "track-list/\(index)"
            guard let id = core.int("\(prefix)/id"),
                  let kind = core.string("\(prefix)/type").flatMap(MediaTrack.Kind.init(rawValue:)) else { return nil }
            let parts = [core.string("\(prefix)/lang")?.uppercased(),
                         core.string("\(prefix)/title"),
                         core.string("\(prefix)/codec")].compactMap { $0 }
            return MediaTrack(
                id: id, kind: kind,
                title: parts.isEmpty ? "Piste \(id)" : parts.joined(separator: " · "),
                selected: core.flag("\(prefix)/selected"))
        }
    }
}

/// Owns the mpv handle and drains its events on a private queue. It outlives the callback: the
/// wakeup context holds a strong reference until the handle is destroyed.
private final class Core: @unchecked Sendable {
    var onEvent: ((PlayerEvent) -> Void)?

    private let handle: OpaquePointer
    private let queue = DispatchQueue(label: "streamly.mpv.events", qos: .userInitiated)
    private var closed = false

    init(handle: OpaquePointer) {
        self.handle = handle
    }

    func start() {
        let context = Unmanaged.passRetained(self).toOpaque()
        mpv_set_wakeup_callback(handle, { raw in
            guard let raw else { return }
            let core = Unmanaged<Core>.fromOpaque(raw).takeUnretainedValue()
            core.queue.async { core.drain() }
        }, context)
    }

    /// Destroying mpv waits for its video output, so this must not happen on the calling thread.
    func shutdown() {
        mpv_set_wakeup_callback(handle, nil, nil)
        queue.async { [self] in
            closed = true
            mpv_terminate_destroy(handle)
            Unmanaged.passUnretained(self).release()
        }
    }

    func observe(_ name: String, _ format: mpv_format) {
        mpv_observe_property(handle, 0, name, format)
    }

    private func drain() {
        while !closed {
            guard let event = mpv_wait_event(handle, 0)?.pointee, event.event_id != MPV_EVENT_NONE else { return }
            if let mapped = map(event) {
                DispatchQueue.main.async { [weak self] in self?.onEvent?(mapped) }
            }
        }
    }

    private func map(_ event: mpv_event) -> PlayerEvent? {
        switch event.event_id {
        case MPV_EVENT_START_FILE:
            return .startedLoading
        case MPV_EVENT_PLAYBACK_RESTART:
            return .playing
        case MPV_EVENT_END_FILE:
            guard let end = event.data?.assumingMemoryBound(to: mpv_event_end_file.self).pointee else {
                return .ended(reason: .streamStopped, error: nil)
            }
            let reason = mpv_end_file_reason(rawValue: UInt32(end.reason.rawValue))
            if reason == MPV_END_FILE_REASON_STOP || reason == MPV_END_FILE_REASON_REDIRECT || reason == MPV_END_FILE_REASON_QUIT {
                return .ended(reason: .replaced, error: nil)
            }
            if reason == MPV_END_FILE_REASON_EOF {
                return .ended(reason: .finished, error: nil)
            }
            let message = reason == MPV_END_FILE_REASON_ERROR ? String(cString: mpv_error_string(end.error)) : nil
            return .ended(reason: .streamStopped, error: message)
        case MPV_EVENT_PROPERTY_CHANGE:
            guard let property = event.data?.assumingMemoryBound(to: mpv_event_property.self).pointee,
                  let rawName = property.name else { return nil }
            let name = String(cString: rawName)
            let flag = property.format == MPV_FORMAT_FLAG
                ? property.data?.assumingMemoryBound(to: Int32.self).pointee != 0 : false
            switch name {
            case "paused-for-cache": return .buffering(flag)
            case "pause": return .paused(flag)
            case "mute": return .muted(flag)
            default: return .mediaChanged
            }
        default:
            return nil
        }
    }

    // MARK: Access, from the control queue only

    func command(_ arguments: [String]) {
        var pointers: [UnsafePointer<CChar>?] = arguments.map { UnsafePointer(strdup($0)) }
        pointers.append(nil)
        defer { pointers.compactMap { $0 }.forEach { free(UnsafeMutablePointer(mutating: $0)) } }
        let status = pointers.withUnsafeMutableBufferPointer {
            mpv_command_async(handle, 0, $0.baseAddress)
        }
        if status < 0 { NSLog("Streamly: mpv command refused: %s", mpv_error_string(status)) }
    }

    func rawFrame(mode: String) -> RawFrame? {
        let arguments = ["screenshot-raw", mode]
        var pointers: [UnsafePointer<CChar>?] = arguments.map { UnsafePointer(strdup($0)) }
        pointers.append(nil)
        defer { pointers.compactMap { $0 }.forEach { free(UnsafeMutablePointer(mutating: $0)) } }

        var result = mpv_node()
        let status = pointers.withUnsafeMutableBufferPointer {
            mpv_command_ret(handle, $0.baseAddress, &result)
        }
        guard status >= 0 else { return nil }
        defer { mpv_free_node_contents(&result) }
        guard result.format == MPV_FORMAT_NODE_MAP, let list = result.u.list?.pointee else { return nil }

        var width = 0, height = 0, stride = 0
        var data = Data()
        for index in 0..<Int(list.num) {
            guard let key = list.keys?[index], let value = list.values?[index] else { continue }
            switch String(cString: key) {
            case "w": width = Int(value.u.int64)
            case "h": height = Int(value.u.int64)
            case "stride": stride = Int(value.u.int64)
            case "data":
                if let bytes = value.u.ba?.pointee, let start = bytes.data {
                    data = Data(bytes: start, count: bytes.size)
                }
            default: break
            }
        }
        guard width > 0, height > 0, !data.isEmpty else { return nil }
        return RawFrame(width: width, height: height, stride: stride, data: data)
    }

    func string(_ name: String) -> String? {
        guard let raw = mpv_get_property_string(handle, name) else { return nil }
        defer { mpv_free(raw) }
        let value = String(cString: raw)
        return value.isEmpty ? nil : value
    }

    func int(_ name: String) -> Int? {
        var value: Int64 = 0
        return mpv_get_property(handle, name, MPV_FORMAT_INT64, &value) >= 0 ? Int(value) : nil
    }

    func double(_ name: String) -> Double? {
        var value = 0.0
        return mpv_get_property(handle, name, MPV_FORMAT_DOUBLE, &value) >= 0 ? value : nil
    }

    func flag(_ name: String) -> Bool {
        var value: Int32 = 0
        return mpv_get_property(handle, name, MPV_FORMAT_FLAG, &value) >= 0 && value != 0
    }

    func setString(_ name: String, _ value: String) {
        mpv_set_property_string(handle, name, value)
    }

    func setDouble(_ name: String, _ value: Double) {
        var raw = value
        mpv_set_property(handle, name, MPV_FORMAT_DOUBLE, &raw)
    }
}

/// MoltenVK sets the drawable size to 1×1 to force a presentation, which makes the picture flicker
/// and can leave it stuck: https://github.com/mpv-player/mpv/pull/13651
public final class MPVMetalLayer: CAMetalLayer {
    public override var drawableSize: CGSize {
        get { super.drawableSize }
        set {
            if Int(newValue.width) > 1, Int(newValue.height) > 1 {
                super.drawableSize = newValue
            }
        }
    }

    public override init() {
        super.init()
        backgroundColor = CGColor(red: 0, green: 0, blue: 0, alpha: 1)
        framebufferOnly = true
    }

    public override init(layer: Any) {
        super.init(layer: layer)
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
    }
}
