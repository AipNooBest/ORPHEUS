//! Prepare an external pack from locally supplied runtime data.
//!
//! Ports `tools/prepare_server_runtime.py` and the file inventory in `ow174/paths.py`.

use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fmt;
use std::fs::{self, File};
use std::io::{self, BufReader, BufWriter, Read, Write};
use std::path::{Component, Path, PathBuf};

pub const LOBBY_RUNTIME_DATA: &[&str] = &[
    "schemas_174.json",
    "retail_templates.json",
    "extracted_items.json",
    "extracted_general_unlocks.json",
    "extracted_heroes.json",
    "extracted_maps.json",
    "resource_keys_174.json",
    "arcade_cards_174.json",
    "game_queues_174.json",
    "lobby_state_blob.key",
];
pub const MATCH_RUNTIME_DATA: &[&str] = &[
    "ai_spawners_174.json",
    "collision_keys_174.json",
    "collision_props_174.json",
    "game_graphs_174.json.gz",
    "game_heroes_174.json",
    "game_maps_174.json",
    "game_movers_174.json",
    "game_schemas_174.json",
    "game_stats_174.json",
    "statescript_codecs_174.json",
];
pub const BNET_DESCRIPTORS: &str = "bnet/descriptors.pb";
const MANIFEST_SCHEMA: &str = "orpheus-runtime-copy-v1";

/// Names of required inputs absent from a selected external runtime root.
pub fn missing_runtime_files(root: &Path, matches: bool, bnet: bool) -> Vec<String> {
    let match_files = if matches { MATCH_RUNTIME_DATA } else { &[] };
    LOBBY_RUNTIME_DATA
        .iter()
        .chain(match_files)
        .map(|name| format!("data/{name}"))
        .chain(bnet.then(|| BNET_DESCRIPTORS.to_owned()))
        .filter(|name| !root.join(name).is_file())
        .collect()
}

#[derive(Debug)]
pub enum RuntimeError {
    Input(String),
    Io { path: PathBuf, source: io::Error },
    Manifest(serde_json::Error),
}

impl fmt::Display for RuntimeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Input(message) => f.write_str(message),
            Self::Io { path, source } => write!(f, "{}: {source}", path.display()),
            Self::Manifest(error) => write!(f, "could not write the runtime manifest: {error}"),
        }
    }
}

impl std::error::Error for RuntimeError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { source, .. } => Some(source),
            Self::Manifest(error) => Some(error),
            Self::Input(_) => None,
        }
    }
}

fn at<T>(path: &Path, result: io::Result<T>) -> Result<T, RuntimeError> {
    result.map_err(|source| RuntimeError::Io {
        path: path.to_owned(),
        source,
    })
}

fn destination_path(path: &Path) -> Result<PathBuf, RuntimeError> {
    let absolute = at(path, std::path::absolute(path))?;
    let mut resolved = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::ParentDir => {
                resolved.pop();
            }
            Component::Normal(name) => {
                resolved.push(name);
                match fs::canonicalize(&resolved) {
                    Ok(path) => resolved = path,
                    Err(error) if error.kind() == io::ErrorKind::NotFound => (),
                    Err(error) => return at(&resolved, Err(error)),
                }
            }
            _ => resolved.push(component.as_os_str()),
        }
    }
    Ok(resolved)
}

fn is_map_index(text: &str) -> bool {
    (4..=8).contains(&text.len()) && text.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn caches(cache: &Path, maps: &[String]) -> Result<Vec<PathBuf>, RuntimeError> {
    if !maps.is_empty() {
        if maps.iter().any(|map| !is_map_index(map)) {
            return Err(RuntimeError::Input(
                "Map indexes must contain four to eight hexadecimal digits".into(),
            ));
        }
        return Ok(maps
            .iter()
            .map(|map| cache.join(format!("{}.col", map.to_ascii_uppercase())))
            .collect());
    }

    let entries = match fs::read_dir(cache) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return at(cache, Err(error)),
    };
    let mut paths = Vec::new();
    for entry in entries {
        let path = at(cache, entry)?.path();
        if path.extension().is_some_and(|extension| extension == "col")
            && path
                .file_stem()
                .and_then(|name| name.to_str())
                .is_some_and(is_map_index)
        {
            paths.push(path);
        }
    }
    Ok(paths)
}

