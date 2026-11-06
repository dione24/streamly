import Foundation
import Libmpv

/// What mpv reads for movies and episodes, instead of FFmpeg's HTTP client.
///
/// FFmpeg keeps the old connection open while it opens the one a seek needs, and opening a Matroska
/// or an MP4 file seeks at least once. Panels allow one connection and refuse a second one, then
/// refuse again for 1 to 2 seconds after a close: a resume or a jump would fail every time. Here the
/// old connection is closed first, and a refusal just means waiting and asking again.
final class RangeStream: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private static let highWater = 12 << 20
    private static let lowWater = 4 << 20
    /// A jump this short costs less by reading through than by reconnecting.
    private static let skipLimit: Int64 = 256 << 10
    private static let maxAttempts = 12

    private let url: URL
    private let userAgent: String
    private let condition = NSCondition()
    private var session: URLSession!

    private var task: URLSessionDataTask?
    private var generation = 0
    private var buffer = Data()
    private var bufferHead = 0
    private var skipBytes: Int64 = 0
    private var position: Int64 = 0
    private var totalSize: Int64?
    private var hasResponded = false
    private var atEnd = false
    private var failed = false
    private var cancelled = false
    private var suspended = false
    private var needsRequest = true
    private var waitingForRetry = false
    private var attempts = 0

    init(url: URL, userAgent: String) {
        self.url = url
        self.userAgent = userAgent
        super.init()
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpMaximumConnectionsPerHost = 1
        configuration.timeoutIntervalForRequest = 20
        configuration.timeoutIntervalForResource = 7 * 24 * 3600
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.urlCache = nil
        configuration.networkServiceType = .video
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        session = URLSession(configuration: configuration, delegate: self, delegateQueue: queue)
    }

    private var buffered: Int { buffer.count - bufferHead }

    // MARK: mpv callbacks, on mpv's threads

    func read(into destination: UnsafeMutablePointer<CChar>, count: UInt64) -> Int64 {
        condition.lock()
        defer { condition.unlock() }
        while true {
            if cancelled { return -1 }
            if buffered > 0 {
                let amount = min(Int(clamping: count), buffered)
                buffer.withUnsafeBytes { bytes in
                    let source = bytes.bindMemory(to: CChar.self).baseAddress! + bufferHead
                    destination.update(from: source, count: amount)
                }
                bufferHead += amount
                position += Int64(amount)
                if bufferHead >= buffer.count {
                    buffer.removeAll(keepingCapacity: true)
                    bufferHead = 0
                } else if bufferHead > 1 << 20 {
                    buffer.removeFirst(bufferHead)
                    bufferHead = 0
                }
                if suspended, buffered < Self.lowWater {
                    suspended = false
                    task?.resume()
                }
                return Int64(amount)
            }
            if atEnd { return 0 }
            if failed { return -1 }
            startIfNeeded()
            _ = condition.wait(until: Date().addingTimeInterval(0.5))
        }
    }

    func seek(to offset: Int64) -> Int64 {
        condition.lock()
        defer { condition.unlock() }
        guard !cancelled, offset >= 0 else { return Int64(MPV_ERROR_GENERIC.rawValue) }
        if offset == position { return offset }

        let distance = offset - position
        if distance > 0, task != nil || buffered > 0, distance <= Int64(buffered) + Self.skipLimit, !atEnd {
            let dropped = min(Int(distance), buffered)
            bufferHead += dropped
            skipBytes += distance - Int64(dropped)
            position = offset
            if suspended, buffered < Self.lowWater {
                suspended = false
                task?.resume()
            }
            return offset
        }

        dropConnection()
        position = offset
        attempts = 0
        failed = false
        atEnd = false
        if let totalSize, offset >= totalSize {
            atEnd = true
            needsRequest = false
        } else {
            needsRequest = true
        }
        condition.broadcast()
        return offset
    }

    /// Blocks until the server has answered: the demuxers ask for the size right after opening.
    func size() -> Int64 {
        condition.lock()
        defer { condition.unlock() }
        while !hasResponded, !failed, !cancelled {
            startIfNeeded()
            _ = condition.wait(until: Date().addingTimeInterval(0.5))
        }
        return totalSize ?? Int64(MPV_ERROR_UNSUPPORTED.rawValue)
    }

    func cancel() {
        condition.lock()
        cancelled = true
        condition.broadcast()
        condition.unlock()
    }

    func close() {
        condition.lock()
        cancelled = true
        dropConnection()
        condition.broadcast()
        condition.unlock()
        session.invalidateAndCancel()
    }

    // MARK: Connection, with the lock held

    private func startIfNeeded() {
        guard needsRequest, task == nil, !waitingForRetry, !cancelled else { return }
        needsRequest = false
        generation += 1
        // Bytes already buffered stay valid: only what follows them is requested.
        let offset = position + Int64(buffered) + skipBytes
        skipBytes = 0
        var request = URLRequest(url: url)
        request.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        request.setValue("bytes=\(offset)-", forHTTPHeaderField: "Range")
        request.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
        request.setValue("close", forHTTPHeaderField: "Connection")
        let newTask = session.dataTask(with: request)
        task = newTask
        suspended = false
        newTask.resume()
    }

    private func dropConnection() {
        generation += 1
        task?.cancel()
        task = nil
        suspended = false
        waitingForRetry = false
        buffer.removeAll(keepingCapacity: true)
        bufferHead = 0
        skipBytes = 0
    }

    private func scheduleRetry() {
        task = nil
        attempts += 1
        guard attempts <= Self.maxAttempts else {
            failed = true
            condition.broadcast()
            return
        }
        waitingForRetry = true
        generation += 1
        let expected = generation
        let delay = min(0.4 * Double(attempts), 2)
        DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self else { return }
            self.condition.lock()
            defer { self.condition.unlock() }
            guard self.generation == expected, !self.cancelled else { return }
            self.waitingForRetry = false
            self.needsRequest = true
            self.condition.broadcast()
        }
    }

    // MARK: URLSession

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        condition.lock()
        defer { condition.unlock() }
        guard dataTask === task, let http = response as? HTTPURLResponse else {
            completionHandler(.cancel)
            return
        }
        switch http.statusCode {
        case 200, 206:
            if http.statusCode == 206 {
                if let range = http.value(forHTTPHeaderField: "Content-Range"),
                   let total = range.split(separator: "/").last.flatMap({ Int64($0) }) {
                    totalSize = total
                }
            } else {
                // The server ignored the range and starts from zero: read through up to where we are.
                if response.expectedContentLength >= 0 { totalSize = response.expectedContentLength }
                skipBytes = position + Int64(buffered)
            }
            hasResponded = true
            completionHandler(.allow)
        case 401, 403, 429, 500...599:
            // A refusal from the panel, usually because the previous connection just closed.
            completionHandler(.cancel)
            scheduleRetry()
        default:
            completionHandler(.cancel)
            task = nil
            failed = true
        }
        condition.broadcast()
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        condition.lock()
        defer { condition.unlock() }
        guard dataTask === task else { return }
        var incoming = data[...]
        if skipBytes > 0 {
            let dropped = min(Int(clamping: skipBytes), incoming.count)
            incoming = incoming.dropFirst(dropped)
            skipBytes -= Int64(dropped)
        }
        guard !incoming.isEmpty else { return }
        attempts = 0
        buffer.append(contentsOf: incoming)
        if buffered > Self.highWater, !suspended {
            suspended = true
            dataTask.suspend()
        }
        condition.broadcast()
    }

    func urlSession(_ session: URLSession, task finished: URLSessionTask, didCompleteWithError error: Error?) {
        condition.lock()
        defer { condition.unlock() }
        guard finished === task else { return }
        if error != nil {
            // Dropped mid-film: carry on from the last byte received.
            scheduleRetry()
        } else {
            task = nil
            atEnd = true
        }
        condition.broadcast()
    }
}

