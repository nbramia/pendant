import Foundation

/// Decides which pages to store and how far to ack the pendant.
///
/// Rules (matching Omi's drain engine):
/// - Ack (`DeleteFlashPage`, frees every page <= index) only after an fsync,
///   and only up to the highest page received from a *batch* drain. The
///   pendant's batch stream resumes from its own cursor, so pages before it
///   were already delivered in an earlier drain.
/// - Never ack because of live-stream pages: live delivery doesn't advance
///   the pendant's batch cursor, so acking a live index could free older pages
///   that haven't been drained yet.
/// - Pages at or below the ack watermark, or already stored this connection,
///   are duplicates and aren't stored again.
public struct AckTracker: Equatable {
    public private(set) var ackedThrough: Int64 = -1
    private var stored = Set<Int64>()
    private var pendingBatchMax: Int64 = -1

    public init() {}

    /// Applies a status report. `localWatermark` is our own last ack for this
    /// storage session (the pendant's `oldest` can lag behind it).
    public mutating func applyStatus(oldest: Int64, newest: Int64, localWatermark: Int64) {
        let empty = newest < 0 || newest >= 0xFFFF_0000 || newest < oldest
        let floor = empty ? -1 : oldest - 1
        let local = empty ? -1 : min(localWatermark, newest)
        ackedThrough = max(floor, local)
        stored = stored.filter { $0 > ackedThrough }
        if pendingBatchMax <= ackedThrough { pendingBatchMax = -1 }
    }

    public func shouldStore(_ index: Int64) -> Bool {
        index > ackedThrough && !stored.contains(index)
    }

    /// Call after the page was appended to the spool (not yet fsync'd).
    public mutating func didStore(_ index: Int64, batch: Bool) {
        stored.insert(index)
        if batch { pendingBatchMax = max(pendingBatchMax, index) }
    }

    /// A batch page we didn't store again because the live stream already
    /// delivered it. It's in the spool, so the next commit may ack it.
    public mutating func didSeeStoredBatchPage(_ index: Int64) {
        if stored.contains(index) { pendingBatchMax = max(pendingBatchMax, index) }
    }

    /// Call after a successful fsync. Returns the index to ack, if any.
    public mutating func commit() -> Int64? {
        guard pendingBatchMax > ackedThrough else { return nil }
        ackedThrough = pendingBatchMax
        pendingBatchMax = -1
        stored = stored.filter { $0 > ackedThrough }
        return ackedThrough
    }

    /// True when the pendant holds sealed pages we haven't acked. The newest
    /// page is still open, so only pages up to newest-1 count.
    public func needsDrain(newest: Int64) -> Bool {
        newest < 0xFFFF_0000 && newest - 1 > ackedThrough
    }

    public mutating func reset() {
        stored.removeAll()
        pendingBatchMax = -1
    }
}
