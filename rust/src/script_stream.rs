//! State script chunk numbering, acknowledged baselines, and transport retry state.
//!
//! Controller streams follow ow174/game/statescript.py. Body streams follow
//! ow174/game/script/driver.py and leave graph projection to the caller.

use std::{
    collections::{BTreeMap, BTreeSet},
    error::Error,
    fmt,
    time::Duration,
};

use crate::bits::BitWriter;
use crate::commands::{FRAME_HALF, frame_distance};
use crate::game_clock::MAX_LEASE_FRAME;
use crate::script_wire::{WireError, chunk, owner_ack};
use crate::world::EntityUpdate;

pub const OWNER_SETTLE_FRAMES: u32 = 16;
const OWNER_ACK_EVERY: Duration = Duration::from_millis(100);

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum StreamError {
    Wire(WireError),
    ChunkExhausted,
    CommandLimit(u32),
    CommandWrap { previous: u32, incoming: u32 },
    BodyRetired,
    BaselinePending,
    OwnerOrder,
}

impl fmt::Display for StreamError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Wire(error) => error.fmt(f),
            Self::ChunkExhausted => f.write_str("state script chunk numbering exhausted"),
            Self::CommandLimit(frame) => write!(f, "command frame {frame} exceeds the lease limit"),
            Self::CommandWrap { previous, incoming } => {
                write!(f, "command frame wrapped from {previous} to {incoming}")
            }
            Self::BodyRetired => f.write_str("state script body has retired"),
            Self::BaselinePending => f.write_str("state script baseline awaits acknowledgement"),
            Self::OwnerOrder => f.write_str("owner delta is outside the current stream phase"),
        }
    }
}

impl Error for StreamError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Wire(error) => Some(error),
            _ => None,
        }
    }
}

impl From<WireError> for StreamError {
    fn from(error: WireError) -> Self {
        Self::Wire(error)
    }
}

#[derive(Debug, PartialEq)]
pub struct ScriptChunk {
    pub update: EntityUpdate,
    pub last: u32,
    /// Retry on loss by default. Body streams also retain the exact initial
    /// owner delta after its first successful transmission.
    pub resend: bool,
}

fn next(last: u32) -> Result<u32, StreamError> {
    last.checked_add(1).ok_or(StreamError::ChunkExhausted)
}

fn update(
    entity: u32,
    payload: &BitWriter,
    first: u32,
    last: u32,
    resend: bool,
) -> Result<ScriptChunk, StreamError> {
    let span = last.checked_sub(first).ok_or(StreamError::ChunkExhausted)?;
    Ok(ScriptChunk {
        update: EntityUpdate {
            entity,
            chunk: Some(chunk(payload, first, span)?),
            ..EntityUpdate::default()
        },
        last,
        resend,
    })
}

fn validate_frame(previous: u32, incoming: u32) -> Result<(), StreamError> {
    if incoming > MAX_LEASE_FRAME {
        return Err(StreamError::CommandLimit(incoming));
    }
    if incoming < previous && frame_distance(incoming, previous) < FRAME_HALF {
        return Err(StreamError::CommandWrap { previous, incoming });
    }
    Ok(())
}

#[derive(Debug)]
pub struct Stream {
    entity: u32,
    last: u32,
    delivered: u32,
    data_last: u32,
    cmfd: u32,
    next_ack: Duration,
}

impl Stream {
    pub fn new(entity: u32) -> Self {
        Self {
            entity,
            last: 0,
            delivered: 0,
            data_last: 0,
            cmfd: 0,
            next_ack: Duration::ZERO,
        }
    }

    pub fn delivered(&self) -> u32 {
        self.delivered
    }

    pub fn command_frame(&self) -> u32 {
        self.cmfd
    }

    pub fn latest_data(&self) -> u32 {
        self.data_last
    }

    pub fn data_in_flight(&self) -> bool {
        self.delivered < self.data_last
    }

    pub fn full(&mut self, payload: &BitWriter) -> Result<ScriptChunk, StreamError> {
        let last = next(self.last)?;
        let result = update(self.entity, payload, 0, last, true)?;
        self.last = last;
        self.data_last = last;
        Ok(result)
    }

