use anyhow::{Context, Result};
use clap::Parser;
use ipyrust::core::message::ConnectionInfo;
use ipyrust::core::zmq_kernel::ZmqKernel;
use ipyrust::python::{ensure_python_env, PythonInterpreter};
use std::fs;
use std::path::PathBuf;

#[derive(Parser, Debug)]
#[command(name = "ipyrust", version)]
struct Args {
    #[arg(short = 'f', long = "connection-file")]
    connection_file: PathBuf,
}

fn main() -> Result<()> {
    env_logger::init();
    if let Err(err) = ensure_python_env() {
        log::warn!("python env detection failed: {err}");
    }
    let args = Args::parse();
    let data = fs::read_to_string(&args.connection_file)
        .with_context(|| format!("reading connection file {:?}", args.connection_file))?;
    let conn: ConnectionInfo = serde_json::from_str(&data)
        .with_context(|| "parsing connection file")?;

    let interpreter = PythonInterpreter::new()?;
    let mut kernel = ZmqKernel::new(conn, interpreter)?;
    kernel.run()?;
    Ok(())
}
