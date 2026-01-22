use anyhow::{anyhow, Result};
use crossbeam_channel::{Receiver, Sender};
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use pyo3::wrap_pyfunction;
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::HashSet;
use std::env;
use std::path::Path;
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;
use std::thread;
use std::time::Duration;

use crate::core::interpreter::Interpreter;
use crate::core::types::{InputRequest, InterpreterResult, KernelInfo};

#[derive(Debug)]
pub enum PythonTask {
    Execute {
        code: String,
        silent: bool,
        store_history: bool,
        user_expressions: Value,
        allow_stdin: bool,
        reply: Sender<InterpreterResult>,
    },
    Complete {
        code: String,
        cursor_pos: Option<usize>,
        reply: Sender<InterpreterResult>,
    },
    Inspect {
        code: String,
        cursor_pos: Option<usize>,
        detail_level: u8,
        reply: Sender<InterpreterResult>,
    },
    IsComplete {
        code: String,
        reply: Sender<InterpreterResult>,
    },
    History {
        hist_access_type: String,
        output: bool,
        raw: bool,
        session: i64,
        start: i64,
        stop: Option<i64>,
        n: Option<i64>,
        pattern: Option<String>,
        unique: bool,
        reply: Sender<InterpreterResult>,
    },
    CommOpen {
        comm_id: String,
        target_name: String,
        data: Value,
        metadata: Value,
        reply: Sender<InterpreterResult>,
    },
    CommMsg {
        comm_id: String,
        data: Value,
        metadata: Value,
        reply: Sender<InterpreterResult>,
    },
    CommClose {
        comm_id: String,
        data: Value,
        metadata: Value,
        reply: Sender<InterpreterResult>,
    },
    CommInfo {
        reply: Sender<InterpreterResult>,
    },
    Debug {
        request: Value,
        reply: Sender<InterpreterResult>,
    },
}

#[derive(Debug)]
pub struct PythonInterpreter {
    tx: Sender<PythonTask>,
    input_rx: Receiver<InputRequest>,
    supports_debugger: bool,
}

static INPUT_SENDER: OnceLock<Sender<InputRequest>> = OnceLock::new();
static INPUT_INTERRUPT: AtomicBool = AtomicBool::new(false);

#[pyfunction]
fn request_input(py: Python, prompt: String, password: bool) -> PyResult<String> {
    let sender = INPUT_SENDER
        .get()
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("input bridge not initialized"))?;
    let (tx, rx) = crossbeam_channel::bounded(1);
    sender
        .send(InputRequest {
            prompt,
            password,
            reply: tx,
        })
        .map_err(|err| pyo3::exceptions::PyRuntimeError::new_err(format!("input request failed: {err}")))?;

    let result = py.allow_threads(|| rx.recv());
    match result {
        Ok(value) => {
            if INPUT_INTERRUPT.swap(false, Ordering::SeqCst) {
                Err(pyo3::exceptions::PyKeyboardInterrupt::new_err("Interrupted by user"))
            } else {
                Ok(value)
            }
        }
        Err(err) => Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
            "input request aborted: {err}"
        ))),
    }
}

impl PythonInterpreter {
    pub fn new() -> Result<Self> {
        let (tx, rx) = crossbeam_channel::unbounded::<PythonTask>();
        let (input_tx, input_rx) = crossbeam_channel::unbounded::<InputRequest>();
        let _ = INPUT_SENDER.set(input_tx);
        let (debug_tx, debug_rx) = crossbeam_channel::bounded(1);
        thread::spawn(move || worker_loop(rx, debug_tx));
        let supports_debugger = debug_rx.recv_timeout(Duration::from_secs(5)).unwrap_or(false);
        Ok(Self {
            tx,
            input_rx,
            supports_debugger,
        })
    }

    fn send_or_error(&self, task: PythonTask, fallback: Sender<InterpreterResult>) {
        if self.tx.send(task).is_err() {
            let _ = fallback.send(InterpreterResult::Error("python worker dropped".to_string()));
        }
    }

    fn request(
        &self,
        build: impl FnOnce(Sender<InterpreterResult>) -> PythonTask,
    ) -> Receiver<InterpreterResult> {
        let (tx, rx) = crossbeam_channel::bounded(1);
        let task = build(tx.clone());
        self.send_or_error(task, tx);
        rx
    }
}

#[derive(Debug, Deserialize)]
struct PythonEnvInfo {
    stdlib: Option<String>,
    platstdlib: Option<String>,
    purelib: Option<String>,
    platlib: Option<String>,
    prefix: Option<String>,
    base_prefix: Option<String>,
    exec_prefix: Option<String>,
    libdir: Option<String>,
    sitepackages: Option<Vec<String>>,
    usersite: Option<String>,
}

const PY_ENV_SCRIPT: &str = include_str!("../scripts/python_env.py");