    /// Build an owner's full frame with a strictly increasing CmFD. A writer
    /// error leaves both frame counters available for the next attempt.
    pub fn full_owner(
        &mut self,
        newest: u32,
        encode: impl FnOnce(u32) -> Result<BitWriter, WireError>,
    ) -> Result<ScriptChunk, StreamError> {
        validate_frame(self.cmfd, newest)?;
        let cmfd = newest.max(self.cmfd + 1);
        validate_frame(self.cmfd, cmfd)?;
        let result = self.full(&encode(cmfd)?)?;
        self.cmfd = cmfd;
        Ok(result)
    }

    /// After the data has arrived, ACK-only chunks may cumulatively cover
    /// earlier ACK-only chunks that the peer has missed.
    pub fn owner_ack(
        &mut self,
        now: Duration,
        newest: Option<u32>,
    ) -> Result<Option<ScriptChunk>, StreamError> {
        if self.data_last == 0 || now < self.next_ack {
            return Ok(None);
        }
        let next_ack = now.saturating_add(OWNER_ACK_EVERY);
        let Some(newest) = newest.filter(|_| !self.data_in_flight()) else {
            self.next_ack = next_ack;
            return Ok(None);
        };
        validate_frame(self.cmfd, newest)?;
        if newest <= self.cmfd {
            self.next_ack = next_ack;
            return Ok(None);
        }
        let first = next(self.delivered)?;
        let last = next(self.last)?;
        let result = update(self.entity, &owner_ack(newest)?, first, last, false)?;
        self.last = last;
        self.cmfd = newest;
        self.next_ack = next_ack;
        Ok(Some(result))
    }

