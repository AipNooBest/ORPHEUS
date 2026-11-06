//! Loopback-only native server for the verified retail Practice Range path.

use std::{
    collections::HashMap,
    fs, io,
    net::{Ipv4Addr, SocketAddr, SocketAddrV4, TcpListener},
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread::{self, JoinHandle},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use crate::{
    accounts::{Account, Accounts, load_or_create_profile},
    bnet_protocol::Protocol,
    bnet_rpc::Player,
    bnet_service::{self, BnetService},
    catalog::ItemDb,
    collection::Collection,
    game_content::GameMap,
    game_service::GameService,
    jam_codec::Schemas,
    lobby_arcade::Arcade,
    lobby_celebrations::Celebrations,
    lobby_login::SIGNED_IN_ELSEWHERE,
    lobby_menu::{MenuHero, extra_preload_themes},
    lobby_practice::{self, SoloPracticeHandoff},
    lobby_presence::{PRO_ACCOUNT_BITS, Presence},
    lobby_ranked::{LeaderboardPlaces, Ranked},
    lobby_session::{LobbySession, LoginContent, LoginRequest, SoloLoginParty},
    match_handoff::{LobbySeat, general_channel, join_chat_channel},
    retail_templates::RetailTemplates,
    runtime::{BNET_DESCRIPTORS, missing_runtime_files},
    scene_assets::{PreparedScene, SceneAssets},
    service_tcp::{PeerStream, PeerWorkers, accept},
};

const MAX_LOBBY_PEERS: usize = 32;
const ACCEPT_POLL: Duration = Duration::from_millis(10);

pub struct ServerConfig {
    pub runtime: PathBuf,
    pub data: PathBuf,
    pub account: Option<String>,
    pub lobby_port: u16,
    pub game_port: u16,
    pub stall_port: u16,
    pub rpc_port: u16,
}

impl ServerConfig {
    pub fn new(runtime: PathBuf, data: PathBuf) -> Self {
        Self {
            runtime,
            data,
            account: None,
            lobby_port: 3724,
            game_port: 3730,
            stall_port: bnet_service::STALL_PORT,
            rpc_port: bnet_service::RPC_PORT,
        }
    }
}

pub struct Server {
    lobby_addr: SocketAddrV4,
    game_addr: SocketAddr,
    stop_lobby: mpsc::Sender<()>,
    lobby: Option<JoinHandle<io::Result<()>>>,
    bnet: BnetService,
    game: Option<Arc<GameService>>,
}

impl Server {
    /// Validate the data before starting the loopback listeners. This entry
    /// point serves the base graph bundle and contains no client launcher.
    pub fn start(config: ServerConfig) -> io::Result<Self> {
        let missing = missing_runtime_files(&config.runtime, true, true);
        if !missing.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                format!("missing runtime files: {}", missing.join(", ")),
            ));
        }
        let data = config.runtime.join("data");
        let signing_key = fs::read(data.join("lobby_state_blob.key"))?
            .try_into()
            .map_err(|_| invalid("lobby signing input must be 64 bytes"))?;
        let schemas = Schemas::load(data.join("schemas_174.json")).map_err(invalid)?;
        let game_schemas =
            Arc::new(Schemas::load(data.join("game_schemas_174.json")).map_err(invalid)?);
        let templates = RetailTemplates::load(data.join("retail_templates.json"))?;
        let items = Arc::new(ItemDb::load(&data)?);
        let collection = Collection::new(&templates, Arc::clone(&items))?;
        let presence = Presence::new(&templates)?;
        let ranked = Ranked::new(&templates)?;
        let arcade = Arcade::load(data.join("arcade_cards_174.json"), &templates)?;
        let celebrations = Celebrations::load(&data, &templates)?;
        let extra_skins = extra_preload_themes(&items).collect();
        let assets = SceneAssets::load(&config.runtime)?;
        let scene = assets.prepare(GameMap::practice_range())?;
        if !scene.movement_ready() {
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "the native Practice Range requires the base graph bundle",
            ));
        }
        let protocol =
            Arc::new(Protocol::load(config.runtime.join(BNET_DESCRIPTORS)).map_err(invalid)?);
        let template = config.data.join("profile.json");
        let accounts = Accounts::new(config.data.join("profiles"), &template)?;
        let local = local_account(
            &accounts,
            &config.data,
            &template,
            config.account.as_deref(),
        )?;
        let listener = TcpListener::bind(addr(config.lobby_port))?;
        listener.set_nonblocking(true)?;
        let SocketAddr::V4(lobby_addr) = listener.local_addr()? else {
            unreachable!("IPv4 lobby bind")
        };
        let game = Arc::new(GameService::bind(
            addr(config.game_port).into(),
            game_schemas,
        )?);
        let game_addr = game.local_addr();
        let bnet_account = Arc::clone(&local);
        let bnet = BnetService::start(
            addr(config.stall_port),
            addr(config.rpc_port),
            lobby_addr,
            protocol,
            move |_| {
                let account = u64::from(bnet_account.account_lo);
                Player {
                    account,
                    game_account: account ^ PRO_ACCOUNT_BITS,
                    battle_tag: bnet_account.battle_tag(),
                    name: bnet_account.name.clone(),
                }
            },
        )?;
        let shared = Arc::new(Lobby {
            signing_key,
            schemas,
            templates,
            collection,
            presence,
            ranked,
            arcade,
            celebrations,
            extra_skins,
            assets,
            first_scene: Mutex::new(Some(scene)),
            accounts,
            local,
            active: Mutex::new(HashMap::new()),
            game: Arc::clone(&game),
        });
        let (stop_lobby, shutdown) = mpsc::channel();
        let lobby = thread::Builder::new()
            .name("orpheus-lobby".into())
            .spawn(move || serve(listener, shared, shutdown))?;
        Ok(Self {
            lobby_addr,
            game_addr,
            stop_lobby,
            lobby: Some(lobby),
            bnet,
            game: Some(game),
        })
    }

    pub fn lobby_addr(&self) -> SocketAddrV4 {
        self.lobby_addr
    }
    pub fn game_addr(&self) -> SocketAddr {
        self.game_addr
    }
    pub fn stall_addr(&self) -> SocketAddr {
        self.bnet.stall_addr()
    }
    pub fn rpc_addr(&self) -> SocketAddr {
        self.bnet.rpc_addr()
    }

    pub fn stop(&mut self) -> io::Result<()> {
        let bnet = self.bnet.stop();
        let lobby = if let Some(worker) = self.lobby.take() {
            let _ = self.stop_lobby.send(());
            worker
                .join()
                .map_err(|_| io::Error::other("lobby listener panicked"))
                .and_then(|result| result)
        } else {
            Ok(())
        };
        let game = self.game.take().map_or(Ok(()), |game| {
            Arc::try_unwrap(game)
                .map_err(|_| io::Error::other("game worker still in use"))?
                .stop()
        });
        bnet.and(lobby).and(game)
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

fn local_account(
    accounts: &Accounts,
    data: &Path,
    template: &Path,
    chosen: Option<&str>,
) -> io::Result<Arc<Account>> {
    if let Some(name) = chosen.map(str::trim) {
        if name.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "empty local account name",
            ));
        }
        return accounts.get(name);
    }
    let saved = accounts.all_saved()?;
    let default = match fs::read_to_string(data.join("default_account.txt")) {
        Ok(name) => name.trim().to_owned(),
        Err(error) if error.kind() == io::ErrorKind::NotFound => String::new(),
        Err(error) => return Err(error),
    };
    if let Some(name) = saved
        .iter()
        .find(|name| name.eq_ignore_ascii_case(&default))
        .or(saved.first())
    {
        return accounts.get(name);
    }
    accounts.get(&load_or_create_profile(template)?.player_name)
}

