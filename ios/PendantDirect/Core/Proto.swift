import Foundation

/// Minimal protobuf wire-format helpers. The pendant protocol only needs
/// varints and length-delimited fields, so a full protobuf runtime isn't worth it.
enum Proto {
    static func varint(_ value: UInt64) -> [UInt8] {
        var v = value
        var out: [UInt8] = []
        while v > 0x7F {
            out.append(UInt8(v & 0x7F) | 0x80)
            v >>= 7
        }
        out.append(UInt8(v))
        return out
    }

    static func tag(_ field: Int, _ wire: Int) -> [UInt8] {
        varint(UInt64(field << 3 | wire))
    }

    static func varintField(_ field: Int, _ value: UInt64) -> [UInt8] {
        tag(field, 0) + varint(value)
    }

    static func bytesField(_ field: Int, _ bytes: [UInt8]) -> [UInt8] {
        tag(field, 2) + varint(UInt64(bytes.count)) + bytes
    }

    enum Value {
        case varint(UInt64)
        case bytes([UInt8])
        case fixed64(UInt64)
        case fixed32(UInt32)
    }

    struct ParseError: Error {}

    /// Parses one message level into (field number, value) pairs, in wire order.
    static func parse(_ data: [UInt8]) throws -> Fields {
        var pos = 0
        var out: [(Int, Value)] = []
        func readVarint() throws -> UInt64 {
            var result: UInt64 = 0
            var shift: UInt64 = 0
            while true {
                guard pos < data.count, shift < 64 else { throw ParseError() }
                let b = data[pos]
                pos += 1
                result |= UInt64(b & 0x7F) << shift
                if b & 0x80 == 0 { return result }
                shift += 7
            }
        }
        while pos < data.count {
            let key = try readVarint()
            let field = Int(key >> 3)
            switch key & 7 {
            case 0:
                out.append((field, .varint(try readVarint())))
            case 1:
                guard pos + 8 <= data.count else { throw ParseError() }
                var v: UInt64 = 0
                for i in 0..<8 { v |= UInt64(data[pos + i]) << (8 * UInt64(i)) }
                pos += 8
                out.append((field, .fixed64(v)))
            case 2:
                let len = Int(try readVarint())
                guard len >= 0, pos + len <= data.count else { throw ParseError() }
                out.append((field, .bytes(Array(data[pos..<pos + len]))))
                pos += len
            case 5:
                guard pos + 4 <= data.count else { throw ParseError() }
                var v: UInt32 = 0
                for i in 0..<4 { v |= UInt32(data[pos + i]) << (8 * UInt32(i)) }
                pos += 4
                out.append((field, .fixed32(v)))
            default:
                throw ParseError()
            }
        }
        return Fields(items: out)
    }
}

struct Fields {
    var items: [(Int, Proto.Value)]

    func uint(_ field: Int) -> UInt64? {
        for (f, v) in items where f == field { if case .varint(let x) = v { return x } }
        return nil
    }

    /// int32 fields are sign-extended to 64 bits on the wire.
    func int(_ field: Int) -> Int64? {
        uint(field).map { Int64(bitPattern: $0) }
    }

    func bytes(_ field: Int) -> [UInt8]? {
        for (f, v) in items where f == field { if case .bytes(let x) = v { return x } }
        return nil
    }

    func message(_ field: Int) -> Fields? {
        bytes(field).flatMap { try? Proto.parse($0) }
    }
}