pub fn ensure_python_env() -> Result<()> {
    let needs_home = env::var_os("PYTHONHOME").is_none();
    let needs_path = env::var_os("PYTHONPATH").is_none();
    let needs_libdir = if cfg!(target_os = "macos") {
        env::var_os("DYLD_LIBRARY_PATH").is_none()
    } else if cfg!(target_os = "linux") {
        env::var_os("LD_LIBRARY_PATH").is_none()
    } else {
        false
    };

    if !needs_home && !needs_path && !needs_libdir {
        return Ok(());
    }

    let info = python_env_info().ok_or_else(|| anyhow!("unable to probe python environment"))?;

    if needs_home {
        if let Some(home) = best_python_home(&info) {
            env::set_var("PYTHONHOME", home);
        }
    }

    if needs_path {
        if let Some(path) = build_pythonpath(&info) {
            env::set_var("PYTHONPATH", path);
        }
    }

    if needs_libdir {
        if let Some(libdir) = info.libdir {
            if cfg!(target_os = "macos") {
                env::set_var("DYLD_LIBRARY_PATH", libdir);
            } else if cfg!(target_os = "linux") {
                env::set_var("LD_LIBRARY_PATH", libdir);
            }
        }
    }

    Ok(())
}

fn python_env_info() -> Option<PythonEnvInfo> {
    for exe in ["python3", "python"] {
        if let Ok(output) = Command::new(exe).args(["-c", PY_ENV_SCRIPT, "--json"]).output() {
            if output.status.success() {
                let stdout = String::from_utf8_lossy(&output.stdout);
                let trimmed = stdout.trim();
                if trimmed.is_empty() {
                    continue;
                }
                if let Ok(info) = serde_json::from_str::<PythonEnvInfo>(trimmed) {
                    return Some(info);
                }
            }
        }
    }
    None
}

fn best_python_home(info: &PythonEnvInfo) -> Option<String> {
    if let Some(base) = info.base_prefix.as_ref().or(info.prefix.as_ref()) {
        if !base.is_empty() {
            return Some(base.clone());
        }
    }
    if let Some(stdlib) = info.stdlib.as_ref() {
        let path = Path::new(stdlib);
        if let Some(grandparent) = path.parent().and_then(|p| p.parent()) {
            return Some(grandparent.to_string_lossy().to_string());
        }
    }
    info.exec_prefix.clone()
}

fn build_pythonpath(info: &PythonEnvInfo) -> Option<String> {
    let mut seen = HashSet::new();
    let mut paths = Vec::new();

    let mut add = |value: &Option<String>| {
        if let Some(val) = value {
            if !val.is_empty() && seen.insert(val.clone()) {
                paths.push(val.clone());
            }
        }
    };

    add(&info.purelib);
    add(&info.platlib);
    add(&info.platstdlib);
    add(&info.stdlib);

    if let Some(sitepackages) = info.sitepackages.as_ref() {
        for path in sitepackages {
            if !path.is_empty() && seen.insert(path.clone()) {
                paths.push(path.clone());
            }
        }
    }
    if let Some(usersite) = info.usersite.as_ref() {
        if !usersite.is_empty() && seen.insert(usersite.clone()) {
            paths.push(usersite.clone());
        }
    }

    if paths.is_empty() {
        None
    } else {
        Some(env::join_paths(paths).ok()?.to_string_lossy().to_string())
    }
}