struct Lobby {
    signing_key: [u8; 64],
    schemas: Schemas,
    templates: RetailTemplates,
    collection: Collection,
    presence: Presence,
    ranked: Ranked,
    arcade: Arcade,
    celebrations: Celebrations,
    extra_skins: Vec<u64>,
    assets: SceneAssets,
    first_scene: Mutex<Option<PreparedScene>>,
    accounts: Accounts,
    local: Arc<Account>,
    active: Mutex<HashMap<u32, Arc<AtomicBool>>>,
    game: Arc<GameService>,
}

struct LoginLease<'a> {
    lobby: &'a Lobby,
    account: Arc<Account>,
    replaced: Arc<AtomicBool>,
    seat: Option<LobbySeat>,
}

impl<'a> LoginLease<'a> {
    fn new(lobby: &'a Lobby, account: Arc<Account>) -> Self {
        let replaced = Arc::new(AtomicBool::new(false));
        let mut active = lobby.active.lock().expect("lobby accounts");
        if let Some(previous) = active.insert(account.account_lo, Arc::clone(&replaced)) {
            previous.store(true, Ordering::Release);
        }
        account.set_status(1);
        Self {
            lobby,
            account,
            replaced,
            seat: None,
        }
    }

    fn current(&self) -> bool {
        !self.replaced.load(Ordering::Acquire)
    }
}

