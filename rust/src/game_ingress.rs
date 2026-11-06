//! Incoming message ordering, pong replies, and map entry milestones.

use std::{collections::HashMap, error::Error, fmt, time::Duration};

use crate::bits::BitReader;
use crate::commands::{Command, InputError, read_commands};
use crate::game_messages::{ChannelError, Message, read_channel};
use crate::jam_codec::{Fields, Schemas, Value};

const PONG: u32 = 20306;
const PING_INDEX: u32 = 0x78;
const PING_STAMPS: u32 = 0x80;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MessageKind {
    Ping,
    MapLoaded,
    GameMessage,
    Other,
}

impl From<u32> for MessageKind {
    fn from(id: u32) -> Self {
        match id {
            21610 => Self::Ping,
            21601 | 21602 => Self::MapLoaded,
            21616 => Self::GameMessage,
            _ => Self::Other,
        }
    }
}

#[derive(Debug)]
pub enum IngressError {
    Channel(ChannelError),
    Commands(InputError),
}

impl fmt::Display for IngressError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Channel(error) => write!(f, "bad game channel: {error}"),
            Self::Commands(error) => write!(f, "bad game input: {error}"),
        }
    }
}

impl Error for IngressError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Channel(error) => Some(error),
            Self::Commands(error) => Some(error),
        }
    }
}

#[derive(Debug, Default)]
pub struct GameIngress {
    // Reliable ordinals increase throughout the session, unlike UDP packet numbers.
    expected_reliable: u64,
    parked: HashMap<u32, Message>,
}

impl GameIngress {
    pub fn new() -> Self {
        Self::default()
    }

    /// Decode a complete channel before changing its reliable state. Dispatch
    /// newly available messages before parsing commands, since a game message
    /// can change which player body should receive those commands.
    pub fn receive(
        &mut self,
        schemas: &Schemas,
        payload: &[u8],
        mut dispatch: impl FnMut(Message),
    ) -> Result<Vec<Command>, IngressError> {
        let mut reader = BitReader::new(payload);
        let channel = read_channel(&mut reader, schemas).map_err(IngressError::Channel)?;
        for reliable in channel.reliable {
            if u64::from(reliable.sequence) < self.expected_reliable {
                continue;
            }
            self.parked.insert(reliable.sequence, reliable.message);
            while let Ok(expected) = u32::try_from(self.expected_reliable) {
                let Some(message) = self.parked.remove(&expected) else {
                    break;
                };
                self.expected_reliable += 1;
                dispatch(message);
            }
        }
        for message in channel.unreliable {
            dispatch(message);
        }
        read_commands(&mut reader).map_err(IngressError::Commands)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct InvalidPingField(pub u32);

impl fmt::Display for InvalidPingField {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "invalid game ping field +0x{:X}", self.0)
    }
}

impl Error for InvalidPingField {}

