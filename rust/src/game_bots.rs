//! Map AI spawners and the training bots' server-side patrol movement.
//!
//! The catalog and patrol behavior follow ow174/game/bots.py.

use std::{
    collections::{HashMap, VecDeque},
    f64::consts,
    fs, io,
    path::Path,
    sync::Arc,
    time::Duration,
};

use serde::Deserialize;

use crate::accounts::parse_guid;
use crate::contact_body::CharacterBody;
use crate::contact_math::{add, sub};
use crate::contact_world::ContactWorld;
use crate::game_clock::TICK_PERIOD;
use crate::mover_ground::{pogo, standing_body, standing_origin};
use crate::mover_settings::MoverSettings;
use crate::world::{Movement, NO_INPUT};

pub const BOT_ENTITY: u32 = 0xc000_1000;
pub const DEAD_MOVEMENT_FLAG: u32 = 0x2000;
pub const SETTLE_TICKS: u32 = 63;
pub const BOT_RESPAWN_TIME: Duration = Duration::from_secs(3);
pub const BOT_RECORD_DELAY: u32 = 8;

const PATROL: u64 = 0x0580_0000_0000_0b8e;
const FIRING: u64 = 0x0580_0000_0000_0b92;
const FIRST_WAIT: f64 = 3.0;
const WALK_GAP: f64 = 1.2;
const ARRIVED: f64 = 0.05;
const MAX_DETOUR_SAMPLES: usize = 64;
const STEP_UP: f64 = 0.6;
const DROP: f64 = 4.0;
const MAX_SLOPE: f64 = 1.0;
const FORWARD: f64 = 127.0;
const YAW_UNITS: f64 = 65536.0;
const TURN_RATE: f64 = YAW_UNITS / 6.0;
const TURN_AFTER: f64 = 0.5;
const CATCH_UP: u32 = 625;
const EPSILON: f64 = 1.0e-9;
const GRAVITY_SCALE: f64 = 1.75;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct BotWalk {
    pub speed: f64,
    pub acceleration: f64,
    pub brake: f64,
}

impl BotWalk {
    pub fn from_mover(run_forward: f64, accel_ground: f64, decel_ground_base: f64) -> Self {
        Self {
            speed: run_forward,
            acceleration: accel_ground * run_forward,
            brake: decel_ground_base * run_forward,
        }
    }
}

impl Default for BotWalk {
    fn default() -> Self {
        Self::from_mover(5.5, 35.0, 15.0)
    }
}

#[derive(Debug)]
pub struct BotSpawn {
    pub hero: u64,
    pub name: String,
    pub body: u64,
    pub team: u8,
    pub health: f64,
    pub position: [f64; 3],
    pub yaw: i32,
    pub firing: bool,
    pub route: Vec<[f64; 3]>,
    initial_index: i64,
}

#[derive(Debug, Default)]
pub struct BotCatalog {
    maps: HashMap<u64, Vec<Arc<BotSpawn>>>,
}

impl BotCatalog {
    pub fn load(path: impl AsRef<Path>) -> io::Result<Self> {
        let path = path.as_ref();
        match path.metadata() {
            Ok(metadata) if !metadata.is_file() => Ok(Self::default()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(Self::default()),
            Err(error) => Err(error),
            Ok(_) => Self::from_json(&fs::read_to_string(path)?),
        }
    }

    pub fn from_json(text: &str) -> io::Result<Self> {
        let raw: RawTable = serde_json::from_str(text)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        let mut maps = HashMap::with_capacity(raw.maps.len());
        for (map, entry) in raw.maps {
            let mut spawns = Vec::new();
            for row in entry.spawners {
                let Some(hero) = raw.heroes.get(&row.hero) else {
                    continue;
                };
                if row.identifier.is_some() {
                    continue;
                }
                let mut patrol = false;
                let mut firing = false;
                for graph in row.graphs {
                    let id = guid(&graph)?;
                    patrol |= id == PATROL;
                    firing |= id == FIRING;
                }
                let route = if patrol {
                    row.vars
                        .route
                        .iter()
                        .filter_map(|point| entry.hints.get(point).copied())
                        .collect()
                } else {
                    Vec::new()
                };
                let team = u8::try_from(row.team)
                    .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
                spawns.push(Arc::new(BotSpawn {
                    hero: guid(&row.hero)?,
                    name: hero.name.clone(),
                    body: guid(&hero.body)?,
                    team,
                    health: hero.health,
                    position: row.position,
                    yaw: yaw_of(row.rotation),
                    firing,
                    route,
                    initial_index: row.vars.index,
                }));
            }
            maps.insert(guid(&map)?, spawns);
        }
        Ok(Self { maps })
    }

    /// Entity IDs follow the authored order after excluding mode-specific
    /// spawners and unknown heroes. Body physics comes from the shared mover.
    pub fn make(&self, map_guid: u64, mover: impl Fn(u64) -> BotWalk) -> Vec<Bot> {
        self.maps
            .get(&map_guid)
            .into_iter()
            .flatten()
            .enumerate()
            .map(|(index, spawn)| {
                Bot::new(
                    BOT_ENTITY + index as u32,
                    Arc::clone(spawn),
                    mover(spawn.body),
                )
            })
            .collect()
    }
}

#[derive(Deserialize)]
struct RawTable {
    heroes: HashMap<String, RawHero>,
    maps: HashMap<String, RawMap>,
}

#[derive(Deserialize)]
struct RawHero {
    name: String,
    body: String,
    health: f64,
}

#[derive(Deserialize)]
struct RawMap {
    spawners: Vec<RawSpawner>,
    hints: HashMap<String, [f64; 3]>,
}

#[derive(Deserialize)]
struct RawSpawner {
    hero: String,
    team: i32,
    identifier: Option<String>,
    position: [f64; 3],
    rotation: [f64; 4],
    graphs: Vec<String>,
    #[serde(default)]
    vars: RawVars,
}

#[derive(Default, Deserialize)]
struct RawVars {
    #[serde(default, rename = "0x0D80000000001B23")]
    route: Vec<String>,
    #[serde(default, rename = "0x0D80000000000027")]
    index: i64,
}

fn guid(text: &str) -> io::Result<u64> {
    parse_guid(text).ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!("invalid bot catalog GUID {text:?}"),
        )
    })
}