fn inputs(
    source: &Path,
    maps: &[String],
    game_graphs: Option<&Path>,
) -> Result<BTreeMap<String, PathBuf>, RuntimeError> {
    let mut inputs = BTreeMap::new();
    for name in LOBBY_RUNTIME_DATA.iter().chain(MATCH_RUNTIME_DATA) {
        let member = format!("data/{name}");
        inputs.insert(member.clone(), source.join(member));
    }
    if let Some(graphs) = game_graphs {
        inputs.insert("data/game_graphs_174.json.gz".into(), graphs.to_owned());
    }
    inputs.insert(
        BNET_DESCRIPTORS.into(),
        source.join("ow174").join(BNET_DESCRIPTORS),
    );

    let caches = caches(&source.join("cache/collision"), maps)?;
    if caches.is_empty() {
        return Err(RuntimeError::Input(
            "No prepared collision caches were found".into(),
        ));
    }
    for cache in caches {
        let rays = cache.with_extension("rays.col");
        for path in std::iter::once(cache).chain(rays.is_file().then_some(rays)) {
            // The index and suffix were limited to ASCII by caches().
            let filename = path
                .file_name()
                .and_then(|name| name.to_str())
                .expect("ASCII cache name");
            inputs.insert(format!("collision/{filename}"), path);
        }
    }

    let missing: Vec<_> = inputs
        .iter()
        .filter(|(_, path)| !path.is_file())
        .map(|(name, _)| name.as_str())
        .collect();
    if !missing.is_empty() {
        return Err(RuntimeError::Input(format!(
            "Missing runtime inputs: {}",
            missing.join(", ")
        )));
    }
    Ok(inputs)
}

struct OutputDirectory<'a> {
    root: &'a Path,
    files: Vec<PathBuf>,
    directories: Vec<PathBuf>,
    complete: bool,
}

impl<'a> OutputDirectory<'a> {
    fn create(root: &'a Path) -> Result<Self, RuntimeError> {
        if let Some(parent) = root.parent() {
            at(parent, fs::create_dir_all(parent))?;
        }
        at(root, fs::create_dir(root))?;
        Ok(Self {
            root,
            files: Vec::new(),
            directories: Vec::new(),
            complete: false,
        })
    }

    fn mkdir(&mut self, name: &str) -> Result<(), RuntimeError> {
        let path = self.root.join(name);
        at(&path, fs::create_dir(&path))?;
        self.directories.push(path);
        Ok(())
    }

    fn file(&mut self, name: &str) -> Result<(PathBuf, BufWriter<File>), RuntimeError> {
        let path = self.root.join(name);
        let file = at(&path, File::create_new(&path))?;
        self.files.push(path.clone());
        Ok((path, BufWriter::new(file)))
    }
}

impl Drop for OutputDirectory<'_> {
    fn drop(&mut self) {
        if !self.complete {
            // Remove only files and empty directories created by this invocation.
            for file in self.files.iter().rev() {
                let _ = fs::remove_file(file);
            }
            for directory in self.directories.iter().rev() {
                let _ = fs::remove_dir(directory);
            }
            let _ = fs::remove_dir(self.root);
        }
    }
}

fn copy_and_hash(
    source: &Path,
    destination: &Path,
    output: &mut impl Write,
) -> Result<String, RuntimeError> {
    let mut input = BufReader::new(at(source, File::open(source))?);
    let mut sha256 = Sha256::new();
    let mut buffer = [0; 32768];
    loop {
        let count = at(source, input.read(&mut buffer))?;
        if count == 0 {
            break;
        }
        let bytes = &buffer[..count];
        at(destination, output.write_all(bytes))?;
        sha256.update(bytes);
    }
    at(destination, output.flush())?;
    Ok(format!("{:x}", sha256.finalize()))
}

