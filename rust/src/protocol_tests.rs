use crate::bits::BitReader;
use crate::commands::{CommandQueue, read_commands};
use crate::game_link::{LinkCipher, ReceiveWindow};
use crate::game_messages::read_channel;
use crate::jam_codec::{Schemas, Value};

#[test]
fn opens_a_python_game_datagram_and_dispatches_its_command_prefix_once() {
    // Python's channel, input record fixture and AES-GCM writer with synthetic keys.
    let datagram = [
        0xb1, 0x57, 0x76, 0xbe, 0x05, 0xcc, 0x27, 0x0d, 0x56, 0x83, 0xfc, 0x74, 0x91, 0x00, 0x00,
        0x00, 0xfe, 0xff, 0xff, 0xff, 0x03, 0x00, 0x00, 0x00, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x02, 0xad, 0xc6, 0x56, 0x67, 0x99, 0x4c, 0x3d, 0x2b, 0x75, 0xa2, 0xc5, 0xb3,
        0x0a, 0x34, 0x4b, 0xdf, 0x38, 0x2d, 0xfc, 0x51, 0x7a, 0xd3, 0x34, 0x2e, 0xc9, 0x69, 0x37,
        0x2d, 0x6b, 0xce, 0x80, 0x8d, 0x30, 0x51, 0xf5, 0xba, 0xbc, 0x07, 0xdb, 0x9a, 0x3c, 0x78,
    ];
    let cipher = LinkCipher::new(
        std::array::from_fn(|i| (i + 32) as u8),
        std::array::from_fn(|i| i as u8),
    );
    let schemas = Schemas::from_json(
        r#"{
        "8F89E0DF":{"25001":[{"type":6,"off":120,"count":0,"array":false}]},
        "82A2F991":{"27500":[{"type":2,"off":120,"count":0,"array":false}]}
    }"#,
    )
    .unwrap();

    let packet = cipher.open(&datagram).unwrap();
    assert_eq!(
        (packet.conn, packet.seq, packet.ack, packet.ack_bits),
        (9, u32::MAX - 1, 3, 5)
    );
    let mut window = ReceiveWindow {
        expected: u32::MAX - 2,
        bits: 1,
    };
    assert!(window.accept(packet.seq));
    assert!(!window.accept(packet.seq));

    let mut reader = BitReader::new(&packet.payload);
    let channel = read_channel(&mut reader, &schemas).unwrap();
    assert_eq!(channel.reliable[0].sequence, 17);
    assert_eq!(channel.reliable[0].message.id, 25001);
    assert_eq!(
        channel.reliable[0].message.fields[&120],
        Value::Unsigned(0x1234_5678)
    );
    assert_eq!(channel.unreliable[0].id, 27500);
    assert_eq!(channel.unreliable[0].fields[&120], Value::Unsigned(7));

    let commands = read_commands(&mut reader).unwrap();
    let mut queue = CommandQueue::new();
    let fresh = queue.consume(&commands);
    assert_eq!(fresh.len(), 2);
    assert_eq!(
        (fresh[0].frame, fresh[0].forward, fresh[0].pitch),
        (5000, 127, -500)
    );
    assert_eq!((fresh[1].frame, fresh[1].yaw), (5001, 300));
    assert!(queue.consume(&commands).is_empty());
}
