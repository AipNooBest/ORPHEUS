use orpheus::runtime;
use std::ffi::OsString;
use std::path::PathBuf;
use std::process::ExitCode;

const USAGE: &str = "Usage: prepare_server_runtime --source DIR --destination DIR [--map INDEX]... [--game-graphs FILE]\n\
    \nCopy runtime inputs from a prepared local checkout into a new external directory.";

struct Arguments {
    source: PathBuf,
    destination: PathBuf,
    maps: Vec<String>,
    game_graphs: Option<PathBuf>,
}

fn parse(mut args: impl Iterator<Item = OsString>) -> Result<Option<Arguments>, String> {
    let (mut source, mut destination, mut game_graphs) = (None, None, None);
    let mut maps = Vec::new();
    while let Some(argument) = args.next() {
        let argument = argument.to_str().ok_or("Expected a command-line option")?;
        let (option, inline) = match argument.split_once('=') {
            Some((name, value)) => (name, Some(value)),
            None => (argument, None),
        };
        if matches!(option, "--help" | "-h") {
            return Ok(None);
        }
        let mut value = || {
            inline
                .map(OsString::from)
                .or_else(|| args.next())
                .ok_or_else(|| format!("{option} needs a value"))
        };
        match option {
            "--source" => source = Some(PathBuf::from(value()?)),
            "--destination" => destination = Some(PathBuf::from(value()?)),
            "--game-graphs" => game_graphs = Some(PathBuf::from(value()?)),
            "--map" => maps.push(value()?.into_string().map_err(|_| "Invalid map index")?),
            _ => return Err(format!("Unknown option {option}")),
        }
    }
    Ok(Some(Arguments {
        source: source.ok_or("--source is required")?,
        destination: destination.ok_or("--destination is required")?,
        maps,
        game_graphs,
    }))
}

fn run() -> Result<(), String> {
    let Some(args) = parse(std::env::args_os().skip(1))? else {
        println!("{USAGE}");
        return Ok(());
    };
    let destination = runtime::prepare(
        &args.source,
        &args.destination,
        &args.maps,
        args.game_graphs.as_deref(),
    )
    .map_err(|error| error.to_string())?;
    println!("Prepared runtime data at {}", destination.display());
    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}\n{USAGE}");
            ExitCode::from(2)
        }
    }
}
