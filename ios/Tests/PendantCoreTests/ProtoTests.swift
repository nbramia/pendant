import XCTest
@testable import PendantCore

final class ProtoTests: XCTestCase {
    func testVarintRoundTrip() throws {
        for v: UInt64 in [0, 1, 127, 128, 300, 1 << 35, .max] {
            let f = try Proto.parse(Proto.varintField(3, v))
            XCTAssertEqual(f.uint(3), v)
        }
    }

    func testNegativeInt32IsSignExtended() throws {
        // StorageState.newest_flash_page is int32; -1 (fresh wipe) arrives as a 10-byte varint.
        let f = try Proto.parse(Proto.varintField(2, UInt64(bitPattern: -1)))
        XCTAssertEqual(f.int(2), -1)
    }

    func testNestedMessage() throws {
        let inner = Proto.varintField(1, 42) + Proto.bytesField(2, Array("hi".utf8))
        let f = try Proto.parse(Proto.bytesField(5, inner))
        XCTAssertEqual(f.message(5)?.uint(1), 42)
        XCTAssertEqual(f.message(5)?.bytes(2), Array("hi".utf8))
    }

    func testTruncatedInputThrows() {
        XCTAssertThrowsError(try Proto.parse([0x0A, 0x05, 0x01]))
    }
}
