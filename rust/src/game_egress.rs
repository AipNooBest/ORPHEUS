//! Outgoing game message queues and the delivery of successfully sent frames.

use std::{borrow::Borrow, collections::VecDeque, error::Error, fmt, mem};

use crate::game_link::delivery;
use crate::world::{EntityUpdate, FrameCandidates, SelectedFrame, WorldError, select_frame};

type Reliable = (u32, Vec<u8>);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct OrdinalExhausted;

impl fmt::Display for OrdinalExhausted {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("game reliable message ordinals exhausted")
    }
}

impl Error for OrdinalExhausted {}

#[derive(Debug)]
pub enum SendError<E> {
    Select(WorldError),
    Transport(E),
}

impl<E: fmt::Display> fmt::Display for SendError<E> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Select(error) => write!(f, "game frame rejected: {error}"),
            Self::Transport(error) => write!(f, "game frame send failed: {error}"),
        }
    }
}

impl<E: Error + 'static> Error for SendError<E> {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Select(error) => Some(error),
            Self::Transport(error) => Some(error),
        }
    }
}

pub struct FrameRecords<'a, Queued = EntityUpdate> {
    pub tick: u32,
    /// The match supplies the records eligible to send in dependency order.
    pub queued: &'a [Queued],
    pub movements: &'a [EntityUpdate],
    pub owner_correction: bool,
    pub reliable_only: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RecordSource {
    Queued(usize),
    Movement(usize),
}

#[derive(Debug, Eq, PartialEq)]
pub struct SentFrame {
    pub sequence: u32,
    /// Records actually sent, including best-effort movement, in wire order.
    pub records: Vec<RecordSource>,
}

#[derive(Debug, Eq, PartialEq)]
pub struct PacketDelivery<T> {
    pub sequence: u32,
    pub arrived: bool,
    pub records: Vec<T>,
}

#[derive(Debug, Eq, PartialEq)]
pub struct DeliveryReport<T> {
    pub requeued_reliable: usize,
    /// Resolved tracked datagrams in send order. The match checks record
    /// lifetimes before applying delivery or requeueing a lost record.
    pub packets: Vec<PacketDelivery<T>>,
}

struct InFlight<T> {
    sequence: u32,
    reliable: Vec<Reliable>,
    records: Vec<T>,
}

pub struct GameEgress<T> {
    next_reliable: u64,
    reliable: VecDeque<Reliable>,
    unreliable: VecDeque<Vec<u8>>,
    in_flight: VecDeque<InFlight<T>>,
}

impl<T> Default for GameEgress<T> {
    fn default() -> Self {
        Self {
            next_reliable: 0,
            reliable: VecDeque::new(),
            unreliable: VecDeque::new(),
            in_flight: VecDeque::new(),
        }
    }
}

impl<T> GameEgress<T> {
    pub fn queue_reliable(&mut self, message: Vec<u8>) -> Result<u32, OrdinalExhausted> {
        let ordinal = u32::try_from(self.next_reliable).map_err(|_| OrdinalExhausted)?;
        self.reliable.push_back((ordinal, message));
        self.next_reliable += 1;
        Ok(ordinal)
    }

    pub fn queue_unreliable(&mut self, message: Vec<u8>) {
        self.unreliable.push_back(message);
    }

    pub fn oldest_pending_reliable(&self) -> u64 {
        self.reliable
            .front()
            .into_iter()
            .chain(
                self.in_flight
                    .iter()
                    .filter_map(|packet| packet.reliable.first()),
            )
            .map(|(ordinal, _)| u64::from(*ordinal))
            .min()
            .unwrap_or(self.next_reliable)
    }

    /// Ownership and other prerequisites take effect after the full reliable
    /// prefix, even if the datagram containing the prerequisite arrived first.
    pub fn delivered_through(&self, ordinal: u32) -> bool {
        self.oldest_pending_reliable() > u64::from(ordinal)
    }