/// Copies a runtime pack to a fresh destination and writes its SHA-256 manifest last.
pub fn prepare(
    source: &Path,
    destination: &Path,
    maps: &[String],
    game_graphs: Option<&Path>,
) -> Result<PathBuf, RuntimeError> {
    if !source.is_dir() {
        return Err(RuntimeError::Input(
            "The prepared checkout does not exist".into(),
        ));
    }
    let source = at(source, fs::canonicalize(source))?;
    let destination = destination_path(destination)?;
    let exists = match fs::symlink_metadata(&destination) {
        Ok(_) => true,
        Err(error) if error.kind() == io::ErrorKind::NotFound => false,
        Err(error) => return at(&destination, Err(error)),
    };
    if exists || destination.starts_with(&source) {
        return Err(RuntimeError::Input(
            "Choose a new destination outside the source checkout".into(),
        ));
    }

    let inputs = inputs(&source, maps, game_graphs)?;
    let mut output = OutputDirectory::create(&destination)?;
    for directory in ["data", "bnet", "collision"] {
        output.mkdir(directory)?;
    }
    let mut hashes = BTreeMap::new();
    for (name, input) in inputs {
        let (path, mut file) = output.file(&name)?;
        hashes.insert(name, copy_and_hash(&input, &path, &mut file)?);
    }
    let (path, mut file) = output.file("manifest.json")?;
    let manifest = serde_json::json!({"schema": MANIFEST_SCHEMA, "sha256": hashes});
    serde_json::to_writer_pretty(&mut file, &manifest).map_err(RuntimeError::Manifest)?;
    at(&path, file.write_all(b"\n"))?;
    at(&path, file.flush())?;
    output.complete = true;
    drop(output);
    Ok(destination)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    struct Fixture {
        temp: PathBuf,
        root: PathBuf,
    }

    impl Fixture {
        fn new() -> Self {
            static NEXT: AtomicUsize = AtomicUsize::new(0);
            let temp = fs::canonicalize(std::env::temp_dir()).unwrap();
            let stamp = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let root = temp.join(format!(
                "orpheus-runtime-test-{}-{stamp}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&root).unwrap();
            Self { temp, root }
        }

        fn write(&self, name: &str, bytes: &[u8]) {
            let path = self.root.join(name);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, bytes).unwrap();
        }

        fn source(&self) -> PathBuf {
            for name in LOBBY_RUNTIME_DATA.iter().chain(MATCH_RUNTIME_DATA) {
                self.write(&format!("source/data/{name}"), name.as_bytes());
            }
            self.write("source/ow174/bnet/descriptors.pb", b"synthetic descriptor");
            self.write("source/cache/collision/0688.col", b"practice");
            self.write("source/cache/collision/0688.rays.col", b"practice rays");
            self.write("source/cache/collision/ABCD.col", b"another map");
            self.write("source/cache/collision/not-a-map.col", b"ignore me");
            self.root.join("source")
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            assert_eq!(self.root.parent(), Some(self.temp.as_path()));
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn copies_selected_map_and_optional_rays_and_hashes_written_files() {
        let fixture = Fixture::new();
        let source = fixture.source();
        fixture.write("graphs.json.gz", b"synthetic replacement graphs");
        let destination = fixture.root.join("new/pack");
        let output = prepare(
            &source,
            &destination,
            &["0688".into()],
            Some(&fixture.root.join("graphs.json.gz")),
        )
        .unwrap();
        assert_eq!(output, fs::canonicalize(&destination).unwrap());
        assert!(missing_runtime_files(&output, true, true).is_empty());
        assert_eq!(
            fs::read(output.join("data/game_graphs_174.json.gz")).unwrap(),
            b"synthetic replacement graphs"
        );
        assert_eq!(
            fs::read(output.join("collision/0688.col")).unwrap(),
            b"practice"
        );
        assert_eq!(
            fs::read(output.join("collision/0688.rays.col")).unwrap(),
            b"practice rays"
        );
        assert!(!output.join("collision/ABCD.col").exists());
        let manifest: serde_json::Value =
            serde_json::from_slice(&fs::read(output.join("manifest.json")).unwrap()).unwrap();
        assert_eq!(manifest["schema"], MANIFEST_SCHEMA);
        let entries = manifest["sha256"].as_object().unwrap();
        assert_eq!(
            entries.len(),
            LOBBY_RUNTIME_DATA.len() + MATCH_RUNTIME_DATA.len() + 3
        );
        for (name, hash) in entries {
            let copied = fs::read(output.join(name)).unwrap();
            assert_eq!(hash, &format!("{:x}", Sha256::digest(copied)));
        }
    }

    #[test]
    fn no_map_argument_copies_all_valid_caches() {
        let fixture = Fixture::new();
        let source = fixture.source();
        let output = prepare(&source, &fixture.root.join("pack"), &[], None).unwrap();
        assert!(output.join("collision/ABCD.col").is_file());
        assert!(!output.join("collision/not-a-map.col").exists());
    }

    #[test]
    fn rejects_existing_nested_invalid_and_missing_inputs_before_writing() {
        let fixture = Fixture::new();
        let source = fixture.source();
        let nested = source.join("new/pack");
        assert!(
            prepare(&source, &nested, &[], None)
                .unwrap_err()
                .to_string()
                .contains("outside")
        );
        assert!(!nested.exists());

        let existing = fixture.root.join("existing");
        fixture.write("existing/keep", b"existing contents");
        assert!(
            prepare(&source, &existing, &[], None)
                .unwrap_err()
                .to_string()
                .contains("new destination")
        );
        assert_eq!(
            fs::read(existing.join("keep")).unwrap(),
            b"existing contents"
        );

        let missing = fixture.root.join("missing");
        assert!(
            prepare(&source, &missing, &["../1".into()], None)
                .unwrap_err()
                .to_string()
                .contains("hexadecimal")
        );
        assert!(
            prepare(&source, &missing, &["CDEF".into()], None)
                .unwrap_err()
                .to_string()
                .contains("collision/CDEF.col")
        );
        assert!(!missing.exists());
    }

    #[test]
    fn unfinished_output_is_removed_without_touching_its_parent() {
        let fixture = Fixture::new();
        fixture.write("keep", b"existing contents");
        let path = fixture.root.join("pack");
        {
            let mut output = OutputDirectory::create(&path).unwrap();
            output.mkdir("data").unwrap();
            let (_path, mut file) = output.file("data/partial").unwrap();
            file.write_all(b"partial copy").unwrap();
            assert!(output.file("data/partial").is_err());
        }
        assert!(!path.exists());
        assert_eq!(
            fs::read(fixture.root.join("keep")).unwrap(),
            b"existing contents"
        );
    }
}
