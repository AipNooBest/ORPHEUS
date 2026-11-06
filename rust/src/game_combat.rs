//! Match-authoritative direct damage and training bot lifetimes.
//!
//! The projectile and body scripts supply shot events. The match supplies
//! the command and pose at consumption, and owns identity-gated delivery.
//! Hit selection, damage, and respawn order follow ow174/game/combat.py.

use std::{io, time::Duration};

use crate::{
    commands::Command,
    game_bots::{BOT_ENTITY, BOT_RECORD_DELAY, BOT_RESPAWN_TIME},
    game_combat_messages::CombatNotice,
    game_heroes::SOLDIER,
    game_hitscan::{HitDefinition, HitscanShot, TargetHistory, ViewFrame},
    match_scene::bot_create,
    scene_assets::PreparedScene,
    vitals::{Death, Vitals},
    world::{EntityUpdate, Operation},
};

pub(crate) fn eye_height(hero: u64, crouched: bool) -> f64 {
    match (crouched, hero) {
        (false, _) => 1.55,
        (true, SOLDIER) => 1.085,
        (true, _) => 1.15,
    }
}

#[derive(Clone, Copy)]
pub(crate) struct PosedHitscan {
    pub source: u32,
    pub origin: [f64; 3],
    pub command: Command,
    pub shot_frame: u32,
    pub reach: f64,
    pub hit: HitDefinition,
}

#[derive(Clone, Copy)]
pub(crate) struct ShotContext {
    pub team: u8,
    pub quanta: u32,
    pub tick: u32,
    pub now: Duration,
}

pub(crate) struct BotHit {
    pub notices: [Option<CombatNotice>; 2],
    pub health: EntityUpdate,
}

#[derive(Clone)]
pub(crate) struct BotReplacement {
    pub retired: u32,
    pub created: EntityUpdate,
}

struct Target {
    vitals: Vitals,
    history: TargetHistory,
    respawn_at: Option<Duration>,
}

impl Target {
    fn new(health: f64, replacement: Option<u32>) -> Self {
        Self {
            vitals: Vitals::new(health, 0.0, 0.0),
            history: TargetHistory::new(replacement),
            respawn_at: None,
        }
    }
}

/// One slot per map spawner, independent of the network ID of the current bot.
pub(crate) struct Combat {
    targets: Vec<Target>,
    next_bot: u32,
}

impl Combat {
    pub(crate) fn new(scene: &PreparedScene) -> Self {
        Self {
            targets: scene
                .bots()
                .iter()
                .map(|bot| Target::new(bot.spawn.health, None))
                .collect(),
            next_bot: BOT_ENTITY + scene.bots().len() as u32,
        }
    }

    pub(crate) fn record_poses(&mut self, scene: &PreparedScene, tick: u32) {
        for (target, bot) in self.targets.iter_mut().zip(scene.bots()) {
            if !bot.dead {
                target.history.record(tick, scene.bot_target(bot));
            }
        }
    }

    pub(crate) fn hitscan(
        &mut self,
        scene: &mut PreparedScene,
        shot: PosedHitscan,
        context: ShotContext,
        visible: impl Fn(u32) -> bool,
    ) -> io::Result<Option<BotHit>> {
        let view = ViewFrame::from_command(&shot.command, context.quanta);
        let ray = HitscanShot::from_command(shot.origin, &shot.command, shot.reach, shot.hit);
        let candidates = scene
            .bots()
            .iter()
            .zip(&self.targets)
            .filter_map(|(bot, target)| {
                if bot.dead
                    || bot.entity == shot.source
                    || !visible(bot.entity)
                    || (!scene.map().free_for_all && bot.spawn.team == context.team)
                {
                    return None;
                }
                target
                    .history
                    .at(view, scene.bot_target(bot))
                    .map(|pose| (bot.entity, pose))
            });
        let Some(impact) = ray
            .resolve(candidates, |origin, direction, reach| {
                scene.hitscan_wall(origin, direction, reach)
            })
            .map_err(io::Error::other)?
        else {
            return Ok(None);
        };
        let (bot, target) = scene
            .bots_mut()
            .iter_mut()
            .zip(&mut self.targets)
            .find(|(bot, _)| bot.entity == impact.entity)
            .expect("hitscan selected a current bot");
        let result = target.vitals.damage(
            impact.damage,
            Death {
                time: context.now.as_secs_f64(),
                frame: context.tick,
                killer: shot.source,
            },
        );
        if result.dealt == 0.0 {
            return Ok(None);
        }
        if result.killed {
            bot.died(context.tick);
            target.respawn_at = Some(context.now.saturating_add(BOT_RESPAWN_TIME));
        }
        let health = EntityUpdate {
            entity: bot.entity,
            operation: Some(Operation::Update(vec![target.vitals.component()])),
            ..EntityUpdate::default()
        };
        target.vitals.clear_changed();
        Ok(Some(BotHit {
            notices: [
                Some(CombatNotice::Hit {
                    target: bot.entity,
                    dealt: result.dealt,
                    critical: impact.critical,
                    killed: result.killed,
                    shot_frame: shot.shot_frame,
                }),
                result.killed.then_some(CombatNotice::Elimination {
                    killer: shot.source,
                    victim: bot.entity,
                }),
            ],
            health,
        }))
    }

