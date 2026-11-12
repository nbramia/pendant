import XCTest
@testable import PendantCore

final class FeedClientTests: XCTestCase {
    private func kind(_ e: Error) -> FeedErrorKind { FeedClient.classify(e) }

    func testClassification() {
        XCTAssertEqual(kind(URLError(.notConnectedToInternet)), .offline)
        XCTAssertEqual(kind(URLError(.cannotFindHost)), .offline)
        XCTAssertEqual(kind(URLError(.cannotConnectToHost)), .offline)
        XCTAssertEqual(kind(URLError(.dnsLookupFailed)), .offline)
        XCTAssertEqual(kind(URLError(.timedOut)), .busy)
        XCTAssertEqual(kind(URLError(.networkConnectionLost)), .busy)
        for code in [500, 502, 503, 504] { XCTAssertEqual(kind(FeedHTTPStatus(code: code)), .busy, "\(code)") }
        XCTAssertEqual(kind(FeedHTTPStatus(code: 409)), .client)
        XCTAssertEqual(kind(FeedHTTPStatus(code: 404)), .client)
        XCTAssertEqual(kind(FeedHTTPStatus(code: 401)), .rejected)
        XCTAssertEqual(kind(DecodingError.dataCorrupted(.init(codingPath: [], debugDescription: "x"))), .decode)
        XCTAssertEqual(kind(URLError(.badURL)), .client)
    }

    func testOnlyOfflineAndBusyAreTransient() {
        XCTAssertTrue(FeedErrorKind.offline.isTransient)
        XCTAssertTrue(FeedErrorKind.busy.isTransient)
        XCTAssertFalse(FeedErrorKind.decode.isTransient)
        XCTAssertFalse(FeedErrorKind.client.isTransient)
        XCTAssertFalse(FeedErrorKind.rejected.isTransient)
    }

    func testMessagesSayWhatWentWrong() {
        XCTAssertEqual(FeedProblem(URLError(.notConnectedToInternet)).message, "No connection to the server (check Tailscale)")
        XCTAssertEqual(FeedProblem(FeedHTTPStatus(code: 502)).message, "The server is busy, retrying…")
        XCTAssertEqual(FeedProblem(URLError(.timedOut)).message, "The server is busy, retrying…")
        XCTAssertEqual(FeedProblem(DecodingError.dataCorrupted(.init(codingPath: [], debugDescription: "x"))).message,
                       "Couldn't read the server's reply (app needs an update?)")
        XCTAssertFalse(FeedProblem(FeedHTTPStatus(code: 502)).message.contains("Tailscale"))
    }

    func testLogLineHasRouteKindStatusAndError() {
        let p = FeedProblem(FeedHTTPStatus(code: 502))
        XCTAssertTrue(p.logLine(route: "day/2026-11-08").hasPrefix("feed day/2026-11-08 failed: busy 502 "))
        let o = FeedProblem(URLError(.notConnectedToInternet))
        XCTAssertTrue(o.logLine(route: "days").hasPrefix("feed days failed: offline - "))
        XCTAssertEqual(FeedClient.route("search?q=secret%20words"), "search")
    }

    // MARK: retry policy, with a fake transport

    private final class Fake {
        var results: [Result<String, Error>]
        var calls = 0
        var slept: [Double] = []
        var logs: [String] = []
        init(_ r: [Result<String, Error>]) { results = r }
        func attempt() async throws -> String {
            calls += 1
            return try results.removeFirst().get()
        }
        func run(route: String = "r") async throws -> String {
            try await FeedClient.run(route: route, sleep: { self.slept.append($0) }, log: { self.logs.append($0) }) {
                try await self.attempt()
            }
        }
    }

    func testRetriesTransientFailuresWithBackoffThenSucceeds() async throws {
        let f = Fake([.failure(FeedHTTPStatus(code: 502)), .failure(URLError(.timedOut)), .success("ok")])
        let v = try await f.run()
        XCTAssertEqual(v, "ok")
        XCTAssertEqual(f.calls, 3)
        XCTAssertEqual(f.slept, [0.5, 1.5])
        XCTAssertTrue(f.logs.isEmpty, "a request that recovered logs nothing")
    }

    func testGivesUpAfterThreeRetriesAndLogsOnce() async {
        let f = Fake(Array(repeating: .failure(FeedHTTPStatus(code: 503)), count: 6))
        do { _ = try await f.run(route: "day/2026-11-08"); XCTFail("should throw") }
        catch { XCTAssertEqual((error as? FeedHTTPStatus)?.code, 503) }
        XCTAssertEqual(f.calls, 4, "the first try plus 3 retries")
        XCTAssertEqual(f.slept, [0.5, 1.5, 4])
        XCTAssertEqual(f.logs.count, 1)
        XCTAssertTrue(f.logs[0].hasPrefix("feed day/2026-11-08 failed: busy 503 "))
    }

    func testClientAndDecodeErrorsAreNotRetried() async {
        for e: Error in [FeedHTTPStatus(code: 409), FeedHTTPStatus(code: 404),
                         DecodingError.dataCorrupted(.init(codingPath: [], debugDescription: "x"))] {
            let f = Fake([.failure(e), .success("never")])
            do { _ = try await f.run(); XCTFail("should throw") } catch {}
            XCTAssertEqual(f.calls, 1)
            XCTAssertTrue(f.slept.isEmpty)
            XCTAssertEqual(f.logs.count, 1)
        }
    }

    func testOfflineIsRetriedToo() async throws {
        let f = Fake([.failure(URLError(.notConnectedToInternet)), .success("back")])
        let v = try await f.run()
        XCTAssertEqual(v, "back")
        XCTAssertEqual(f.slept, [0.5])
    }

    func testCancellationIsNeitherRetriedNorLogged() async {
        let f = Fake([.failure(CancellationError())])
        do { _ = try await f.run(); XCTFail("should throw") } catch { XCTAssertTrue(error is CancellationError) }
        XCTAssertEqual(f.calls, 1)
        XCTAssertTrue(f.logs.isEmpty)
    }

    func testBodyAcceptsOnly200() throws {
        let url = URL(string: "http://x")!
        func resp(_ c: Int) -> HTTPURLResponse { HTTPURLResponse(url: url, statusCode: c, httpVersion: nil, headerFields: nil)! }
        XCTAssertEqual(try FeedClient.body(Data([1]), resp(200)), Data([1]))
        XCTAssertThrowsError(try FeedClient.body(Data(), resp(502))) { XCTAssertEqual(($0 as? FeedHTTPStatus)?.code, 502) }
        XCTAssertThrowsError(try FeedClient.body(Data(), nil))
    }

    // MARK: search-only homepage

    func testSearchOnlyFiltering() {
        XCTAssertFalse(SearchOnly.active(""))
        XCTAssertFalse(SearchOnly.active("   "))
        XCTAssertTrue(SearchOnly.active("lunch"))
        XCTAssertTrue(SearchOnly.matches("anything", ""))
        XCTAssertTrue(SearchOnly.matches("We had LUNCH today", "lunch"))
        XCTAssertTrue(SearchOnly.matches("We had lunch today", " lunch "))
        XCTAssertFalse(SearchOnly.matches("We had dinner", "lunch"))
        XCTAssertEqual(SearchOnly.countLabel(1), "1 match")
        XCTAssertEqual(SearchOnly.countLabel(0), "0 matches")
        XCTAssertEqual(SearchOnly.countLabel(7), "7 matches")
    }
}
