//! Owner controller and remote team-entry frames for the 104319 hero select.

use std::{collections::BTreeMap, io};

use crate::{
    bits::BitWriter,
    game_content::PRACTICE_RANGE_MODE,
    script_graph::{Graph, GraphStore},
    script_wire::{
        Instance, Layout, Value, Variables, WireError, owner_full, remote_full, subscript,
    },
};

const PRACTICE: u32 = 0x13c1;
const PVP: u32 = 0x0c90;
const HUD: u32 = 0x20e4;
const HERO_SELECT_HOST: u32 = 0x288a;
const HERO_SELECT: u32 = 0x288b;
const TEAM_ENTRY: u32 = 0x288d;

const ALLOW_SELECT: u32 = 8419;
const ASSEMBLING: u32 = 25377;
const HIDE_ASSEMBLE: u32 = 8535;
const OPEN_SELECT: u32 = 2438;
const SELECT_SHOWN: u32 = 31435;
const CURRENT_HERO: u32 = 940;
const PICKED: u32 = 2114;
const PICKED_HERO: u32 = 13161;

#[derive(Clone, Copy, Debug)]
struct StateSlot {
    index: u16,
    wire: usize,
}

#[derive(Clone, Copy, Debug)]
struct Hud {
    layout: Layout,
    root_state: StateSlot,
    shared_vars: usize,
}

/// Loaded layouts keep filtered owner positions separate from raw parent state IDs.
#[derive(Clone, Copy, Debug)]
pub struct Controller {
    root: Layout,
    host: Layout,
    screen: Layout,
    entry: Layout,
    hud: Option<Hud>,
    allow: usize,
    hero_select: usize,
    start_host: StateSlot,
    start_screen: StateSlot,
    start_entry: StateSlot,
    assembling: usize,
}

#[derive(Clone, Copy, Debug, Default)]
pub struct ControllerFrame<'a> {
    pub select_open: bool,
    pub skins: bool,
    pub hide_assemble: bool,
    /// Extra presence bits of the root controller, such as notice switches.
    pub presence: Option<&'a BTreeMap<usize, Value>>,
    pub hero: Option<u64>,
}

impl Controller {
    pub fn load(graphs: &GraphStore, mode_guid: u64) -> io::Result<Self> {
        let (root_id, hero_select, start_host, start_hud) = if mode_guid == PRACTICE_RANGE_MODE {
            (PRACTICE, 29, 43, Some(42))
        } else {
            (PVP, 1, 119, None)
        };
        let root = graph(graphs, root_id)?;
        let host = graph(graphs, HERO_SELECT_HOST)?;
        let screen = graph(graphs, HERO_SELECT)?;
        let entry = graph(graphs, TEAM_ENTRY)?;
        let hud = start_hud
            .map(|index| {
                let hud = graph(graphs, HUD)?;
                Ok::<_, io::Error>(Hud {
                    layout: layout(hud)?,
                    root_state: owner_slot(root, index)?,
                    // 20E4's EntryClientOnlySharedVars initializes raw state 25.
                    shared_vars: owner_slot(hud, 25)?.wire,
                })
            })
            .transpose()?;
        Ok(Self {
            root: layout(root)?,
            host: layout(host)?,
            screen: layout(screen)?,
            entry: layout(entry)?,
            hud,
            allow: presence_bit(root, ALLOW_SELECT)?,
            hero_select: owner_slot(root, hero_select)?.wire,
            start_host: owner_slot(root, start_host)?,
            start_screen: owner_slot(host, 0)?,
            start_entry: owner_slot(host, 1)?,
            assembling: presence_bit(screen, ASSEMBLING)?,
        })
    }

    /// Owner full frame: root controller, 288A, 288B, optional HUD, and team entry.
    pub fn controller_frame(
        &self,
        command_frame: u32,
        input: ControllerFrame<'_>,
    ) -> Result<BitWriter, WireError> {
        let mut root = Instance::new(1, self.root, None);
        root.presence.insert(self.allow, Value::Bool(true));
        if let Some(extra) = input.presence {
            root.presence
                .extend(extra.iter().map(|(&slot, value)| (slot, value.clone())));
        }
        root.states.insert(self.hero_select, BitWriter::new());
        root.states.insert(self.start_host.wire, subscript(2));

        let entry_id = if self.hud.is_some() { 5 } else { 4 };
        let mut host = Instance::new(2, self.host, Some((1, self.start_host.index)));
        host.states.insert(self.start_screen.wire, subscript(3));
        host.states
            .insert(self.start_entry.wire, subscript(entry_id));
        let mut screen = Instance::new(3, self.screen, Some((2, self.start_screen.index)));
        if input.skins {
            screen.presence.insert(self.assembling, Value::Bool(true));
        }
        if input.hide_assemble {
            screen.variables.insert(HIDE_ASSEMBLE, Value::Bool(true));
        }
        let entry = Instance::new(entry_id, self.entry, Some((2, self.start_entry.index)));

        let mut instances = Vec::with_capacity(usize::from(entry_id));
        let hud = self.hud.map(|hud| {
            root.states.insert(hud.root_state.wire, subscript(4));
            let mut instance = Instance::new(4, hud.layout, Some((1, hud.root_state.index)));
            instance.states.insert(hud.shared_vars, BitWriter::new());
            instance
        });
        instances.extend([root, host, screen]);
        instances.extend(hud);
        instances.push(entry);

        let mut variables = Variables::from([
            (OPEN_SELECT, Value::Bool(input.select_open)),
            (SELECT_SHOWN, Value::Bool(input.select_open)),
        ]);
        if let Some(hero) = input.hero.filter(|&hero| hero != 0) {
            variables.insert(CURRENT_HERO, Value::Asset(hero));
        }
        owner_full(command_frame, &instances, &variables)
    }