    pub(crate) fn respawn_due(
        &mut self,
        scene: &mut PreparedScene,
        tick: u32,
        now: Duration,
    ) -> io::Result<Vec<BotReplacement>> {
        let mut prepared = Vec::new();
        let mut next_bot = self.next_bot;
        for (slot, (bot, target)) in scene.bots().iter().zip(&self.targets).enumerate() {
            if !target.respawn_at.is_some_and(|deadline| deadline <= now) {
                continue;
            }
            let fresh = bot.respawned(next_bot, tick.saturating_add(BOT_RECORD_DELAY));
            next_bot = next_bot
                .checked_add(1)
                .ok_or_else(|| io::Error::other("bot entity IDs exhausted"))?;
            let created = bot_create(&fresh, None)?;
            prepared.push((
                slot,
                fresh,
                BotReplacement {
                    retired: bot.entity,
                    created,
                },
            ));
        }
        self.next_bot = next_bot;
        Ok(prepared
            .into_iter()
            .map(|(slot, fresh, replacement)| {
                self.targets[slot] = Target::new(fresh.spawn.health, Some(tick));
                scene.bots_mut()[slot] = fresh;
                replacement
            })
            .collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{game_content::GameMap, game_instance::RETAIL_QUANTA, scene_assets::SceneAssets};

    #[test]
    #[ignore = "requires a prepared 104319 Practice Range scene"]
    fn repeated_rifle_hits_retire_and_respawn_only_the_hit_bot() {
        let runtime = std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap();
        let mut scene = SceneAssets::load(runtime)
            .unwrap()
            .prepare(GameMap::practice_range())
            .unwrap();
        let slot = scene
            .bots()
            .iter()
            .position(|bot| bot.spawn.team == 1)
            .unwrap();
        let bot = &scene.bots()[slot];
        let entity = bot.entity;
        let [x, y, z] = bot.position;
        let count = scene.bots().len();
        let mut combat = Combat::new(&scene);
        let mut context = ShotContext {
            team: 0,
            quanta: RETAIL_QUANTA,
            tick: 1000,
            now: Duration::from_secs(4),
        };
        combat.record_poses(&scene, context.tick);
        let mut shot = PosedHitscan {
            source: 0xa000_0101,
            origin: [x - 8.0, y + eye_height(SOLDIER, false), z],
            command: Command {
                frame: context.tick,
                yaw: 16384,
                ..Command::default()
            },
            shot_frame: 999,
            reach: 180.0,
            hit: HitDefinition {
                amount: 20.0,
                loadout: None,
                critical: 2.0,
                falloff: None,
            },
        };
        assert!(
            combat
                .hitscan(&mut scene, shot, context, |_| false)
                .unwrap()
                .is_none()
        );
        assert!(
            combat
                .hitscan(&mut scene, shot, ShotContext { team: 1, ..context }, |_| {
                    true
                })
                .unwrap()
                .is_none()
        );
        for index in 0..5 {
            let result = combat
                .hitscan(&mut scene, shot, context, |_| true)
                .unwrap()
                .unwrap();
            let terminal = index == 4;
            assert_eq!(result.health.entity, entity);
            assert_eq!(
                result.notices[0],
                Some(CombatNotice::Hit {
                    target: entity,
                    dealt: 40.0,
                    critical: true,
                    killed: terminal,
                    shot_frame: shot.shot_frame,
                })
            );
            assert_eq!(
                result.notices[1],
                terminal.then_some(CombatNotice::Elimination {
                    killer: shot.source,
                    victim: entity,
                })
            );
            assert_eq!(
                combat.targets[slot].vitals.total(),
                160.0 - f64::from(index) * 40.0
            );
            assert_eq!(scene.bots()[slot].dead, terminal);
        }
        assert!(
            combat
                .hitscan(&mut scene, shot, context, |_| true)
                .unwrap()
                .is_none()
        );
        let due = context.now + BOT_RESPAWN_TIME;
        assert!(
            combat
                .respawn_due(&mut scene, 1187, due - Duration::from_millis(1))
                .unwrap()
                .is_empty()
        );
        let [replacement] = combat
            .respawn_due(&mut scene, 1188, due)
            .unwrap()
            .try_into()
            .ok()
            .unwrap();
        assert_eq!(replacement.retired, entity);
        let next = BOT_ENTITY + count as u32;
        assert_eq!(replacement.created.entity, next);
        assert!(matches!(
            replacement.created.operation,
            Some(Operation::Create(_))
        ));
        assert!(!scene.bots()[slot].dead);
        assert_eq!(combat.targets[slot].vitals.total(), 200.0);
        context.tick = 1188;
        context.now = due;
        combat.record_poses(&scene, context.tick);
        assert!(
            combat
                .hitscan(&mut scene, shot, context, |_| true)
                .unwrap()
                .is_none()
        );
        shot.command.frame = context.tick;
        assert!(
            combat
                .hitscan(&mut scene, shot, context, |visible| visible == next)
                .unwrap()
                .is_some()
        );
        assert_eq!(combat.targets[slot].vitals.total(), 160.0);
    }
}
