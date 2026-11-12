import XCTest
@testable import PendantCore

final class RecordingKindTests: XCTestCase {
    func testFeedValuesMapToKinds() {
        XCTAssertEqual(RecordingKind(feed: "conversation"), .conversation)
        XCTAssertEqual(RecordingKind(feed: "media"), .media)
        XCTAssertEqual(RecordingKind(feed: "fragment"), .fragment)
    }

    func testMissingOrUnknownReadsAsConversation() {
        XCTAssertEqual(RecordingKind(feed: nil), .conversation)
        XCTAssertEqual(RecordingKind(feed: ""), .conversation)
        XCTAssertEqual(RecordingKind(feed: "podcast"), .conversation)
    }

    func testBadgesAndReviewFilter() {
        XCTAssertNil(RecordingKind.conversation.badge)
        XCTAssertEqual(RecordingKind.media.badge, "Media")
        XCTAssertEqual(RecordingKind.fragment.badge, "Fragment")
        XCTAssertFalse(RecordingKind.conversation.needsReview)
        XCTAssertTrue(RecordingKind.media.needsReview)
        XCTAssertTrue(RecordingKind.fragment.needsReview)
    }
}
