//! tidemark: predicts tide heights for a coastal station and plots them.
use clap::{Parser, Subcommand};
use std::path::PathBuf;

mod harmonics;
mod model;
mod plotter;

/// Tide predictions for one station.
#[derive(Parser)]
#[command(name = "tidemark", version)]
struct Cli {
    /// Station identifier (a harbour code)
    #[arg(long, env = "TIDEMARK_STATION")]
    station: String,

    /// Directory holding the station's constituent tables
    #[arg(long, env = "TIDEMARK_DATA_DIR")]
    data_dir: Option<PathBuf>,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Print predicted heights for the next hours
    Predict {
        #[arg(long, default_value_t = 24)]
        hours: u32,
    },
    /// Draw a chart of the predictions with the external plotting helper
    Plot {
        #[arg(long)]
        out: PathBuf,
    },
}

fn main() {
    let cli = Cli::parse();
    let table = harmonics::Table::load(&cli.station, cli.data_dir.as_deref());
    match cli.command {
        Commands::Predict { hours } => {
            for h in model::predict(&table, hours) {
                println!("{h:.2}");
            }
        }
        Commands::Plot { out } => {
            let heights = model::predict(&table, 48);
            if let Err(e) = plotter::plot(&heights, &out) {
                eprintln!("plot failed: {e}");
                std::process::exit(1);
            }
        }
    }
}