    /// A remote 288D root and the three entity variables used by team selection.
    pub fn team_entry_frame(&self, hero: Option<u64>) -> Result<BitWriter, WireError> {
        team_entry(self.entry, hero)
    }
}

fn team_entry(layout: Layout, hero: Option<u64>) -> Result<BitWriter, WireError> {
    let entry = Instance::new(1, layout, None);
    let hero = hero.filter(|&hero| hero != 0);
    let mut variables = Variables::new();
    if let Some(hero) = hero {
        variables.insert(CURRENT_HERO, Value::Asset(hero));
    }
    variables.insert(PICKED, Value::Bool(hero.is_some()));
    if let Some(hero) = hero {
        variables.insert(PICKED_HERO, Value::Asset(hero));
    }
    // Bool and Asset have the same representation in compact and explicit frames.
    remote_full(&[entry], &variables)
}

fn graph(graphs: &GraphStore, index: u32) -> io::Result<&Graph> {
    graphs.graph(index).ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::NotFound,
            format!("missing controller graph {index:04X}"),
        )
    })
}

fn layout(graph: &Graph) -> io::Result<Layout> {
    Layout::try_from(graph).map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
}

fn owner_slot(graph: &Graph, index: u16) -> io::Result<StateSlot> {
    let wire = graph.owner_slot(usize::from(index)).ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "controller graph {:04X} lacks owner state {index}",
                graph.index
            ),
        )
    })?;
    Ok(StateSlot { index, wire })
}

fn presence_bit(graph: &Graph, id: u32) -> io::Result<usize> {
    graph.presence_bit(id).ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "controller graph {:04X} lacks presence variable {id}",
                graph.index
            ),
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    const SOLDIER: u64 = 0x02e0_0000_0000_006e;
    const REAPER: u64 = 0x02e0_0000_0000_0002;

    fn assert_frame(frame: &BitWriter, bits: usize, hex: &str) {
        assert_eq!(frame.bit_len(), bits);
        let actual: String = frame
            .as_bytes()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        assert_eq!(actual, hex);
    }

    #[test]
    fn team_entry_exact_wire_bytes_with_and_without_pick() {
        let layout = Layout {
            graph: TEAM_ENTRY as u16,
            owner_slots: 0,
            remote_slots: 0,
            presence_slots: 0,
        };
        let selected = team_entry(layout, Some(SOLDIER)).unwrap();
        assert_frame(&selected, 167, "88461400631dc8e002dc248400953693c105b80100");
        let choosing = team_entry(layout, None).unwrap();
        assert_frame(&choosing, 69, "884614001342000000");
        assert_eq!(team_entry(layout, Some(0)).unwrap(), choosing);
    }

    #[test]
    #[ignore = "requires an authorized 104319 runtime in ORPHEUS_RUNTIME_DIR"]
    fn practice_and_pvp_controller_frames_match_reference_graphs_and_bytes() {
        let runtime = PathBuf::from(
            std::env::var_os("ORPHEUS_RUNTIME_DIR").expect("set ORPHEUS_RUNTIME_DIR"),
        );
        let graphs =
            GraphStore::load(runtime.join("data").join("game_graphs_174.json.gz")).unwrap();
        let practice = Controller::load(&graphs, PRACTICE_RANGE_MODE).unwrap();
        let pvp = Controller::load(&graphs, 0).unwrap();
        let open = ControllerFrame {
            select_open: true,
            skins: true,
            ..ControllerFrame::default()
        };
        let extra = BTreeMap::from([(19, Value::Bool(false)), (0, Value::Bool(true))]);
        for (controller, command_frame, input, bits, expected) in [
            (
                practice,
                123456,
                open,
                446,
                "1a483c0040044f805044a1002bc4220a090020c84128800ad188420202801926402deb410040080000002000910458a3028700000000d80d",
            ),
            (
                practice,
                123456,
                ControllerFrame {
                    hide_assemble: true,
                    hero: Some(SOLDIER),
                    ..open
                },
                528,
                "1a483c0040044f805044a1002bc4220a090020c84128800ad188420202801926402deb41b10e6470016e00801000000040002209b046050e61572110000000006037",
            ),
            (
                practice,
                123457,
                ControllerFrame {
                    hero: Some(REAPER),
                    ..ControllerFrame::default()
                },
                489,
                "3a483c0040044f805044a1002bc4220a090020c84128800ad188420202801926002deb01b10e6470010200801000000040002209b0460506000000c06e00",
            ),
            (
                pvp,
                123456,
                open,
                485,
                "1a483c00404032805044a10077c4220a0900201a5148400030c304a8653d080000004200080000000000000000000000001200c01a113804000000c006",
            ),
            (
                pvp,
                123458,
                ControllerFrame {
                    presence: Some(&extra),
                    hero: Some(SOLDIER),
                    ..ControllerFrame::default()
                },
                534,
                "5a483c00404032805044a10077c4220a0900201a5148400030c304a0653d20d6810c2ec00d801000000100040000000000000000000000000900608d080c000000800d",
            ),
        ] {
            let frame = controller.controller_frame(command_frame, input).unwrap();
            assert_frame(&frame, bits, expected);
        }
        assert_frame(
            &practice.team_entry_frame(Some(SOLDIER)).unwrap(),
            167,
            "88461400631dc8e002dc248400953693c105b80100",
        );
        assert_eq!(
            pvp.controller_frame(0, open),
            Err(WireError::ZeroCommandFrame)
        );
    }
}