fn s16(angle: f64) -> f64 {
    (angle + YAW_UNITS / 2.0).rem_euclid(YAW_UNITS) - YAW_UNITS / 2.0
}

fn yaw_of([_, y, _, w]: [f64; 4]) -> i32 {
    s16((y.atan2(w) * YAW_UNITS / consts::PI).round_ties_even()) as i32
}

fn heading([dx, dz]: [f64; 2]) -> f64 {
    s16((dx.atan2(dz) * YAW_UNITS / consts::TAU).round_ties_even())
}

fn throttles(yaw: f64, [dx, dz]: [f64; 2]) -> [i8; 2] {
    let (sin, cos) = (yaw * consts::TAU / YAW_UNITS).sin_cos();
    let right = -dx * cos + dz * sin;
    let forward = dx * sin + dz * cos;
    [
        (right * FORWARD).round_ties_even() as i8,
        (forward * FORWARD).round_ties_even() as i8,
    ]
}

fn planar_distance(a: [f64; 3], b: [f64; 3]) -> f64 {
    (a[0] - b[0]).hypot(a[2] - b[2])
}

type Detour = [Option<[f64; 3]>; 2];

#[derive(Debug)]
struct PatrolPlans {
    first_index: usize,
    first: Detour,
    legs: Vec<Detour>,
}

#[derive(Debug)]
struct PatrolCollision {
    body: CharacterBody,
    lift: f32,
}

impl PatrolCollision {
    fn clear(&self, from: [f64; 3], to: [f64; 3]) -> bool {
        self.body
            .sweep(
                standing_origin(from, self.lift),
                standing_origin(to, self.lift),
            )
            .is_ok_and(|hit| hit.is_none())
    }

    fn supported(
        &self,
        from: [f64; 3],
        to: [f64; 3],
        floor_tolerance: f64,
        ground: &mut impl FnMut(f64, f64, f64, f64) -> Option<f64>,
    ) -> bool {
        let width = 2.0 * f64::from(self.body.radius());
        let samples = (planar_distance(from, to) / width).ceil() as usize;
        if samples == 0 || samples > MAX_DETOUR_SAMPLES || !self.clear(from, to) {
            return false;
        }
        (0..=samples).all(|sample| {
            let t = sample as f64 / samples as f64;
            let [x, y, z] = std::array::from_fn(|axis| from[axis] + (to[axis] - from[axis]) * t);
            ground(x, z, y + floor_tolerance, y - floor_tolerance).is_some()
        })
    }

    fn advance(&mut self, from: [f64; 3], to: [f64; 3], dt: f64) -> [f64; 3] {
        let start = standing_origin(from, self.lift);
        self.body.teleport(start);
        let delta = sub(standing_origin(to, self.lift), start);
        match self.body.move_capsule(delta, dt as f32) {
            Ok(moved) if moved.position == add(start, delta) => to,
            Ok(moved) => {
                let [x, y, z] = moved.position.map(f64::from);
                [x, y - f64::from(self.lift), z]
            }
            Err(_) => from,
        }
    }
}