impl Drop for LoginLease<'_> {
    fn drop(&mut self) {
        if let Some(seat) = self.seat.take() {
            let _ = self.lobby.game.release_seat(&seat.handoff);
        }
        let mut active = self.lobby.active.lock().expect("lobby accounts");
        if active
            .get(&self.account.account_lo)
            .is_some_and(|current| Arc::ptr_eq(current, &self.replaced))
        {
            active.remove(&self.account.account_lo);
            self.account.set_status(0);
        }
    }
}

impl Lobby {
    fn serve_peer(&self, stream: PeerStream, id: u32) -> io::Result<()> {
        let Some(mut session) = LobbySession::accept_peer(
            stream,
            id,
            &self.signing_key,
            &self.accounts,
            &self.schemas,
        )?
        else {
            return Ok(());
        };
        let login = match session.prepare_login(&self.accounts, || Ok(Arc::clone(&self.local)))? {
            LoginRequest::Prepared(login) => login,
            LoginRequest::Rejected { reason } => {
                session.send_disconnect(reason)?;
                return Ok(());
            }
            LoginRequest::Disconnected => return Ok(()),
        };
        let mut lease = LoginLease::new(self, Arc::clone(&login.account));
        let wall_time = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(io::Error::other)?
            .as_secs();
        let (menu, members_may_invite) = {
            let mut profile = login.account.profile();
            profile.last_online = wall_time as i64;
            let menu = MenuHero::default().select(
                &profile,
                self.collection.heroes(),
                self.collection.items(),
            )?;
            let members = profile
                .settings
                .get("+0x10D")
                .and_then(|settings| settings.get("+0x0"))
                .and_then(serde_json::Value::as_bool)
                .unwrap_or(true);
            (menu, members)
        };
        login.account.save()?;
        let messages = login.available_messages(
            &self.schemas,
            &self.templates,
            &self.collection,
            &self.presence,
            &self.ranked,
            LoginContent {
                menu_hero: menu.menu_guid,
                extra_skins: &self.extra_skins,
                wall_time,
                arcade: &self.arcade,
                celebrations: Some(&self.celebrations),
                leaderboard_places: &LeaderboardPlaces::new(),
                solo_party: Some(SoloLoginParty {
                    id: login.identity.party_id,
                    entity: login.identity.party_entity,
                    menu_hero: menu.party_hero,
                    members_may_invite,
                }),
            },
        )?;
        for message in messages {
            if !lease.current() {
                break;
            }
            session.send(&message)?;
        }
        if lease.current() {
            session.send(&join_chat_channel(general_channel()))?;
        }
        while let Some(request) = session.receive_message_until(|| lease.current())? {
            if !lobby_practice::is_request(&request) {
                continue;
            }
            if let Some(old) = &lease.seat {
                if self.game.seat_exists(&old.handoff)? {
                    continue;
                }
            }
            let input = SoloPracticeHandoff::new(match_id()?, "127.0.0.1");
            lease.seat = lobby_practice::dispatch_scene(
                &request,
                &login,
                &self.game,
                input,
                (&self.assets, &self.collection),
                |map| {
                    let first = self.first_scene.lock().expect("prepared scene").take();
                    first.map_or_else(|| self.assets.prepare(map), Ok)
                },
                |message| session.send(message),
            )?;
        }
        if !lease.current() {
            session.send_disconnect(SIGNED_IN_ELSEWHERE)?;
        }
        Ok(())
    }
}

fn serve(listener: TcpListener, lobby: Arc<Lobby>, shutdown: mpsc::Receiver<()>) -> io::Result<()> {
    let mut peers = PeerWorkers::default();
    let mut id = 0u32;
    while matches!(shutdown.try_recv(), Err(mpsc::TryRecvError::Empty)) {
        peers.reap();
        if let Some((socket, _)) = accept(&listener)? {
            if peers.len() < MAX_LOBBY_PEERS {
                id = id.wrapping_add(1);
                let lobby = Arc::clone(&lobby);
                let _ = peers.spawn("orpheus-lobby-peer", socket, move |stream| {
                    let _ = lobby.serve_peer(stream, id);
                });
            }
        } else if !matches!(
            shutdown.recv_timeout(ACCEPT_POLL),
            Err(mpsc::RecvTimeoutError::Timeout)
        ) {
            break;
        }
    }
    Ok(())
}

fn match_id() -> io::Result<[u64; 2]> {
    let mut random = [0; 16];
    getrandom::fill(&mut random).map_err(|error| io::Error::other(error.to_string()))?;
    Ok(std::array::from_fn(|i| {
        u64::from_le_bytes(random[i * 8..i * 8 + 8].try_into().unwrap()) >> 1
    }))
}

fn addr(port: u16) -> SocketAddrV4 {
    SocketAddrV4::new(Ipv4Addr::LOCALHOST, port)
}

fn invalid(error: impl std::fmt::Display) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error.to_string())
}