/// The two wall-clock readings belong to message dispatch, including when a
/// reliable ping has waited behind a missing ordinal.
pub fn pong(ping: &Fields, received_ns: u64, sent_ns: u64) -> Result<Message, InvalidPingField> {
    let index = match ping.get(&PING_INDEX) {
        None => 0,
        Some(Value::Unsigned(index)) if u32::try_from(*index).is_ok() => *index,
        _ => return Err(InvalidPingField(PING_INDEX)),
    };
    let mut stamps = match ping.get(&PING_STAMPS) {
        None => Vec::new(),
        Some(Value::Array(stamps))
            if stamps
                .iter()
                .all(|stamp| matches!(stamp, Value::Unsigned(_))) =>
        {
            stamps.clone()
        }
        _ => return Err(InvalidPingField(PING_STAMPS)),
    };
    stamps.extend([Value::Unsigned(received_ns), Value::Unsigned(sent_ns)]);
    Ok(Message {
        id: PONG,
        fields: Fields::from([
            (PING_INDEX, Value::Unsigned(index)),
            (PING_STAMPS, Value::Array(stamps)),
        ]),
    })
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MapReadyStep {
    Ownership,
    Game,
    World,
    Controller,
}

impl MapReadyStep {
    const fn delay(self) -> Duration {
        Duration::from_millis(match self {
            Self::Ownership => 300,
            Self::Game => 400,
            Self::World => 600,
            Self::Controller => 700,
        })
    }

    const fn next(self) -> Option<Self> {
        match self {
            Self::Ownership => Some(Self::Game),
            Self::Game => Some(Self::World),
            Self::World => Some(Self::Controller),
            Self::Controller => None,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct WorldReadiness {
    /// The client acknowledged the entire reliable prefix through ownership.
    pub ownership_delivered: bool,
    pub collision_ready: bool,
    pub bot_scripts_ready: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct MapReadiness {
    loaded_at: Option<Duration>,
    next: Option<MapReadyStep>,
}

impl Default for MapReadiness {
    fn default() -> Self {
        Self {
            loaded_at: None,
            next: Some(MapReadyStep::Ownership),
        }
    }
}

impl MapReadiness {
    /// Anchor the entry schedule to the first dispatched map-loaded notice.
    /// Both inputs and tick times use the same monotonic time origin.
    pub fn mark_loaded(&mut self, now: Duration) -> bool {
        if self.loaded_at.is_some() {
            return false;
        }
        self.loaded_at = Some(now);
        true
    }

    /// Take one due action. A tick can take several actions in order, checking
    /// the live world prerequisites again after each action.
    pub fn take_due(&mut self, now: Duration, world: WorldReadiness) -> Option<MapReadyStep> {
        let waited = now.checked_sub(self.loaded_at?)?;
        let step = self.next?;
        if waited < step.delay()
            || (step == MapReadyStep::World
                && !(world.ownership_delivered && world.collision_ready && world.bot_scripts_ready))
        {
            return None;
        }
        self.next = step.next();
        Some(step)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bits::BitWriter;
    use crate::game_messages::{encode, write_channel};

    fn schemas() -> Schemas {
        Schemas::from_json(
            r#"{
                "C77F6403": {
                    "20306": [
                        {"type":6,"off":120,"size":4,"count":0,"array":false},
                        {"type":8,"off":128,"size":8,"count":-1,"array":true}
                    ]
                },
                "A7FBBD0C": {
                    "21600":[], "21602":[], "21603":[], "21604":[], "21616":[]
                }
            }"#,
        )
        .unwrap()
    }

    fn channel(schemas: &Schemas, reliable: &[(u32, u32)], unreliable: &[u32]) -> BitWriter {
        let message = |id| encode(schemas, id, &Fields::new()).unwrap();
        let reliable: Vec<_> = reliable
            .iter()
            .map(|&(sequence, id)| (sequence, message(id)))
            .collect();
        let unreliable: Vec<_> = unreliable.iter().copied().map(message).collect();
        let mut writer = BitWriter::new();
        write_channel(&mut writer, &reliable, &unreliable).unwrap();
        writer
    }

    #[test]
    fn fills_the_reliable_gap_before_unreliable_messages() {
        let schemas = schemas();
        let mut ingress = GameIngress::new();
        let mut delivered = Vec::new();
        let mut dispatch = |message: Message| delivered.push(message.id);
        let first = channel(&schemas, &[(1, 21603), (2, 21604)], &[21602]);
        let second = channel(&schemas, &[(1, 21616)], &[]);
        let third = channel(&schemas, &[(0, 21600), (1, 21603)], &[21602]);
        for frame in [first, second, third] {
            assert!(
                ingress
                    .receive(&schemas, frame.as_bytes(), &mut dispatch)
                    .unwrap()
                    .is_empty()
            );
        }
        assert_eq!(delivered, [21602, 21600, 21616, 21604, 21602]);
    }

    #[test]
    fn a_truncated_channel_cannot_dispatch_its_valid_prefix() {
        let schemas = schemas();
        let mut ingress = GameIngress::new();
        let frame = channel(&schemas, &[(0, 21600), (1, 21603)], &[]).into_bytes();
        let bad = &frame[..frame.len() - 1];
        assert!(matches!(
            ingress.receive(&schemas, bad, |_| panic!("unexpected message")),
            Err(IngressError::Channel(_))
        ));

        let mut delivered = Vec::new();
        ingress
            .receive(&schemas, &frame, |message| delivered.push(message.id))
            .unwrap();
        assert_eq!(delivered, [21600, 21603]);
    }

    #[test]
    fn a_bad_input_record_does_not_undo_dispatched_messages() {
        let schemas = schemas();
        let mut ingress = GameIngress::new();
        let mut map = MapReadiness::default();
        let now = Duration::from_secs(10);
        let mut frame = channel(&schemas, &[(0, 21602)], &[]);
        frame.bit(true); // Presence bit without its 32-bit length.
        assert!(matches!(
            ingress.receive(&schemas, frame.as_bytes(), |message| {
                assert_eq!(MessageKind::from(message.id), MessageKind::MapLoaded);
                assert!(map.mark_loaded(now));
            }),
            Err(IngressError::Commands(InputError::Truncated))
        ));
        assert_eq!(
            map.take_due(now + Duration::from_millis(300), WorldReadiness::default()),
            Some(MapReadyStep::Ownership)
        );
        assert!(matches!(
            ingress.receive(&schemas, frame.as_bytes(), |_| panic!("duplicate message")),
            Err(IngressError::Commands(_))
        ));
    }

    #[test]
    fn a_body_change_is_visible_when_the_returned_commands_are_applied() {
        let schemas = schemas();
        let mut frame = channel(&schemas, &[], &[21616]);
        // Input record from tests/test_game_commands.py::record(5000).
        let commands = [
            0x55, 0x01, 0, 0, 0x46, 0, 0, 0x04, 0x10, 0x27, 0, 0, 0x2e, 0, 0, 0x06, 0x7f, 0, 0, 0,
            0x80, 0x11, 0, 0x29, 0x4b, 0,
        ];
        frame.raw_bits(&commands, 203).unwrap();

        let mut body = None;
        let commands = GameIngress::new()
            .receive(&schemas, frame.as_bytes(), |message| {
                assert_eq!(MessageKind::from(message.id), MessageKind::GameMessage);
                body = Some(9);
            })
            .unwrap();
        assert_eq!(body, Some(9));
        assert_eq!(
            commands
                .iter()
                .map(|command| command.frame)
                .collect::<Vec<_>>(),
            [5000, 5001]
        );
    }

    #[test]
    fn reliable_ordinals_do_not_wrap_with_udp_packet_sequences() {
        let schemas = schemas();
        let mut ingress = GameIngress {
            expected_reliable: u64::from(u32::MAX),
            ..GameIngress::default()
        };
        let frame = channel(&schemas, &[(u32::MAX, 21600), (0, 21603)], &[]);
        let mut delivered = Vec::new();
        ingress
            .receive(&schemas, frame.as_bytes(), |message| {
                delivered.push(message.id)
            })
            .unwrap();
        assert_eq!(delivered, [21600]);
        assert!(ingress.parked.is_empty());
    }

    #[test]
    fn builds_the_pong_with_exact_stamp_order() {
        let ping = Fields::from([
            (0x78, Value::Unsigned(3)),
            (
                0x80,
                Value::Array(vec![Value::Unsigned(111), Value::Unsigned(222)]),
            ),
        ]);
        let reply = pong(&ping, 333, 444).unwrap();
        assert_eq!(reply.id, 20306);
        let bytes = encode(&schemas(), reply.id, &reply.fields).unwrap();
        let mut expected = vec![0, 6, 3, 0, 0, 0, 4, 0, 0, 0];
        for stamp in [111_u64, 222, 333, 444] {
            expected.extend(stamp.to_le_bytes());
        }
        assert_eq!(bytes, expected);
        let defaults = pong(&Fields::new(), 1, 2).unwrap();
        assert_eq!(defaults.fields[&0x78], Value::Unsigned(0));
        assert_eq!(
            defaults.fields[&0x80],
            Value::Array(vec![Value::Unsigned(1), Value::Unsigned(2)])
        );
        let malformed = Fields::from([(0x80, Value::Array(vec![Value::Signed(-1)]))]);
        assert_eq!(pong(&malformed, 1, 2), Err(InvalidPingField(0x80)));
        assert_eq!(MessageKind::from(21610), MessageKind::Ping);
        assert_eq!(MessageKind::from(21601), MessageKind::MapLoaded);
        assert_eq!(MessageKind::from(21603), MessageKind::Other);
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 game schema"]
    fn matches_a_python_mixed_frame_and_pong_with_the_runtime_schema() {
        use std::path::{Path, PathBuf};

        let root = std::env::var_os("ORPHEUS_RUNTIME_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).to_owned());
        let schemas = Schemas::load(root.join("data/game_schemas_174.json")).unwrap();
        // Python write_channel + tests/test_game_commands.py::record(5000).
        let frame = [
            0x23, 0, 0, 0, 0, 0, 1, 2, 1, 10, 3, 0, 0, 0, 2, 0, 0, 0, 111, 0, 0, 0, 0, 0, 0, 0,
            222, 0, 0, 0, 0, 0, 0, 0, 0x55, 1, 0, 0, 0x46, 0, 0, 4, 0x10, 0x27, 0, 0, 0x2e, 0, 0,
            6, 0x7f, 0, 0, 0, 0x80, 0x11, 0, 0x29, 0x4b, 0,
        ];
        let mut map = MapReadiness::default();
        let mut reply = None;
        let commands = GameIngress::new()
            .receive(&schemas, &frame, |message| {
                match MessageKind::from(message.id) {
                    MessageKind::MapLoaded => assert!(map.mark_loaded(Duration::ZERO)),
                    MessageKind::Ping => {
                        assert_eq!(
                            map.take_due(Duration::from_millis(300), WorldReadiness::default()),
                            Some(MapReadyStep::Ownership)
                        );
                        reply = Some(pong(&message.fields, 333, 444).unwrap());
                    }
                    kind => panic!("unexpected message kind {kind:?}"),
                }
            })
            .unwrap();
        assert_eq!(commands[0].frame, 5000);
        assert_eq!(commands[1].frame, 5001);
        let reply = reply.unwrap();
        // Python messages.encode(20306, match.pong(...)) with the same clocks.
        let expected = [
            0, 6, 3, 0, 0, 0, 4, 0, 0, 0, 111, 0, 0, 0, 0, 0, 0, 0, 222, 0, 0, 0, 0, 0, 0, 0, 0x4d,
            1, 0, 0, 0, 0, 0, 0, 0xbc, 1, 0, 0, 0, 0, 0, 0,
        ];
        assert_eq!(encode(&schemas, reply.id, &reply.fields).unwrap(), expected);
    }

    #[test]
    fn map_steps_are_relative_to_the_first_notice_and_wait_for_the_world() {
        let mut map = MapReadiness::default();
        let waiting = WorldReadiness::default();
        let ready = WorldReadiness {
            ownership_delivered: true,
            collision_ready: true,
            bot_scripts_ready: true,
        };
        let time = |offset: u64| Duration::from_millis(10_000 + offset);
        assert_eq!(map.take_due(time(1_000), ready), None);
        assert!(map.mark_loaded(time(0)));
        assert!(!map.mark_loaded(time(250)));
        assert_eq!(map.take_due(Duration::ZERO, ready), None);
        assert_eq!(map.take_due(time(299), ready), None);
        assert_eq!(
            map.take_due(time(300), waiting),
            Some(MapReadyStep::Ownership)
        );
        assert_eq!(map.take_due(time(399), waiting), None);
        assert_eq!(map.take_due(time(400), waiting), Some(MapReadyStep::Game));
        assert_eq!(map.take_due(time(599), ready), None);
        for blocker in [
            WorldReadiness {
                ownership_delivered: false,
                ..ready
            },
            WorldReadiness {
                collision_ready: false,
                ..ready
            },
            WorldReadiness {
                bot_scripts_ready: false,
                ..ready
            },
        ] {
            assert_eq!(map.take_due(time(600), blocker), None);
        }
        assert_eq!(map.take_due(time(600), ready), Some(MapReadyStep::World));
        assert_eq!(map.take_due(time(699), ready), None);
        assert_eq!(
            map.take_due(time(700), ready),
            Some(MapReadyStep::Controller)
        );
        assert_eq!(map.take_due(time(701), ready), None);
    }
}