#[derive(Debug)]
pub struct Bot {
    pub entity: u32,
    pub spawn: Arc<BotSpawn>,
    pub position: [f64; 3],
    pub yaw: f64,
    pub velocity: [f64; 3],
    pub dead: bool,
    walk: BotWalk,
    pace: f64,
    direction: [f64; 2],
    index: usize,
    wait: f64,
    target: Option<[f64; 3]>,
    waypoints: VecDeque<[f64; 3]>,
    collision: Option<PatrolCollision>,
    plans: Option<Arc<PatrolPlans>>,
    start: [f64; 3],
    keys: bool,
    turning: f64,
    held: [i8; 2],
    last_held: [i8; 2],
    input_tick: Option<u32>,
    moved_tick: Option<u32>,
    last_tick: Option<u32>,
    records_from: Option<u32>,
}

impl Bot {
    fn new(entity: u32, spawn: Arc<BotSpawn>, walk: BotWalk) -> Self {
        let index = if spawn.route.is_empty() {
            0
        } else {
            spawn.initial_index.rem_euclid(spawn.route.len() as i64) as usize
        };
        Self {
            entity,
            position: spawn.position,
            yaw: f64::from(spawn.yaw),
            velocity: [0.0; 3],
            dead: false,
            walk,
            pace: 0.0,
            direction: [0.0, 1.0],
            index,
            wait: FIRST_WAIT,
            target: None,
            waypoints: VecDeque::new(),
            collision: None,
            plans: None,
            start: spawn.position,
            keys: false,
            turning: 0.0,
            held: [0; 2],
            last_held: [0; 2],
            input_tick: None,
            moved_tick: None,
            last_tick: None,
            records_from: None,
            spawn,
        }
    }

    pub(crate) fn attach_collision(
        &mut self,
        settings: &MoverSettings,
        world: Arc<ContactWorld>,
        category: u16,
        mut ground: impl FnMut(f64, f64, f64, f64) -> Option<f64>,
    ) {
        self.collision = Some(PatrolCollision {
            body: standing_body(settings, world, self.position, category),
            lift: pogo(settings, false) as f32,
        });
        let count = self.spawn.route.len();
        if count != 0 {
            let first_index = (self.index + 1) % count;
            self.plans = Some(Arc::new(PatrolPlans {
                first_index,
                first: self.detour(self.position, self.spawn.route[first_index], &mut ground),
                legs: (0..count)
                    .map(|index| {
                        let from = self.spawn.route[(index + count - 1) % count];
                        self.detour(from, self.spawn.route[index], &mut ground)
                    })
                    .collect(),
            }));
        }
    }

    fn detour(
        &self,
        from: [f64; 3],
        target: [f64; 3],
        ground: &mut impl FnMut(f64, f64, f64, f64) -> Option<f64>,
    ) -> Detour {
        let Some(collision) = &self.collision else {
            return [None; 2];
        };
        let same_floor = |point: [f64; 3]| (point[1] - from[1]).abs() <= ARRIVED;
        let distance = planar_distance(from, target);
        if distance <= ARRIVED || !same_floor(target) || collision.clear(from, target) {
            return [None; 2];
        }
        let hint = self
            .spawn
            .route
            .iter()
            .copied()
            .filter(|&point| {
                same_floor(point)
                    && planar_distance(from, point) > ARRIVED
                    && planar_distance(point, target) > ARRIVED
                    && collision.supported(from, point, STEP_UP, ground)
                    && collision.supported(point, target, STEP_UP, ground)
            })
            .min_by(|&a, &b| {
                let length = |point| planar_distance(from, point) + planar_distance(point, target);
                length(a).total_cmp(&length(b))
            });
        if hint.is_some() {
            return [hint, None];
        }

        // Try a body-width lane to either side. Each leg must fit the same
        // capsule and stay supported by the same floor as ordinary movement.
        let [x, _, z] = from;
        let [tx, _, tz] = target;
        let scale = 2.0 * f64::from(collision.body.radius()) / distance;
        for sign in [1.0, -1.0] {
            let offset = [(tz - z) * scale * sign, 0.0, (x - tx) * scale * sign];
            let side = |point: [f64; 3]| std::array::from_fn(|axis| point[axis] + offset[axis]);
            let [a, b] = [side(from), side(target)];
            if collision.supported(from, a, ARRIVED, ground)
                && collision.supported(a, b, ARRIVED, ground)
                && collision.supported(b, target, ARRIVED, ground)
            {
                return [Some(a), Some(b)];
            }
        }
        [None; 2]
    }