    pub fn arrived(&mut self, last: u32) -> bool {
        if last <= self.delivered || last > self.last {
            return false;
        }
        self.delivered = last;
        true
    }
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum ChangeKey {
    Instance(u16),
    Variable(u16, u32),
    State(u16, u16),
    VolleyIntroduction(u16, u16),
}

impl ChangeKey {
    fn carries_forward(self) -> bool {
        !matches!(self, Self::VolleyIntroduction(..))
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DeltaKind {
    Observer { resend: bool },
    InitialOwner(u32),
    Correction(u32),
}

impl DeltaKind {
    fn command_frame(self) -> Option<u32> {
        match self {
            Self::Observer { .. } => None,
            Self::InitialOwner(frame) | Self::Correction(frame) => Some(frame),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum PendingKind {
    Full,
    Delta(DeltaKind),
}

#[derive(Debug)]
struct Pending<S> {
    snapshot: S,
    keys: BTreeSet<ChangeKey>,
    kind: PendingKind,
    sent: bool,
}

impl<S> Pending<S> {
    fn retry(&self) -> bool {
        match self.kind {
            PendingKind::Full => true,
            PendingKind::Delta(DeltaKind::Observer { resend }) => resend,
            PendingKind::Delta(DeltaKind::InitialOwner(_)) => self.sent,
            PendingKind::Delta(DeltaKind::Correction(_)) => false,
        }
    }
}

pub struct SentOwner<'a, S> {
    pub command_frame: u32,
    pub snapshot: &'a S,
    pub changes: &'a BTreeSet<ChangeKey>,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct BodyDelivery {
    pub retry: bool,
    pub advanced: bool,
    /// An individually transmitted owner report is resolved independently of
    /// cumulative baseline changes. True means its own packet arrived.
    pub owner_report: Option<bool>,
}

#[derive(Debug)]
pub struct BodyStream<S> {
    entity: u32,
    create_delivered: bool,
    retired: bool,
    last: u32,
    cmfd: u32,
    first_owner_frame: Option<u32>,
    base: Option<S>,
    base_last: u32,
    pending: BTreeMap<u32, Pending<S>>,
    sent_reports: BTreeSet<u32>,
    lost_keys: BTreeMap<ChangeKey, u32>,
    latest_lost: Option<S>,
    initial_owner: Option<u32>,
}

impl<S> BodyStream<S> {
    pub fn new(entity: u32) -> Self {
        Self {
            entity,
            create_delivered: false,
            retired: false,
            last: 0,
            cmfd: 0,
            first_owner_frame: None,
            base: None,
            base_last: 0,
            pending: BTreeMap::new(),
            sent_reports: BTreeSet::new(),
            lost_keys: BTreeMap::new(),
            latest_lost: None,
            initial_owner: None,
        }
    }

    pub fn create_arrived(&mut self) {
        self.create_delivered = true;
    }

    pub fn ready_to_send(&self, last: u32) -> bool {
        self.create_delivered && !self.retired && self.pending.contains_key(&last)
    }

    pub fn last(&self) -> u32 {
        self.last
    }

    pub fn command_frame(&self) -> u32 {
        self.cmfd
    }

    pub fn base(&self) -> Option<(u32, &S)> {
        self.base
            .as_ref()
            .map(|snapshot| (self.base_last, snapshot))
    }

    pub fn pending(&self) -> impl Iterator<Item = (u32, &S, &BTreeSet<ChangeKey>)> {
        self.pending
            .iter()
            .map(|(&last, item)| (last, &item.snapshot, &item.keys))
    }

    pub fn latest_snapshot(&self) -> Option<&S> {
        self.pending
            .get(&self.last)
            .map(|item| &item.snapshot)
            .or(self.latest_lost.as_ref())
    }

    pub fn touched(&self) -> BTreeSet<ChangeKey> {
        self.lost_keys
            .keys()
            .chain(self.pending.values().flat_map(|item| &item.keys))
            .copied()
            .filter(|key| key.carries_forward())
            .collect()
    }

    /// The baseline may be prepared before the body's create arrives. Its
    /// record becomes sendable only after the create acknowledgement.
    pub fn full(&mut self, payload: &BitWriter, snapshot: S) -> Result<ScriptChunk, StreamError> {
        if self.retired {
            return Err(StreamError::BodyRetired);
        }
        let last = next(self.last)?;
        let result = update(self.entity, payload, 0, last, true)?;
        self.pending.insert(
            last,
            Pending {
                snapshot,
                keys: BTreeSet::new(),
                kind: PendingKind::Full,
                sent: false,
            },
        );
        self.last = last;
        self.latest_lost = None;
        Ok(result)
    }

    /// Find the kind of owner update due at this command frame. A caller may
    /// refresh an unsent projection at the same frame, or replace its initial
    /// projection before the old one reaches the transport.
    pub fn owner_kind(
        &self,
        frame: u32,
        refresh_unsent: bool,
        replace_initial: bool,
    ) -> Result<Option<DeltaKind>, StreamError> {
        if self.retired || self.base.is_none() {
            return Ok(None);
        }
        validate_frame(self.cmfd, frame)?;
        if frame == 0
            || frame < self.cmfd
            || self.initial_owner.is_some_and(|last| self.retry(last))
            || (frame == self.cmfd && !refresh_unsent)
        {
            return Ok(None);
        }
        let Some(first) = self.first_owner_frame.filter(|_| !replace_initial) else {
            return Ok(Some(DeltaKind::InitialOwner(frame)));
        };
        Ok((frame.saturating_sub(first) >= OWNER_SETTLE_FRAMES)
            .then_some(DeltaKind::Correction(frame)))
    }

    pub fn delta(
        &mut self,
        payload: &BitWriter,
        snapshot: S,
        keys: BTreeSet<ChangeKey>,
        kind: DeltaKind,
    ) -> Result<ScriptChunk, StreamError> {
        if self.retired {
            return Err(StreamError::BodyRetired);
        }
        if self.base.is_none() {
            return Err(StreamError::BaselinePending);
        }
        if let Some(frame) = kind.command_frame() {
            validate_frame(self.cmfd, frame)?;
            if frame == 0 {
                return Err(WireError::ZeroCommandFrame.into());
            }
            let replacement = matches!(kind, DeltaKind::InitialOwner(_));
            if self.owner_kind(frame, true, replacement)? != Some(kind) {
                return Err(StreamError::OwnerOrder);
            }
        }
        let last = next(self.last)?;
        let first = next(self.base_last)?;
        let resend = matches!(kind, DeltaKind::Observer { resend: true });
        let result = update(self.entity, payload, first, last, resend)?;
        self.pending.insert(
            last,
            Pending {
                snapshot,
                keys,
                kind: PendingKind::Delta(kind),
                sent: false,
            },
        );
        if let Some(frame) = kind.command_frame() {
            self.cmfd = frame;
        }
        if let DeltaKind::InitialOwner(frame) = kind {
            self.first_owner_frame = Some(frame);
            self.initial_owner = Some(last);
        }
        self.last = last;
        self.latest_lost = None;
        Ok(result)
    }

    /// Remove a superseded queued delta that never reached the transport.
    pub fn discard_unsent(&mut self, last: u32) -> bool {
        if !self
            .pending
            .get(&last)
            .is_some_and(|item| !item.sent && matches!(item.kind, PendingKind::Delta(_)))
        {
            return false;
        }
        self.pending.remove(&last);
        if self.initial_owner == Some(last) {
            self.initial_owner = None;
        }
        true
    }

    /// Call after the selected datagram was actually sent. The first owner
    /// transmission exposes the snapshot and keys for its own report.
    pub fn sent(&mut self, last: u32) -> Option<SentOwner<'_, S>> {
        if !self.ready_to_send(last) {
            return None;
        }
        let item = self.pending.get_mut(&last)?;
        if item.sent {
            return None;
        }
        item.sent = true;
        let PendingKind::Delta(kind) = item.kind else {
            return None;
        };
        let command_frame = kind.command_frame()?;
        self.sent_reports.insert(last);
        Some(SentOwner {
            command_frame,
            snapshot: &item.snapshot,
            changes: &item.keys,
        })
    }

    pub fn retry(&self, last: u32) -> bool {
        !self.retired && self.pending.get(&last).is_some_and(Pending::retry)
    }

    pub fn arrived(&mut self, last: u32) -> BodyDelivery {
        if !self.create_delivered || self.retired {
            return BodyDelivery::default();
        }
        let owner_report = self.sent_reports.remove(&last).then_some(true);
        let mut delivery = BodyDelivery {
            owner_report,
            ..BodyDelivery::default()
        };
        let Some(item) = self.pending.remove(&last) else {
            return delivery;
        };
        self.base = Some(item.snapshot);
        self.base_last = last;
        self.pending.retain(|&number, _| number > last);
        self.lost_keys.retain(|_, number| *number > last);
        if self.initial_owner.is_some_and(|initial| initial <= last) {
            self.initial_owner = None;
        }
        delivery.advanced = true;
        delivery
    }

    pub fn lost(&mut self, last: u32) -> BodyDelivery {
        if self.retry(last) {
            return BodyDelivery {
                retry: true,
                ..BodyDelivery::default()
            };
        }
        let owner_report = self.sent_reports.remove(&last).then_some(false);
        let delivery = BodyDelivery {
            owner_report,
            ..BodyDelivery::default()
        };
        if self
            .pending
            .get(&last)
            .is_some_and(|item| matches!(item.kind, PendingKind::Delta(DeltaKind::Correction(_))))
        {
            let item = self
                .pending
                .remove(&last)
                .expect("checked pending owner delta");
            for key in item.keys.into_iter().filter(|key| key.carries_forward()) {
                let number = self.lost_keys.entry(key).or_default();
                *number = (*number).max(last);
            }
            if last == self.last {
                self.latest_lost = Some(item.snapshot);
            }
        }
        // A lost observer delta remains in the cumulative projection until a
        // later acknowledgement. Its instances may still need removal.
        delivery
    }

    pub fn retire(&mut self) {
        self.retired = true;
        self.pending.clear();
        self.sent_reports.clear();
        self.lost_keys.clear();
        self.latest_lost = None;
        self.initial_owner = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const CONTROLLER: u32 = 0xa000_0100;
    const BODY: u32 = 0xa000_0101;

    fn payload() -> BitWriter {
        let mut out = BitWriter::new();
        out.bits(0x15, 5).unwrap();
        out
    }

    fn check_chunk(chunk: &ScriptChunk, last: u32, resend: bool, bits: usize, hex: &str) {
        assert_eq!((chunk.last, chunk.resend), (last, resend));
        let chunk = chunk.update.chunk.as_ref().unwrap();
        let encoded: String = chunk
            .as_bytes()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        assert_eq!((chunk.bit_len(), encoded.as_str()), (bits, hex));
    }

    #[test]
    fn controller_data_precedes_ack_only_chunks_and_ranges_match_python() {
        let mut stream = Stream::new(CONTROLLER);
        let at = Duration::from_millis;
        assert!(stream.owner_ack(at(0), Some(100)).unwrap().is_none());
        let first = stream
            .full_owner(100, |cmfd| {
                assert_eq!(cmfd, 100);
                Ok(payload())
            })
            .unwrap();
        check_chunk(&first, 1, true, 41, "000004505001");
        assert_eq!(first.update.entity, CONTROLLER);
        let second = stream
            .full_owner(100, |cmfd| {
                assert_eq!(cmfd, 101);
                Ok(payload())
            })
            .unwrap();
        check_chunk(&second, 2, true, 41, "000008505001");
        assert!(stream.arrived(first.last));
        assert!(stream.data_in_flight());
        assert!(stream.owner_ack(at(0), Some(104)).unwrap().is_none());
        assert!(stream.arrived(second.last));
        assert!(!stream.data_in_flight());
        assert!(stream.owner_ack(at(99), Some(105)).unwrap().is_none());
        let ack = stream.owner_ack(at(100), Some(105)).unwrap().unwrap();
        check_chunk(&ack, 3, false, 67, "060000f021690e0000");
        assert!(stream.owner_ack(at(199), Some(107)).unwrap().is_none());
        let cumulative = stream.owner_ack(at(200), Some(107)).unwrap().unwrap();
        check_chunk(&cumulative, 4, false, 67, "060004f0216b0e0000");
        assert!(stream.arrived(cumulative.last));
        assert!(!stream.arrived(ack.last));
        assert!(!stream.arrived(9));
        assert_eq!(stream.delivered(), 4);
        let later = stream.owner_ack(at(300), Some(108)).unwrap().unwrap();
        check_chunk(&later, 5, false, 67, "0a0000f0216c0e0000");
    }

    #[test]
    fn writers_and_lease_boundaries_leave_the_existing_stream_usable() {
        let mut stream = Stream::new(CONTROLLER);
        assert_eq!(
            stream.full_owner(42, |_| Err(WireError::InvalidValue)),
            Err(StreamError::Wire(WireError::InvalidValue))
        );
        assert_eq!(stream.command_frame(), 0);
        let full = stream
            .full_owner(MAX_LEASE_FRAME, |_| Ok(payload()))
            .unwrap();
        assert_eq!(full.last, 1);
        assert!(stream.arrived(full.last));
        assert_eq!(
            stream.owner_ack(Duration::ZERO, Some(MAX_LEASE_FRAME + 1)),
            Err(StreamError::CommandLimit(MAX_LEASE_FRAME + 1))
        );
        assert_eq!(
            stream.owner_ack(Duration::ZERO, Some(1)),
            Err(StreamError::CommandWrap {
                previous: MAX_LEASE_FRAME,
                incoming: 1
            })
        );
        assert_eq!(
            stream.full_owner(MAX_LEASE_FRAME, |_| unreachable!()),
            Err(StreamError::CommandLimit(MAX_LEASE_FRAME + 1))
        );
        assert_eq!(stream.command_frame(), MAX_LEASE_FRAME);
        assert_eq!(stream.delivered(), 1);
        assert_eq!(stream.full(&payload()).unwrap().last, 2);
        stream.last = u32::MAX;
        assert_eq!(stream.full(&payload()), Err(StreamError::ChunkExhausted));

        let data = payload();
        let mut body = BodyStream::new(BODY);
        body.create_arrived();
        let full = body.full(&data, 7).unwrap();
        body.arrived(full.last);
        let initial = body
            .delta(
                &data,
                8,
                BTreeSet::new(),
                DeltaKind::InitialOwner(MAX_LEASE_FRAME),
            )
            .unwrap();
        body.sent(initial.last);
        body.arrived(initial.last);
        assert_eq!(
            body.owner_kind(1, false, false),
            Err(StreamError::CommandWrap {
                previous: MAX_LEASE_FRAME,
                incoming: 1,
            })
        );
        assert_eq!(
            body.delta(
                &data,
                9,
                BTreeSet::new(),
                DeltaKind::Correction(MAX_LEASE_FRAME + 1),
            ),
            Err(StreamError::CommandLimit(MAX_LEASE_FRAME + 1))
        );
        assert_eq!(body.command_frame(), MAX_LEASE_FRAME);
        assert_eq!(body.base(), Some((initial.last, &8)));
    }

    #[test]
    fn body_full_waits_for_create_and_the_first_owner_delta_retries_unchanged() {
        let mut stream = BodyStream::new(BODY);
        let data = payload();
        let full = stream.full(&data, 100).unwrap();
        check_chunk(&full, 1, true, 41, "000004505001");
        assert!(!stream.ready_to_send(full.last));
        assert!(!stream.arrived(full.last).advanced);
        assert_eq!(stream.owner_kind(101, false, false).unwrap(), None);
        assert_eq!(
            stream.delta(&data, 101, BTreeSet::new(), DeltaKind::InitialOwner(101)),
            Err(StreamError::BaselinePending)
        );
        stream.create_arrived();
        assert!(stream.ready_to_send(full.last));
        assert!(stream.sent(full.last).is_none());
        assert!(stream.lost(full.last).retry);
        assert!(stream.arrived(full.last).advanced);
        assert_eq!(stream.base(), Some((1, &100)));
        let change = ChangeKey::State(11, 10);
        let initial_kind = stream.owner_kind(101, false, false).unwrap().unwrap();
        assert_eq!(initial_kind, DeltaKind::InitialOwner(101));
        let original = stream
            .delta(&data, 101, BTreeSet::from([change]), initial_kind)
            .unwrap();
        check_chunk(&original, 2, false, 41, "040000505001");
        assert_eq!(stream.owner_kind(101, false, true).unwrap(), None);
        assert_eq!(
            stream.owner_kind(101, true, true).unwrap(),
            Some(initial_kind)
        );
        let replacement = stream
            .delta(&data, 111, BTreeSet::from([change]), initial_kind)
            .unwrap();
        assert!(stream.discard_unsent(original.last));
        let sent = stream.sent(replacement.last).unwrap();
        assert_eq!((sent.command_frame, *sent.snapshot), (101, 111));
        assert!(sent.changes.contains(&change));
        assert!(!stream.discard_unsent(replacement.last));
        assert!(stream.lost(replacement.last).retry);
        assert_eq!(stream.latest_snapshot(), Some(&111));
        assert!(stream.sent(replacement.last).is_none());
        assert_eq!(stream.owner_kind(117, true, true).unwrap(), None);
        let delivered = stream.arrived(replacement.last);
        assert_eq!(
            delivered,
            BodyDelivery {
                retry: false,
                advanced: true,
                owner_report: Some(true)
            }
        );
        assert_eq!(stream.base(), Some((replacement.last, &111)));
        assert_eq!(stream.owner_kind(116, false, false).unwrap(), None);
        assert_eq!(
            stream.owner_kind(117, false, false).unwrap(),
            Some(DeltaKind::Correction(117))
        );
    }

    #[test]
    fn older_positive_ack_keeps_newer_lost_changes_and_exact_cumulative_ranges() {
        let data = payload();
        let mut stream = BodyStream::new(BODY);
        stream.create_arrived();
        let full = stream.full(&data, 100).unwrap();
        stream.arrived(full.last);
        let initial = stream
            .delta(&data, 101, BTreeSet::new(), DeltaKind::InitialOwner(101))
            .unwrap();
        stream.sent(initial.last);
        stream.arrived(initial.last);

        let old_key = ChangeKey::Variable(11, 53);
        let shared = ChangeKey::State(11, 10);
        let newer_key = ChangeKey::State(11, 16);
        let volley = ChangeKey::VolleyIntroduction(11, 16);
        let old = stream
            .delta(
                &data,
                117,
                BTreeSet::from([old_key, shared]),
                DeltaKind::Correction(117),
            )
            .unwrap();
        check_chunk(&old, 3, false, 41, "060000505001");
        let received = stream
            .delta(
                &data,
                118,
                BTreeSet::from([old_key, shared]),
                DeltaKind::Correction(118),
            )
            .unwrap();
        check_chunk(&received, 4, false, 41, "060004505001");
        let newer = stream
            .delta(
                &data,
                119,
                BTreeSet::from([shared, newer_key, volley]),
                DeltaKind::Correction(119),
            )
            .unwrap();
        for update in [&old, &received, &newer] {
            assert!(stream.sent(update.last).is_some());
        }
        assert_eq!(stream.lost(old.last).owner_report, Some(false));
        assert_eq!(stream.lost(newer.last).owner_report, Some(false));
        assert_eq!(stream.arrived(received.last).owner_report, Some(true));
        assert!(!stream.arrived(old.last).advanced);
        assert_eq!(stream.base(), Some((received.last, &118)));
        assert_eq!(stream.latest_snapshot(), Some(&119));
        assert_eq!(stream.pending().count(), 0);
        assert_eq!(stream.touched(), BTreeSet::from([shared, newer_key]));

        let recovered = stream
            .delta(&data, 120, stream.touched(), DeltaKind::Correction(120))
            .unwrap();
        stream.sent(recovered.last);
        assert!(stream.arrived(recovered.last).advanced);
        assert!(stream.touched().is_empty());
        assert_eq!(stream.latest_snapshot(), None);
    }

    #[test]
    fn a_superseded_owner_report_still_resolves_its_own_transport_outcome() {
        let data = payload();
        let mut stream = BodyStream::new(BODY);
        stream.create_arrived();
        let full = stream.full(&data, 100).unwrap();
        stream.arrived(full.last);
        let initial = stream
            .delta(&data, 101, BTreeSet::new(), DeltaKind::InitialOwner(101))
            .unwrap();
        stream.sent(initial.last);
        stream.arrived(initial.last);
        let early = stream
            .delta(&data, 117, BTreeSet::new(), DeltaKind::Correction(117))
            .unwrap();
        let late = stream
            .delta(&data, 118, BTreeSet::new(), DeltaKind::Correction(118))
            .unwrap();
        stream.sent(early.last);
        stream.sent(late.last);
        assert!(stream.arrived(late.last).advanced);
        assert_eq!(stream.pending().count(), 0);
        let old = stream.lost(early.last);
        assert_eq!(
            old,
            BodyDelivery {
                retry: false,
                advanced: false,
                owner_report: Some(false)
            }
        );
        assert_eq!(stream.lost(early.last), BodyDelivery::default());
        assert_eq!(stream.base(), Some((late.last, &118)));
    }

    #[test]
    fn observer_loss_keeps_the_pending_projection_until_superseded() {
        let data = payload();
        let mut stream = BodyStream::new(BODY);
        stream.create_arrived();
        let full = stream.full(&data, 100).unwrap();
        stream.arrived(full.last);
        let change = ChangeKey::Instance(11);
        let observer = stream
            .delta(
                &data,
                101,
                BTreeSet::from([change]),
                DeltaKind::Observer { resend: false },
            )
            .unwrap();
        assert!(stream.sent(observer.last).is_none());
        assert_eq!(stream.lost(observer.last), BodyDelivery::default());
        assert_eq!(stream.latest_snapshot(), Some(&101));
        assert_eq!(stream.touched(), BTreeSet::from([change]));
        let durable = stream
            .delta(
                &data,
                102,
                BTreeSet::from([change]),
                DeltaKind::Observer { resend: true },
            )
            .unwrap();
        assert!(stream.lost(durable.last).retry);
        stream.retire();
        assert!(!stream.retry(durable.last));
        assert!(!stream.ready_to_send(durable.last));
        assert!(!stream.arrived(durable.last).advanced);
        assert_eq!(stream.full(&data, 103), Err(StreamError::BodyRetired));
    }
}
