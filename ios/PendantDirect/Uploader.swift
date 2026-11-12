import CryptoKit
import Foundation

/// Uploads sealed spool segments to the server ingest server with a
/// background URLSession, so uploads continue while the app is suspended.
/// A segment is deleted locally only after the server answers 200 with the
/// segment's SHA-256, meaning it is durable on the server.
final class Uploader: NSObject, URLSessionDataDelegate {
    static let sessionID = "\(Bundle.main.bundleIdentifier ?? "com.example.pendant").upload"   // background URLSession

    private let store: CaptureStore
    private let log: (String) -> Void
    private lazy var session: URLSession = {
        let cfg = URLSessionConfiguration.background(withIdentifier: Self.sessionID)
        cfg.isDiscretionary = false
        cfg.sessionSendsLaunchEvents = true
        cfg.allowsCellularAccess = true
        return URLSession(configuration: cfg, delegate: self, delegateQueue: nil)
    }()
    private var inFlight = Set<String>()
    private var responses: [Int: Data] = [:]
    private let lock = NSLock()
    var backgroundCompletion: (() -> Void)?
    var onUploaded: ((String, Int) -> Void)?

    static var baseURL: URL? {
        let s = (Bundle.main.object(forInfoDictionaryKey: "PendantIngestURL") as? String) ?? ""
        return URL(string: s)
    }

    static var token: String {
        (Bundle.main.object(forInfoDictionaryKey: "PendantIngestToken") as? String) ?? ""
    }

    init(store: CaptureStore, log: @escaping (String) -> Void) {
        self.store = store
        self.log = log
        super.init()
        _ = session
        session.getAllTasks { tasks in
            self.lock.lock()
            for t in tasks { if let name = t.taskDescription { self.inFlight.insert(name) } }
            self.lock.unlock()
        }
    }

    func kick() {
        guard let base = Self.baseURL, !Self.token.isEmpty else {
            log("upload disabled: no ingest URL/token in build")
            return
        }
        for url in store.readySegments() {
            let name = url.lastPathComponent
            lock.lock()
            let busy = inFlight.contains(name)
            if !busy { inFlight.insert(name) }
            lock.unlock()
            if busy { continue }
            guard let data = try? Data(contentsOf: url) else { continue }
            let sha = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
            var req = URLRequest(url: base.appendingPathComponent(name))
            req.httpMethod = "PUT"
            req.setValue("Bearer \(Self.token)", forHTTPHeaderField: "Authorization")
            req.setValue(sha, forHTTPHeaderField: "X-Content-SHA256")
            req.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
            let task = session.uploadTask(with: req, fromFile: url)
            task.taskDescription = name
            task.resume()
        }
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        lock.lock()
        responses[dataTask.taskIdentifier, default: Data()].append(data)
        lock.unlock()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let name = task.taskDescription else { return }
        lock.lock()
        let body = responses.removeValue(forKey: task.taskIdentifier) ?? Data()
        inFlight.remove(name)
        lock.unlock()
        let status = (task.response as? HTTPURLResponse)?.statusCode ?? 0
        let sent = task.originalRequest?.value(forHTTPHeaderField: "X-Content-SHA256") ?? ""
        let reply = String(decoding: body, as: UTF8.self)
        if error == nil, status == 200, !sent.isEmpty, reply.contains(sent) {
            let url = store.readyDir.appendingPathComponent(name)
            let size = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
            try? FileManager.default.removeItem(at: url)
            log("uploaded \(name) (\(size) B)")
            onUploaded?(name, size)
        } else {
            log("upload \(name) failed: status=\(status) \(error?.localizedDescription ?? reply.prefix(80).description)")
        }
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        DispatchQueue.main.async {
            self.backgroundCompletion?()
            self.backgroundCompletion = nil
        }
    }
}