    fn planned_detour(&self, previous: usize) -> Detour {
        let Some(plans) = &self.plans else {
            return [None; 2];
        };
        if self.index == plans.first_index && self.position == self.spawn.position {
            return plans.first;
        }
        let from = self.spawn.route[previous];
        if planar_distance(self.position, from) <= ARRIVED
            && (self.position[1] - from[1]).abs() <= STEP_UP
        {
            plans.legs[self.index]
        } else {
            [None; 2]
        }
    }

    /// The floor provider can give a surface Y within the top and bottom
    /// probe bounds. A missing surface uses the authored hint heights.
    pub fn step(&mut self, tick: u32, mut ground: impl FnMut(f64, f64, f64, f64) -> Option<f64>) {
        if self.spawn.route.is_empty() || self.dead {
            return;
        }
        let ticks = self
            .last_tick
            .map_or(1, |last| tick.saturating_sub(last))
            .min(CATCH_UP);
        if ticks == 0 {
            return;
        }
        self.last_tick = Some(tick);
        let seconds = TICK_PERIOD.as_secs_f64();
        for at in tick - (ticks - 1)..=tick {
            self.tick(at, seconds, &mut ground);
        }
    }

    fn tick(
        &mut self,
        tick: u32,
        dt: f64,
        ground: &mut impl FnMut(f64, f64, f64, f64) -> Option<f64>,
    ) {
        let before = (self.position, self.yaw.round_ties_even());
        self.held = [0; 2];
        if !self.keys {
            self.wait -= dt;
            if self.target.is_none() && self.wait <= EPSILON {
                let previous = self.index;
                self.index = (self.index + 1) % self.spawn.route.len();
                let goal = self.spawn.route[self.index];
                let detour = self.planned_detour(previous);
                self.waypoints.extend(detour.into_iter().flatten());
                self.target = Some(self.waypoints.pop_front().unwrap_or(goal));
                if self.target != Some(goal) {
                    self.waypoints.push_back(goal);
                }
                self.start = self.position;
                self.keys = true;
                self.turning = 0.0;
            }
        }
        if self.keys {
            self.walk(tick, dt, ground);
        } else {
            if self.target.is_some() {
                self.slide(dt, ground);
            }
            if self.turning > EPSILON {
                self.turning -= dt;
                self.turn(dt);
            }
        }
        if (self.position, self.yaw.round_ties_even()) != before {
            self.moved_tick = Some(tick);
        }
    }

    fn walk(
        &mut self,
        tick: u32,
        dt: f64,
        ground: &mut impl FnMut(f64, f64, f64, f64) -> Option<f64>,
    ) {
        let mut offset = self.to_target();
        let mut distance = offset[0].hypot(offset[1]);
        if distance <= ARRIVED {
            if let Some(goal) = self.waypoints.pop_front() {
                self.target = Some(goal);
                self.start = self.position;
                offset = self.to_target();
                distance = offset[0].hypot(offset[1]);
            }
        }
        if distance <= ARRIVED {
            self.keys = false;
            self.wait = WALK_GAP;
            self.stand();
            return;
        }
        self.direction = offset.map(|axis| axis / distance);
        self.turn(dt);
        self.pace = self.walk.speed.min(self.pace + self.walk.acceleration * dt);
        self.held = throttles(self.yaw, self.direction);
        self.last_held = self.held;
        self.input_tick = Some(tick);
        let mut stride = self.pace * dt;
        if self.waypoints.is_empty() {
            let slide = self.slide_length(dt);
            if distance - stride <= slide {
                stride = (distance - slide).max(0.0);
                self.keys = false;
                self.wait = WALK_GAP;
                self.turning = TURN_AFTER;
            }
        }
        self.advance(stride, distance, dt, ground);
    }

    fn slide(&mut self, dt: f64, ground: &mut impl FnMut(f64, f64, f64, f64) -> Option<f64>) {
        self.pace = (self.pace - self.walk.brake * dt).max(0.0);
        let [dx, dz] = self.to_target();
        let distance = dx.hypot(dz);
        if self.pace > 0.0
            && distance > EPSILON
            && self.advance(self.pace * dt, distance, dt, ground) < distance - EPSILON
        {
            return;
        }
        self.stand();
    }

    fn stand(&mut self) {
        self.pace = 0.0;
        self.velocity = [0.0; 3];
        self.target = None;
        self.waypoints.clear();
    }

    fn to_target(&self) -> [f64; 2] {
        let [tx, _, tz] = self.target.expect("a walking bot has a target");
        [tx - self.position[0], tz - self.position[2]]
    }

    fn slide_length(&self, dt: f64) -> f64 {
        let slow = self.walk.brake * dt;
        let mut pace = self.pace - slow;
        let mut length = 0.0;
        if slow <= 0.0 && pace > 0.0 {
            return f64::INFINITY;
        }
        for _ in 0..CATCH_UP {
            if pace <= 0.0 {
                return length;
            }
            length += pace * dt;
            pace -= slow;
        }
        // Invalid or extremely weak braking must not stall the tick.
        f64::INFINITY
    }