/// Lets mpv open `streamly://<token>` without ever seeing the address, which holds the password.
final class RangeStreamRegistry: @unchecked Sendable {
    static let scheme = "streamly"

    private let lock = NSLock()
    private var token: String?
    private var target: (url: URL, userAgent: String)?

    func register(url: URL, userAgent: String) -> String {
        lock.lock()
        defer { lock.unlock() }
        let fresh = UUID().uuidString
        token = fresh
        target = (url, userAgent)
        return "\(Self.scheme)://\(fresh)"
    }

    func stream(for uri: String) -> RangeStream? {
        lock.lock()
        defer { lock.unlock() }
        guard let token, uri == "\(Self.scheme)://\(token)", let target else { return nil }
        return RangeStream(url: target.url, userAgent: target.userAgent)
    }

    func install(on handle: OpaquePointer) {
        let status = mpv_stream_cb_add_ro(handle, Self.scheme, Unmanaged.passUnretained(self).toOpaque()) { context, uri, info in
            guard let context, let uri, let info else { return MPV_ERROR_LOADING_FAILED.rawValue }
            let registry = Unmanaged<RangeStreamRegistry>.fromOpaque(context).takeUnretainedValue()
            guard let stream = registry.stream(for: String(cString: uri)) else {
                return MPV_ERROR_LOADING_FAILED.rawValue
            }
            info.pointee.cookie = Unmanaged.passRetained(stream).toOpaque()
            info.pointee.read_fn = { cookie, destination, count in
                guard let cookie, let destination else { return -1 }
                return Unmanaged<RangeStream>.fromOpaque(cookie).takeUnretainedValue().read(into: destination, count: count)
            }
            info.pointee.seek_fn = { cookie, offset in
                guard let cookie else { return Int64(MPV_ERROR_GENERIC.rawValue) }
                return Unmanaged<RangeStream>.fromOpaque(cookie).takeUnretainedValue().seek(to: offset)
            }
            info.pointee.size_fn = { cookie in
                guard let cookie else { return Int64(MPV_ERROR_UNSUPPORTED.rawValue) }
                return Unmanaged<RangeStream>.fromOpaque(cookie).takeUnretainedValue().size()
            }
            info.pointee.cancel_fn = { cookie in
                guard let cookie else { return }
                Unmanaged<RangeStream>.fromOpaque(cookie).takeUnretainedValue().cancel()
            }
            info.pointee.close_fn = { cookie in
                guard let cookie else { return }
                let stream = Unmanaged<RangeStream>.fromOpaque(cookie)
                stream.takeUnretainedValue().close()
                stream.release()
            }
            return 0
        }
        if status < 0 { NSLog("Streamly: mpv refused the stream callbacks") }
    }
}