fn worker_loop(rx: Receiver<PythonTask>, debug_tx: Sender<bool>) {
    pyo3::prepare_freethreaded_python();
    let bridge = Python::with_gil(|py| -> Result<Py<PyAny>> {
        let code = include_str!("python_bridge.py");
        let module = PyModule::from_code_bound(py, code, "python_bridge.py", "ipyrust_bridge")?;
        let module_ref = module.clone();
        module.add_function(wrap_pyfunction!(request_input, module_ref)?)?;
        let class = module.getattr("RustKernelBridge")?;
        let instance = class.call0()?;
        Ok(instance.into_py(py))
    });

    let bridge = match bridge {
        Ok(bridge) => bridge,
        Err(err) => {
            let _ = debug_tx.send(false);
            for task in rx.iter() {
                let _ = match task {
                    PythonTask::Execute { reply, .. }
                    | PythonTask::Complete { reply, .. }
                    | PythonTask::Inspect { reply, .. }
                    | PythonTask::IsComplete { reply, .. }
                    | PythonTask::History { reply, .. }
                    | PythonTask::CommOpen { reply, .. }
                    | PythonTask::CommMsg { reply, .. }
                    | PythonTask::CommClose { reply, .. }
                    | PythonTask::CommInfo { reply, .. }
                    | PythonTask::Debug { reply, .. } => {
                        reply.send(InterpreterResult::Error(err.to_string()))
                    }
                };
            }
            return;
        }
    };

    let supports_debugger = Python::with_gil(|py| -> bool {
        let instance = bridge.bind(py);
        match instance.call_method0("debug_available") {
            Ok(val) => val.extract::<bool>().unwrap_or(false),
            Err(_) => false,
        }
    });
    let _ = debug_tx.send(supports_debugger);

    fn call_bridge_json<T, A>(
        py: Python<'_>,
        bridge: &Py<PyAny>,
        method: &str,
        args: A,
    ) -> Result<T>
    where
        T: for<'de> Deserialize<'de>,
        A: IntoPy<Py<PyTuple>>,
    {
        let instance = bridge.bind(py);
        let raw = instance.call_method1(method, args)?;
        let raw: String = raw.extract()?;
        Ok(serde_json::from_str(&raw)?)
    }

    for task in rx.iter() {
        match task {
            PythonTask::Execute {
                code,
                silent,
                store_history,
                user_expressions,
                allow_stdin,
                reply,
            } => {
                let result = Python::with_gil(|py| {
                    let user_expressions_json = serde_json::to_string(&user_expressions)?;
                    call_bridge_json(
                        py,
                        &bridge,
                        "execute",
                        (code, silent, store_history, user_expressions_json, allow_stdin),
                    )
                });
                let _ = reply.send(match result {
                    Ok(res) => InterpreterResult::Execute(res),
                    Err(err) => InterpreterResult::Error(err.to_string()),
                });
            }
            PythonTask::Complete {
                code,
                cursor_pos,
                reply,
            } => {
                let result =
                    Python::with_gil(|py| call_bridge_json(py, &bridge, "complete", (code, cursor_pos)));
                let _ = reply.send(match result {
                    Ok(res) => InterpreterResult::Complete(res),
                    Err(err) => InterpreterResult::Error(err.to_string()),
                });
            }
            PythonTask::Inspect {
                code,
                cursor_pos,
                detail_level,
                reply,
            } => {
                let result = Python::with_gil(|py| {
                    call_bridge_json(py, &bridge, "inspect", (code, cursor_pos, detail_level))
                });
                let _ = reply.send(match result {
                    Ok(res) => InterpreterResult::Inspect(res),
                    Err(err) => InterpreterResult::Error(err.to_string()),
                });
            }
            PythonTask::IsComplete { code, reply } => {
                let result =
                    Python::with_gil(|py| call_bridge_json(py, &bridge, "is_complete", (code,)));
                let _ = reply.send(match result {
                    Ok(res) => InterpreterResult::IsComplete(res),
                    Err(err) => InterpreterResult::Error(err.to_string()),
                });
            }
            PythonTask::History {
                hist_access_type,
                output,
                raw,
                session,
                start,
                stop,
                n,
                pattern,
                unique,
                reply,
            } => {
                let result = Python::with_gil(|py| {
                    call_bridge_json(
                        py,
                        &bridge,
                        "history",
                        (
                            hist_access_type,
                            output,
                            raw,
                            session,
                            start,
                            stop,
                            n,
                            pattern,
                            unique,
                        ),
                    )
                });
                let _ = reply.send(match result {
                    Ok(res) => InterpreterResult::History(res),
                    Err(err) => InterpreterResult::Error(err.to_string()),
                });
            }
            PythonTask::CommOpen {
                comm_id,
                target_name,
                data,
                metadata,
                reply,
            } => {
                let result = Python::with_gil(|py| {
                    let data_json = serde_json::to_string(&data)?;
                    let metadata_json = serde_json::to_string(&metadata)?;
                    call_bridge_json(
                        py,
                        &bridge,
                        "comm_open",
                        (comm_id, target_name, data_json, metadata_json),
                    )
                });
                let _ = reply.send(match result {
                    Ok(res) => InterpreterResult::Comm(res),
                    Err(err) => InterpreterResult::Error(err.to_string()),
                });
            }
            PythonTask::CommMsg {
                comm_id,
                data,
                metadata,
                reply,
            } => {
                let result = Python::with_gil(|py| {
                    let data_json = serde_json::to_string(&data)?;
                    let metadata_json = serde_json::to_string(&metadata)?;
                    call_bridge_json(
                        py,
                        &bridge,
                        "comm_msg",
                        (comm_id, data_json, metadata_json),
                    )
                });
                let _ = reply.send(match result {
                    Ok(res) => InterpreterResult::Comm(res),
                    Err(err) => InterpreterResult::Error(err.to_string()),
                });
            }
            PythonTask::CommClose {
                comm_id,
                data,
                metadata,
                reply,
            } => {
                let result = Python::with_gil(|py| {
                    let data_json = serde_json::to_string(&data)?;
                    let metadata_json = serde_json::to_string(&metadata)?;
                    call_bridge_json(
                        py,
                        &bridge,
                        "comm_close",
                        (comm_id, data_json, metadata_json),
                    )
                });
                let _ = reply.send(match result {
                    Ok(res) => InterpreterResult::Comm(res),
                    Err(err) => InterpreterResult::Error(err.to_string()),
                });
            }
            PythonTask::CommInfo { reply } => {
                let result = Python::with_gil(|py| call_bridge_json(py, &bridge, "comm_info", ()));
                let _ = reply.send(match result {
                    Ok(res) => InterpreterResult::Comm(res),
                    Err(err) => InterpreterResult::Error(err.to_string()),
                });
            }
            PythonTask::Debug { request, reply } => {
                let result = Python::with_gil(|py| {
                    let request_json = serde_json::to_string(&request)?;
                    call_bridge_json(py, &bridge, "debug_request", (request_json,))
                });
                let _ = reply.send(match result {
                    Ok(res) => InterpreterResult::Debug(res),
                    Err(err) => InterpreterResult::Error(err.to_string()),
                });
            }
        }
    }
}