    fn turn(&mut self, dt: f64) {
        let left = s16(heading(self.direction) - self.yaw);
        let units = TURN_RATE * dt;
        self.yaw = s16(self.yaw + left.clamp(-units, units));
    }

    fn advance(
        &mut self,
        mut stride: f64,
        distance: f64,
        dt: f64,
        ground: &mut impl FnMut(f64, f64, f64, f64) -> Option<f64>,
    ) -> f64 {
        let [x, y, z] = self.position;
        let [dx, dz] = self.direction;
        let probe = stride.min(distance);
        let probe_y = self.floor(x + dx * probe, y, z + dz * probe, ground);
        let rise = (probe_y - y).abs();
        if 0.0 < rise && rise <= MAX_SLOPE * probe {
            stride *= probe / probe.hypot(rise);
        }
        let reuse_probe = stride == probe && stride < distance;
        let [nx, nz] = if stride >= distance {
            stride = distance;
            let [tx, _, tz] = self.target.expect("a walking bot has a target");
            [tx, tz]
        } else {
            [x + dx * stride, z + dz * stride]
        };
        let ny = if reuse_probe {
            probe_y
        } else {
            self.floor(nx, y, nz, ground)
        };
        let intended = [nx, ny, nz];
        let [nx, ny, nz] = self.collision.as_mut().map_or(intended, |collision| {
            collision.advance(self.position, intended, dt)
        });
        self.position = [nx, ny, nz];
        self.velocity = [(nx - x) / dt, (ny - y) / dt, (nz - z) / dt];
        if self.position == intended {
            stride
        } else {
            ((nx - x) * dx + (nz - z) * dz).max(0.0)
        }
    }

    fn floor(
        &self,
        x: f64,
        y: f64,
        z: f64,
        ground: &mut impl FnMut(f64, f64, f64, f64) -> Option<f64>,
    ) -> f64 {
        if let Some(height) = ground(x, z, y + STEP_UP, y - DROP) {
            return height;
        }
        let [sx, sy, sz] = self.start;
        let [tx, ty, tz] = self.target.expect("a walking bot has a target");
        let whole = (tx - sx).hypot(tz - sz);
        let gone = if whole <= 0.0 {
            1.0
        } else {
            ((x - sx).hypot(z - sz) / whole).min(1.0)
        };
        sy + (ty - sy) * gone
    }

    pub fn walking(&self) -> bool {
        self.target.is_some()
    }

    pub fn died(&mut self, tick: u32) {
        self.dead = true;
        self.keys = false;
        self.held = [0; 2];
        self.stand();
        self.moved_tick = Some(tick);
    }

    pub fn respawned(&self, entity: u32, records_from: u32) -> Self {
        let mut fresh = Self::new(entity, Arc::clone(&self.spawn), self.walk);
        fresh.collision = self.collision.as_ref().map(|collision| PatrolCollision {
            body: collision
                .body
                .fresh_at(standing_origin(fresh.position, collision.lift)),
            lift: collision.lift,
        });
        fresh.plans = self.plans.clone();
        fresh.records_from = Some(records_from);
        fresh
    }

    pub fn record_due(&self, tick: u32, every: u32) -> bool {
        if self.records_from.is_some_and(|from| tick < from) {
            return false;
        }
        if self.walking()
            || self
                .moved_tick
                .is_some_and(|moved| tick.saturating_sub(moved) < SETTLE_TICKS)
        {
            return true;
        }
        let every = every.max(1);
        let period = (SETTLE_TICKS / every).max(1);
        (tick / every) % period == self.entity % period
    }

