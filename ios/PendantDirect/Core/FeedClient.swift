import Foundation

/// The server answered with something other than 200.
public struct FeedHTTPStatus: Error, Equatable {
    public let code: Int
    public init(code: Int) { self.code = code }
}

/// Why a feed request failed, as far as the app can tell.
public enum FeedErrorKind: String, Equatable {
    /// Offline, or the name doesn't resolve: Tailscale is the usual cause.
    case offline
    /// The server is up but restarting or overloaded (502/503/504, other 5xx), or the request timed out.
    case busy
    /// The reply arrived but couldn't be read (the app and server disagree about its shape).
    case decode
    /// 401/403: the app's token isn't accepted.
    case rejected
    /// Any other 4xx, a bad URL: retrying can't help.
    case client

    /// Worth trying again shortly: the cause is usually a deploy, a restart or a blip.
    public var isTransient: Bool { self == .offline || self == .busy }
}

/// A failed feed request: its kind, the HTTP status when there was one, and what to tell the person.
public struct FeedProblem: Equatable {
    public let kind: FeedErrorKind
    public let status: Int?
    public let detail: String

    public init(_ error: Error) {
        self.kind = FeedClient.classify(error)
        if let s = error as? FeedHTTPStatus { status = s.code } else { status = nil }
        detail = "\(error)"
    }

    public var message: String {
        switch kind {
        case .offline: return "No connection to \(AppSettings.serverName) (check Tailscale)"
        case .busy: return "\(AppSettings.serverNameAtStart) is busy, retrying…"
        case .decode: return "Couldn't read the server's reply (app needs an update?)"
        case .rejected: return "\(AppSettings.serverNameAtStart) didn't accept this app's token (app needs an update?)"
        case .client: return "\(AppSettings.serverNameAtStart) couldn't do that request"
        }
    }

    /// The line the app log gets for a final failure.
    public func logLine(route: String) -> String {
        "feed \(route) failed: \(kind.rawValue) \(status.map(String.init) ?? "-") \(detail)"
    }
}

public enum FeedClient {
    /// Waits before each retry: 3 retries after the first try.
    public static let delays: [Double] = [0.5, 1.5, 4]

    public static func classify(_ error: Error) -> FeedErrorKind {
        if let s = error as? FeedHTTPStatus {
            switch s.code {
            case 401, 403: return .rejected
            case 400..<500: return .client
            default: return .busy
            }
        }
        if error is DecodingError { return .decode }
        if let u = error as? URLError {
            switch u.code {
            case .timedOut: return .busy
            case .badURL, .unsupportedURL: return .client
            case .cannotDecodeContentData, .cannotDecodeRawData, .cannotParseResponse, .zeroByteResource: return .decode
            case .networkConnectionLost, .secureConnectionFailed: return .busy
            case .badServerResponse: return .busy
            default: return .offline     // not connected, DNS, can't connect, cancelled-by-network...
            }
        }
        return .busy
    }

    /// Runs `attempt`, trying again after each of `delays` while the failure is transient.
    /// The final failure is logged once (`log(route failed...)`) and thrown as it came.
    public static func run<T>(route: String, delays: [Double] = FeedClient.delays,
                              sleep: (Double) async throws -> Void = { try await Task.sleep(for: .seconds($0)) },
                              log: (String) -> Void = FeedLog.write,
                              _ attempt: () async throws -> T) async throws -> T {
        var tries = 0
        while true {
            do {
                return try await attempt()
            } catch {
                if error is CancellationError || (error as? URLError)?.code == .cancelled { throw error }
                let problem = FeedProblem(error)
                guard problem.kind.isTransient, tries < delays.count else {
                    log(problem.logLine(route: route))
                    throw error
                }
                try await sleep(delays[tries])
                tries += 1
            }
        }
    }

    /// The body of a 200 answer, or the status error.
    public static func body(_ data: Data, _ response: URLResponse?) throws -> Data {
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        guard http.statusCode == 200 else { throw FeedHTTPStatus(code: http.statusCode) }
        return data
    }

    /// The route in a log line: the path without its query (a search's words stay out of the log).
    public static func route(_ path: String) -> String {
        String(path.split(separator: "?", maxSplits: 1, omittingEmptySubsequences: false)[0])
    }
}

/// Where final feed failures are logged (the app points it at its log file).
public enum FeedLog {
    public static var sink: (String) -> Void = { _ in }
    public static func write(_ line: String) { sink(line) }
}

/// What `DayView` shows: only the matching rows while a query is typed.
public enum SearchOnly {
    /// True while the query has something in it (whitespace alone doesn't count).
    public static func active(_ query: String) -> Bool {
        !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    public static func matches(_ text: String, _ query: String) -> Bool {
        !active(query) || text.localizedCaseInsensitiveContains(query.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    /// "1 match" / "N matches".
    public static func countLabel(_ n: Int) -> String { n == 1 ? "1 match" : "\(n) matches" }
}