    /// Select and attempt one frame. The transport owns packet sequence
    /// allocation and returns the sequence only after the datagram is sent.
    /// Record associations and queue removal follow that successful send.
    pub fn send_frame<E, Queued: Borrow<EntityUpdate>>(
        &mut self,
        records: FrameRecords<'_, Queued>,
        ack_for_records: impl Fn(&[&EntityUpdate]) -> u32,
        mut identify: impl FnMut(RecordSource) -> T,
        send: impl FnOnce(Vec<u8>) -> Result<u32, E>,
    ) -> Result<SentFrame, SendError<E>> {
        let SelectedFrame {
            payload,
            reliable_count,
            unreliable_count,
            entities,
        } = select_frame(
            FrameCandidates {
                tick: records.tick,
                reliable: self.reliable.make_contiguous(),
                unreliable: self.unreliable.make_contiguous(),
                queued: records.queued,
                movements: records.movements,
                owner_correction: records.owner_correction,
                reliable_only: records.reliable_only,
            },
            ack_for_records,
        )
        .map_err(SendError::Select)?;
        let sequence = send(payload).map_err(SendError::Transport)?;

        let mut sent = Vec::with_capacity(entities.len());
        let mut tracked = Vec::new();
        for (index, entity) in entities.into_iter().enumerate() {
            let source = match index.checked_sub(records.queued.len()) {
                None => RecordSource::Queued(index),
                Some(index) => {
                    let index = if records
                        .movements
                        .get(index)
                        .is_some_and(|candidate| std::ptr::eq(entity, candidate))
                    {
                        index
                    } else {
                        // Budget selection may replace the tail of the movement
                        // prefix with the final record, the owner's correction.
                        records.movements.len() - 1
                    };
                    RecordSource::Movement(index)
                }
            };
            sent.push(source);
            if entity.operation.is_some() || entity.chunk.is_some() || entity.ch2.is_some() {
                tracked.push(identify(source));
            }
        }
        self.unreliable.drain(..unreliable_count);
        if reliable_count != 0 || !tracked.is_empty() {
            self.in_flight.push_back(InFlight {
                sequence,
                reliable: self.reliable.drain(..reliable_count).collect(),
                records: tracked,
            });
        }
        Ok(SentFrame {
            sequence,
            records: sent,
        })
    }

    /// Interpret one authenticated ACK window. Resolved packets leave flight
    /// once, so a duplicate ACK cannot deliver or retry their records again.
    pub fn acknowledge(&mut self, ack: u32, ack_bits: u64) -> DeliveryReport<T> {
        let mut lost = Vec::new();
        let mut packets = Vec::new();
        self.in_flight.retain_mut(|packet| {
            let Some(arrived) = delivery(packet.sequence, ack, ack_bits) else {
                return true;
            };
            if !arrived {
                lost.append(&mut packet.reliable);
            }
            packets.push(PacketDelivery {
                sequence: packet.sequence,
                arrived,
                records: mem::take(&mut packet.records),
            });
            false
        });
        let requeued_reliable = lost.len();
        if requeued_reliable != 0 {
            lost.extend(self.reliable.drain(..));
            lost.sort_unstable_by_key(|(ordinal, _)| *ordinal);
            self.reliable = lost.into();
        }
        DeliveryReport {
            requeued_reliable,
            packets,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bits::BitReader;
    use crate::game_messages::read_channel;
    use crate::jam_codec::Schemas;
    use crate::world::{MAX_ENTITY_RECORDS, MAX_PAYLOAD, Movement, Operation};

    const MESSAGE: [u8; 2] = [1, 2];

    fn frame_records<'a>(
        queued: &'a [EntityUpdate],
        movements: &'a [EntityUpdate],
    ) -> FrameRecords<'a> {
        FrameRecords {
            tick: 5000,
            queued,
            movements,
            owner_correction: false,
            reliable_only: false,
        }
    }

    fn destroy(id: u32) -> EntityUpdate {
        EntityUpdate {
            entity: 0x8000_0000 | id,
            operation: Some(Operation::Destroy),
            ..EntityUpdate::default()
        }
    }

    #[test]
    fn transport_failure_retains_message_and_record_candidates() {
        let mut egress = GameEgress::default();
        let reliable = MESSAGE.to_vec();
        let original = reliable.as_ptr();
        egress.queue_reliable(reliable).unwrap();
        egress.queue_unreliable(MESSAGE.to_vec());
        let queued = [destroy(1)];
        let mut sequence = 0;
        let mut attempted = None;
        let failed = egress.send_frame(
            frame_records(&queued, &[]),
            |_| 77,
            |_| panic!("a failed send must not associate records"),
            |payload| {
                attempted = Some(payload);
                sequence += 1;
                Err(std::io::ErrorKind::WouldBlock)
            },
        );
        assert!(matches!(
            failed,
            Err(SendError::Transport(std::io::ErrorKind::WouldBlock))
        ));
        assert!(egress.in_flight.is_empty());
        assert_eq!(egress.reliable[0].1.as_ptr(), original);
        assert_eq!(egress.unreliable.len(), 1);

        let sent = egress
            .send_frame(
                frame_records(&queued, &[]),
                |_| 77,
                |source| source,
                |payload| {
                    assert_eq!(Some(payload), attempted);
                    Ok::<_, std::io::ErrorKind>(sequence)
                },
            )
            .unwrap();
        assert_eq!(sent.sequence, 1);
        assert_eq!(sent.records, [RecordSource::Queued(0)]);
        assert!(egress.reliable.is_empty());
        assert!(egress.unreliable.is_empty());
        assert_eq!(egress.in_flight[0].reliable[0].1.as_ptr(), original);
    }