impl Interpreter for PythonInterpreter {
    fn kernel_info(&self) -> KernelInfo {
        let mut supported_features = Vec::new();
        if self.supports_debugger {
            supported_features.push("debugger".to_string());
        }
        KernelInfo {
            implementation: "ipyrust".to_string(),
            implementation_version: env!("CARGO_PKG_VERSION").to_string(),
            language_info: json!({
                "name": "python",
                "version": "3",
                "mimetype": "text/x-python",
                "file_extension": ".py",
                "pygments_lexer": "python",
                "codemirror_mode": "ipython",
                "nbconvert_exporter": "python"
            }),
            banner: "IPyRust".to_string(),
            help_links: Vec::new(),
            supported_features,
        }
    }

    fn interrupt(&self) {
        unsafe {
            pyo3::ffi::PyErr_SetInterrupt();
        }
        INPUT_INTERRUPT.store(true, Ordering::SeqCst);
    }

    fn shutdown(&self) {}

    fn execute(
        &self,
        code: String,
        silent: bool,
        store_history: bool,
        user_expressions: Value,
        allow_stdin: bool,
    ) -> Receiver<InterpreterResult> {
        self.request(|reply| PythonTask::Execute {
            code,
            silent,
            store_history,
            user_expressions,
            allow_stdin,
            reply,
        })
    }

    fn complete(&self, code: String, cursor_pos: Option<usize>) -> Receiver<InterpreterResult> {
        self.request(|reply| PythonTask::Complete {
            code,
            cursor_pos,
            reply,
        })
    }

    fn inspect(
        &self,
        code: String,
        cursor_pos: Option<usize>,
        detail_level: u8,
    ) -> Receiver<InterpreterResult> {
        self.request(|reply| PythonTask::Inspect {
            code,
            cursor_pos,
            detail_level,
            reply,
        })
    }

    fn is_complete(&self, code: String) -> Receiver<InterpreterResult> {
        self.request(|reply| PythonTask::IsComplete { code, reply })
    }

    fn history(
        &self,
        hist_access_type: String,
        output: bool,
        raw: bool,
        session: i64,
        start: i64,
        stop: Option<i64>,
        n: Option<i64>,
        pattern: Option<String>,
        unique: bool,
    ) -> Receiver<InterpreterResult> {
        self.request(|reply| PythonTask::History {
            hist_access_type,
            output,
            raw,
            session,
            start,
            stop,
            n,
            pattern,
            unique,
            reply,
        })
    }


    fn comm_open(
        &self,
        comm_id: String,
        target_name: String,
        data: Value,
        metadata: Value,
    ) -> Receiver<InterpreterResult> {
        self.request(|reply| PythonTask::CommOpen {
            comm_id,
            target_name,
            data,
            metadata,
            reply,
        })
    }

    fn comm_msg(
        &self,
        comm_id: String,
        data: Value,
        metadata: Value,
    ) -> Receiver<InterpreterResult> {
        self.request(|reply| PythonTask::CommMsg {
            comm_id,
            data,
            metadata,
            reply,
        })
    }

    fn comm_close(
        &self,
        comm_id: String,
        data: Value,
        metadata: Value,
    ) -> Receiver<InterpreterResult> {
        self.request(|reply| PythonTask::CommClose {
            comm_id,
            data,
            metadata,
            reply,
        })
    }

    fn comm_info(&self) -> Receiver<InterpreterResult> {
        self.request(|reply| PythonTask::CommInfo { reply })
    }

    fn debug_request(&self, request: Value) -> Receiver<InterpreterResult> {
        self.request(|reply| PythonTask::Debug { request, reply })
    }

    fn input_requests(&self) -> Receiver<InputRequest> {
        self.input_rx.clone()
    }
}