    pub fn movement(&self, tick: u32) -> Movement {
        let input_frame = if self.held != [0; 2] {
            None
        } else {
            match self.input_tick {
                None => Some(NO_INPUT),
                Some(frame) if frame < tick => Some(frame),
                Some(_) => None,
            }
        };
        Movement {
            position: self.position,
            yaw: s16(self.yaw.round_ties_even()) as i32,
            velocity: self.velocity,
            flags: if self.dead { DEAD_MOVEMENT_FLAG } else { 0 },
            throttles: self.held,
            input_frame,
            input_throttles: self.last_held,
            gravity: GRAVITY_SCALE,
            ..Movement::default()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::game_clock::SEND_EVERY;
    use crate::game_content::PRACTICE_RANGE_MAP;
    use crate::world::movement_record;
    use sha2::{Digest, Sha256};

    const TRAINING: u64 = 0x02e0_0000_0000_016b;
    const TRAINING_BODY: u64 = 0x0400_0000_0000_1403;
    const FAR_RUNNER: u64 = 0x02e0_0000_0000_016e;

    fn walker(target: [f64; 3]) -> Bot {
        Bot::new(
            BOT_ENTITY,
            Arc::new(BotSpawn {
                hero: TRAINING,
                name: "Training Bot".into(),
                body: TRAINING_BODY,
                team: 1,
                health: 200.0,
                position: [0.0; 3],
                yaw: 0,
                firing: false,
                route: vec![[0.0; 3], target],
                initial_index: 0,
            }),
            BotWalk::from_mover(4.5, 1.0, 15.0),
        )
    }

    fn route_vector(bots: &mut [Bot], ticks: impl IntoIterator<Item = u32>) -> String {
        let mut digest = Sha256::new();
        for tick in ticks {
            for bot in &mut *bots {
                bot.step(tick, |_, _, _, _| None);
                let bits = movement_record(&bot.movement(tick), Some(tick)).unwrap();
                digest.update(tick.to_le_bytes());
                digest.update(u32::try_from(bits.bit_len()).unwrap().to_le_bytes());
                digest.update([u8::from(bot.record_due(tick, SEND_EVERY))]);
                digest.update(bits.as_bytes());
            }
        }
        format!("{:x}", digest.finalize())
    }

    #[test]
    fn catalogs_only_unconditional_spawners_and_preserves_the_hint_sequence() {
        let catalog = BotCatalog::from_json(r#"{
            "heroes": {
                "0x02E000000000016B": {"name":"Fixture bot","body":"0x0400000000001403","health":200}
            },
            "maps": {"0x0800000000000688": {
                "hints": {"a": [1,2,3], "b": [4,5,6]},
                "spawners": [
                    {"hero":"0x02E000000000016B","team":0,"identifier":"0xF",
                     "position":[0,0,0],"rotation":[0,0,0,1],"graphs":[]},
                    {"hero":"0x02E0000000000000","team":0,
                     "position":[0,0,0],"rotation":[0,0,0,1],"graphs":[]},
                    {"hero":"0x02E000000000016B","team":1,"identifier":null,
                     "position":[2,3,4],"rotation":[0,0.70710678118,0,0.70710678118],
                     "graphs":["0x0580000000000B8E","0x0580000000000B92"],
                     "vars":{"0x0D80000000001B23":["b","missing","a","b"],"0x0D80000000000027":2}},
                    {"hero":"0x02E000000000016B","team":0,
                     "position":[0,0,0],"rotation":[0,0,0,1],"graphs":[],
                     "vars":{"0x0D80000000001B23":["a"]}}
                ]
            }}
        }"#).unwrap();
        let bots = catalog.make(PRACTICE_RANGE_MAP, |body| {
            assert_eq!(body, TRAINING_BODY);
            BotWalk::from_mover(4.5, 1.0, 15.0)
        });
        assert_eq!(bots.len(), 2);
        assert_eq!(bots[0].entity, BOT_ENTITY);
        assert_eq!(bots[1].entity, BOT_ENTITY + 1);
        assert_eq!(bots[0].spawn.hero, TRAINING);
        assert_eq!(bots[0].spawn.name, "Fixture bot");
        assert_eq!(bots[0].spawn.team, 1);
        assert_eq!(bots[0].spawn.health, 200.0);
        assert_eq!(bots[0].position, [2.0, 3.0, 4.0]);
        assert_eq!(bots[0].yaw, 16384.0);
        assert!(bots[0].spawn.firing);
        assert_eq!(
            bots[0].spawn.route,
            [[4.0, 5.0, 6.0], [1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
        );
        assert_eq!(bots[0].index, 2);
        assert!(!bots[1].spawn.firing);
        assert!(bots[1].spawn.route.is_empty());
        assert!(catalog.make(0, |_| unreachable!()).is_empty());
    }

    #[test]
    fn missing_spawner_file_leaves_an_empty_catalog() {
        let root = tempfile::tempdir().unwrap();
        let catalog = BotCatalog::load(root.path().join("ai_spawners_174.json")).unwrap();
        assert!(
            catalog
                .make(PRACTICE_RANGE_MAP, |_| unreachable!())
                .is_empty()
        );
    }

    #[test]
    fn backward_walk_brake_and_next_leg_match_python_wire_vectors() {
        // Python tests/test_bots.py::behind(), captured at every 16 ms tick.
        let mut bot = walker([0.0, 0.0, -20.0]);
        assert_eq!(
            route_vector(std::slice::from_mut(&mut bot), 1..=750),
            "80053f3ad91f5ca2e6e9fb638a2cb5fd1ac83851602e4079a6385c9ce39a7ee7"
        );
    }

    #[test]
    fn skipped_ticks_catch_up_without_replaying_a_duplicate_tick() {
        let mut contiguous = walker([0.0, 0.0, 10.0]);
        for tick in 1..=251 {
            contiguous.step(tick, |_, _, _, _| None);
        }
        let mut skipped = walker([0.0, 0.0, 10.0]);
        for tick in [1, 201, 251, 251, 100] {
            skipped.step(tick, |_, _, _, _| None);
        }
        assert_eq!(contiguous.movement(251), skipped.movement(251));

        let mut late = walker([0.0, 0.0, 10.0]);
        let mut bounded = walker([0.0, 0.0, 10.0]);
        late.step(1, |_, _, _, _| None);
        late.step(10_000, |_, _, _, _| None);
        bounded.step(10_000 - CATCH_UP, |_, _, _, _| None);
        bounded.step(10_000, |_, _, _, _| None);
        assert_eq!(late.movement(10_000), bounded.movement(10_000));
    }

    #[test]
    fn terrain_probe_uses_ramp_speed_and_falls_back_to_hint_height() {
        let mut ramp = walker([0.0, 10.0, 20.0]);
        let mut fallback = walker([0.0, 10.0, 20.0]);
        for tick in 1..=344 {
            ramp.step(tick, |_, z, top, bottom| {
                let height = 0.5 * z;
                (bottom..=top).contains(&height).then_some(height)
            });
            fallback.step(tick, |_, _, _, _| None);
        }
        let [_, y, z] = ramp.position;
        assert!((y - 0.5 * z).abs() < 1.0e-12);
        let [_, vy, vz] = ramp.velocity;
        assert!((vy.hypot(vz) - 4.5).abs() < 1.0e-8);
        assert!((vz - 4.5 / 1.0_f64.hypot(0.5)).abs() < 1.0e-8);
        assert!((fallback.position[1] - 0.5 * fallback.position[2]).abs() < 1.0e-12);
    }

    #[test]
    fn patrol_uses_the_shared_capsule_and_team_mask() {
        use crate::{
            collision_cache::{Mesh, Triangle},
            contact_mesh::ContactMesh,
            contact_world::{StaticGeometry, StaticShape},
        };

        let wall = Mesh {
            vertices: vec![
                [-10.0, 0.0, 2.0],
                [-10.0, 4.0, 2.0],
                [10.0, 0.0, 2.0],
                [10.0, 4.0, 2.0],
            ],
            triangles: vec![
                Triangle {
                    corners: [0, 1, 2],
                    wings: [-1, 3, -1],
                    mask: 0x08,
                    surface: 0,
                },
                Triangle {
                    corners: [3, 2, 1],
                    wings: [-1, 0, -1],
                    mask: 0x08,
                    surface: 0,
                },
            ],
        };
        let mesh = ContactMesh::new(wall, [1.0; 3], u16::MAX).unwrap();
        let wall = StaticShape::new(StaticGeometry::Mesh(Arc::new(mesh)), None).unwrap();
        let world = Arc::new(ContactWorld::new(vec![wall]).unwrap());
        let settings = MoverSettings::default();
        let mut blocked = walker([0.0, 0.0, 4.0]);
        let mut allowed = walker([0.0, 0.0, 4.0]);
        blocked.attach_collision(&settings, Arc::clone(&world), 0x08, |_, _, _, _| Some(0.0));
        allowed.attach_collision(&settings, world, 0x10, |_, _, _, _| Some(0.0));
        for tick in 1..=295 {
            for bot in [&mut blocked, &mut allowed] {
                bot.step(tick, |_, _, _, _| Some(0.0));
            }
        }
        assert!(blocked.position[2] < 2.0);
        assert!(allowed.position[2] > 3.0);
        assert!(blocked.velocity[2].abs() < 0.05);
    }

    #[test]
    fn death_stops_the_bot_and_respawn_restarts_the_route_after_create() {
        let mut bot = walker([0.0, 0.0, 10.0]);
        bot.step(1, |_, _, _, _| None);
        bot.step(250, |_, _, _, _| None);
        assert!(bot.walking());
        let death_position = bot.position;
        bot.died(250);
        bot.step(400, |_, _, _, _| unreachable!());
        assert!(!bot.walking());
        assert_eq!(bot.position, death_position);
        let state = bot.movement(250);
        assert_eq!(state.flags, DEAD_MOVEMENT_FLAG);
        assert_eq!(state.velocity, [0.0; 3]);
        assert_eq!(state.throttles, [0; 2]);
        assert_eq!(state.input_frame, None);
        assert_eq!(bot.movement(251).input_frame, Some(250));
        assert!(bot.record_due(250 + SETTLE_TICKS - 1, SEND_EVERY));

        let replacement = BOT_ENTITY + 15;
        let tick = 250
            + BOT_RESPAWN_TIME
                .as_millis()
                .div_ceil(TICK_PERIOD.as_millis()) as u32;
        let records_from = tick + BOT_RECORD_DELAY;
        let mut fresh = bot.respawned(replacement, records_from);
        assert!(Arc::ptr_eq(&fresh.spawn, &bot.spawn));
        assert_eq!(fresh.entity, replacement);
        assert!(!fresh.dead);
        assert_eq!(fresh.position, [0.0; 3]);
        assert_eq!(fresh.movement(tick).input_frame, Some(NO_INPUT));
        let turn = |at: &u32| at % SETTLE_TICKS == fresh.entity % SETTLE_TICKS;
        let before = (records_from - SETTLE_TICKS..records_from)
            .find(turn)
            .unwrap();
        let after = (records_from..records_from + SETTLE_TICKS)
            .find(turn)
            .unwrap();
        assert!(!fresh.record_due(before, 1));
        assert!(fresh.record_due(after, 1));
        fresh.step(tick, |_, _, _, _| None);
        fresh.step(tick + 186, |_, _, _, _| None);
        assert!(!fresh.walking());
        fresh.step(tick + 187, |_, _, _, _| None);
        assert!(fresh.walking());
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 AI spawner and mover tables"]
    fn fifteen_practice_bots_match_python_roster_and_patrol_frames() {
        use std::path::PathBuf;

        let root = std::env::var_os("ORPHEUS_RUNTIME_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).to_owned());
        let data = root.join("data");
        let catalog = BotCatalog::load(data.join("ai_spawners_174.json")).unwrap();
        let raw_movers: HashMap<String, serde_json::Value> =
            serde_json::from_str(&fs::read_to_string(data.join("game_movers_174.json")).unwrap())
                .unwrap();
        let mut bots = catalog.make(PRACTICE_RANGE_MAP, |body| {
            let values = raw_movers
                .get(&format!("0x{body:016X}"))
                .or_else(|| raw_movers.get("0x04000000000003CF"));
            let number = |key, default| {
                values
                    .and_then(|values| values.get(key))
                    .and_then(serde_json::Value::as_f64)
                    .unwrap_or(default)
            };
            BotWalk::from_mover(
                number("run_forward", 5.5),
                number("accel_ground", 35.0),
                number("decel_ground_base", 15.0),
            )
        });
        assert_eq!(bots.len(), 15);
        assert_eq!(bots.iter().filter(|bot| bot.spawn.team == 1).count(), 11);
        assert_eq!(bots.iter().filter(|bot| bot.spawn.team == 0).count(), 4);
        assert_eq!(
            bots.iter()
                .filter(|bot| !bot.spawn.route.is_empty())
                .count(),
            6
        );
        assert_eq!(
            bots.iter()
                .filter(|bot| bot.spawn.hero == FAR_RUNNER)
                .count(),
            4
        );

        let mut roster = Sha256::new();
        for (index, bot) in bots.iter().enumerate() {
            let spawn = &bot.spawn;
            assert_eq!(bot.entity, BOT_ENTITY + index as u32);
            roster.update(bot.entity.to_le_bytes());
            roster.update(spawn.hero.to_le_bytes());
            roster.update(spawn.body.to_le_bytes());
            roster.update([spawn.team]);
            roster.update((spawn.health as f32).to_le_bytes());
            roster.update(spawn.yaw.to_le_bytes());
            roster.update([u8::from(spawn.firing)]);
            roster.update(u32::try_from(spawn.name.len()).unwrap().to_le_bytes());
            roster.update(spawn.name.as_bytes());
            for value in bot.position.into_iter().chain([
                bot.walk.speed,
                bot.walk.acceleration,
                bot.walk.brake,
            ]) {
                roster.update((value as f32).to_le_bytes());
            }
            roster.update(i32::try_from(spawn.initial_index).unwrap().to_le_bytes());
            roster.update(u32::try_from(spawn.route.len()).unwrap().to_le_bytes());
            for &point in &spawn.route {
                for value in point {
                    roster.update((value as f32).to_le_bytes());
                }
            }
        }
        // Generated by Python bots.make and world.movement_record with no floor data.
        assert_eq!(
            format!("{:x}", roster.finalize()),
            "04745e4f99dbbfa08bdef514d77ccc0b8e023e769efa4f5fb58e6f794f50ac4b"
        );
        assert_eq!(
            route_vector(&mut bots, (1..=1300).chain([1325, 1955, 1956, 1960])),
            "b149457ffd1e86f976def1ffb6d53c7a955582cced2fbcc03fd424c1a0a893e9"
        );
    }
}