    #[test]
    fn loss_requeues_reliable_bytes_once_and_ownership_waits_for_the_prefix() {
        #[derive(Debug, Eq, PartialEq)]
        struct Token(u32);

        let mut egress = GameEgress::default();
        let first = MESSAGE.to_vec();
        let original = first.as_ptr();
        assert_eq!(egress.queue_reliable(first).unwrap(), 0);
        egress.queue_unreliable(MESSAGE.to_vec());
        egress
            .send_frame(
                frame_records(&[destroy(1)], &[]),
                |_| 0,
                |_| Token(1),
                |_| Ok::<_, ()>(10),
            )
            .unwrap();
        let ownership = egress.queue_reliable(MESSAGE.to_vec()).unwrap();
        assert_eq!(ownership, 1);
        egress
            .send_frame(
                frame_records(&[destroy(2)], &[]),
                |_| 0,
                |_| Token(2),
                |_| Ok::<_, ()>(11),
            )
            .unwrap();
        assert_eq!(egress.queue_reliable(MESSAGE.to_vec()).unwrap(), 2);

        let report = egress.acknowledge(12, 1);
        assert_eq!(report.requeued_reliable, 1);
        assert_eq!(
            report.packets,
            [
                PacketDelivery {
                    sequence: 10,
                    arrived: false,
                    records: vec![Token(1)]
                },
                PacketDelivery {
                    sequence: 11,
                    arrived: true,
                    records: vec![Token(2)]
                },
            ]
        );
        assert_eq!(egress.oldest_pending_reliable(), 0);
        assert!(!egress.delivered_through(ownership));
        assert_eq!(egress.reliable[0].1.as_ptr(), original);
        assert!(egress.unreliable.is_empty());
        assert!(egress.acknowledge(12, 1).packets.is_empty());
        assert_eq!(egress.reliable.len(), 2);

        let schemas = Schemas::from_json(r#"{"A7FBBD0C":{"21602":[]}}"#).unwrap();
        egress
            .send_frame(
                frame_records(&[], &[]),
                |_| 0,
                |_| unreachable!(),
                |payload| {
                    let mut reader = BitReader::new(&payload);
                    assert_eq!(reader.var_a().unwrap(), 5000);
                    let channel = read_channel(&mut reader, &schemas).unwrap();
                    assert!(channel.unreliable.is_empty());
                    assert_eq!(
                        channel
                            .reliable
                            .iter()
                            .map(|message| message.sequence)
                            .collect::<Vec<_>>(),
                        [0, 2]
                    );
                    Ok::<_, ()>(12)
                },
            )
            .unwrap();
        assert!(!egress.delivered_through(ownership));
        assert!(egress.acknowledge(12, 1).packets.is_empty());
        assert_eq!(egress.acknowledge(13, 1).packets[0].sequence, 12);
        assert!(egress.delivered_through(ownership));
        assert_eq!(egress.oldest_pending_reliable(), 3);
    }

    #[test]
    fn reports_acknowledgements_in_send_order_across_packet_rollover() {
        let mut egress = GameEgress::default();
        for sequence in [u32::MAX - 1, u32::MAX, 0] {
            egress
                .send_frame(
                    frame_records(&[destroy(1)], &[]),
                    |_| 0,
                    |_| sequence,
                    |_| Ok::<_, ()>(sequence),
                )
                .unwrap();
        }
        assert!(egress.acknowledge(u32::MAX - 1, 1).packets.is_empty());
        let report = egress.acknowledge(1, 0b101);
        assert_eq!(report.requeued_reliable, 0);
        assert_eq!(
            report.packets,
            [
                PacketDelivery {
                    sequence: u32::MAX - 1,
                    arrived: true,
                    records: vec![u32::MAX - 1]
                },
                PacketDelivery {
                    sequence: u32::MAX,
                    arrived: false,
                    records: vec![u32::MAX]
                },
                PacketDelivery {
                    sequence: 0,
                    arrived: true,
                    records: vec![0]
                },
            ]
        );
        assert!(egress.acknowledge(1, 0b101).packets.is_empty());
    }

    #[test]
    fn an_unsendable_oldest_reliable_never_reaches_transport() {
        let mut egress = GameEgress::<()>::default();
        egress.queue_reliable(vec![0; MAX_PAYLOAD]).unwrap();
        egress.queue_unreliable(MESSAGE.to_vec());
        let result = egress.send_frame(
            frame_records(&[destroy(1)], &[]),
            |_| 0,
            |_| panic!("invalid frame cannot associate records"),
            |_| -> Result<u32, ()> { panic!("invalid frame cannot reach transport") },
        );
        assert!(matches!(
            result,
            Err(SendError::Select(WorldError::ReliableMessageDoesNotFit))
        ));
        assert_eq!(egress.reliable.len(), 1);
        assert_eq!(egress.unreliable.len(), 1);
        assert!(egress.in_flight.is_empty());
    }

    #[test]
    fn reliable_only_fits_the_message_prefix_and_leaves_other_candidates() {
        let mut egress = GameEgress::<RecordSource>::default();
        for _ in 0..2 {
            egress.queue_reliable(vec![0; MAX_PAYLOAD * 3 / 4]).unwrap();
        }
        egress.queue_unreliable(MESSAGE.to_vec());
        let queued = [destroy(1)];
        let records = FrameRecords {
            reliable_only: true,
            ..frame_records(&queued, &[])
        };
        let send = |sequence| {
            move |payload: Vec<u8>| {
                assert!(payload.len() <= MAX_PAYLOAD);
                Ok::<_, ()>(sequence)
            }
        };
        let sent = egress
            .send_frame(records, |_| 0, |_| unreachable!(), send(1))
            .unwrap();
        assert!(sent.records.is_empty());
        assert_eq!(egress.reliable.len(), 1);
        assert_eq!(egress.unreliable.len(), 1);
        assert_eq!(egress.in_flight[0].reliable[0].0, 0);
        let sent = egress
            .send_frame(frame_records(&queued, &[]), |_| 0, |source| source, send(2))
            .unwrap();
        assert_eq!(sent.records, [RecordSource::Queued(0)]);
        assert_eq!(egress.in_flight[1].reliable[0].0, 1);
        assert!(egress.unreliable.is_empty());
    }

    #[test]
    fn records_actual_correction_selection_but_only_tracks_acknowledgeable_records() {
        let mut egress = GameEgress::default();
        let mut queued: Vec<_> = (1..MAX_ENTITY_RECORDS as u32)
            .map(|id| EntityUpdate {
                entity: 0x8000_0000 | id,
                ..EntityUpdate::default()
            })
            .collect();
        queued[0] = destroy(1);
        let movements = [
            EntityUpdate {
                entity: 0x8000_0080,
                movement: Some(Movement::default()),
                ..EntityUpdate::default()
            },
            EntityUpdate {
                entity: 0xa000_0100,
                movement: Some(Movement::default()),
                ..EntityUpdate::default()
            },
        ];
        let sent = egress
            .send_frame(
                FrameRecords {
                    owner_correction: true,
                    ..frame_records(&queued, &movements)
                },
                |records| {
                    u32::from(
                        records
                            .last()
                            .is_some_and(|record| record.entity == movements[1].entity),
                    )
                },
                |source| source,
                |payload| {
                    let mut reader = BitReader::new(&payload);
                    assert_eq!(reader.var_a().unwrap(), 5000);
                    assert!(!reader.bit().unwrap());
                    assert_eq!(reader.var_a().unwrap(), 1);
                    Ok::<_, ()>(7)
                },
            )
            .unwrap();
        assert_eq!(sent.records.len(), MAX_ENTITY_RECORDS);
        assert_eq!(
            sent.records[..queued.len()],
            (0..queued.len())
                .map(RecordSource::Queued)
                .collect::<Vec<_>>()
        );
        assert_eq!(sent.records[queued.len()], RecordSource::Movement(1));
        let report = egress.acknowledge(8, 1);
        assert_eq!(report.packets[0].records, [RecordSource::Queued(0)]);
    }

    #[test]
    fn reliable_ordinals_cannot_wrap() {
        let mut egress = GameEgress::<()> {
            next_reliable: u64::from(u32::MAX),
            ..GameEgress::default()
        };
        assert_eq!(egress.queue_reliable(MESSAGE.to_vec()), Ok(u32::MAX));
        assert_eq!(
            egress.queue_reliable(MESSAGE.to_vec()),
            Err(OrdinalExhausted)
        );
        assert_eq!(egress.reliable.len(), 1);
    }
}
