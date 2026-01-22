use crossbeam_channel::unbounded;
use ipyrust::core::interpreter::Interpreter;
use ipyrust::core::kernel_core::{KernelCore, Publisher};
use ipyrust::core::message::JupyterMessage;
use ipyrust::core::types::{ExecuteResult, InputRequest, InterpreterResult, KernelInfo};
use serde_json::{json, Value};

#[derive(Default)]
struct CapturePublisher {
    messages: Vec<(String, String, Value)>,
}

impl Publisher for CapturePublisher {
    fn send_shell_reply(&mut self, _msg: &JupyterMessage, msg_type: &str, content: &Value) -> anyhow::Result<()> {
        self.messages
            .push(("shell".to_string(), msg_type.to_string(), content.clone()));
        Ok(())
    }

    fn send_control_reply(
        &mut self,
        _msg: &JupyterMessage,
        msg_type: &str,
        content: &Value,
    ) -> anyhow::Result<()> {
        self.messages
            .push(("control".to_string(), msg_type.to_string(), content.clone()));
        Ok(())
    }

    fn send_iopub(&mut self, _msg: &JupyterMessage, msg_type: &str, content: &Value) -> anyhow::Result<()> {
        self.messages
            .push(("iopub".to_string(), msg_type.to_string(), content.clone()));
        Ok(())
    }

    fn send_iopub_status(&mut self, _msg: &JupyterMessage, state: &str) -> anyhow::Result<()> {
        let content = json!({"execution_state": state});
        self.messages
            .push(("iopub".to_string(), "status".to_string(), content));
        Ok(())
    }

    fn send_input_request(
        &mut self,
        _msg: &JupyterMessage,
        prompt: &str,
        password: bool,
    ) -> anyhow::Result<()> {
        let content = json!({"prompt": prompt, "password": password});
        self.messages
            .push(("stdin".to_string(), "input_request".to_string(), content));
        Ok(())
    }
}

struct FakeInterpreter {
    input_rx: crossbeam_channel::Receiver<InputRequest>,
    kernel_info: KernelInfo,
}

impl FakeInterpreter {
    fn new() -> Self {
        let (_tx, rx) = unbounded();
        Self {
            input_rx: rx,
            kernel_info: KernelInfo {
                implementation: "fake".to_string(),
                implementation_version: "0.0.1".to_string(),
                language_info: json!({"name": "fake"}),
                banner: "fake".to_string(),
                help_links: Vec::new(),
                supported_features: vec!["debugger".to_string()],
            },
        }
    }

    fn ok_channel(&self, value: InterpreterResult) -> crossbeam_channel::Receiver<InterpreterResult> {
        let (tx, rx) = unbounded();
        let _ = tx.send(value);
        rx
    }
}

impl Interpreter for FakeInterpreter {
    fn kernel_info(&self) -> KernelInfo {
        self.kernel_info.clone()
    }

    fn interrupt(&self) {}

    fn shutdown(&self) {}

    fn execute(
        &self,
        _code: String,
        _silent: bool,
        _store_history: bool,
        _user_expressions: Value,
        _allow_stdin: bool,
    ) -> crossbeam_channel::Receiver<InterpreterResult> {
        let result = ExecuteResult {
            stdout: String::new(),
            stderr: String::new(),
            streams: vec![],
            display: vec![],
            result: Some(json!({"text/plain": "1"})),
            result_metadata: json!({}),
            execution_count: 1,
            error: None,
            user_expressions: json!({}),
            payload: vec![],
        };
        self.ok_channel(InterpreterResult::Execute(result))
    }

    fn complete(&self, _code: String, _cursor_pos: Option<usize>) -> crossbeam_channel::Receiver<InterpreterResult> {
        self.ok_channel(InterpreterResult::Complete(json!({"status": "ok"})))
    }

    fn inspect(
        &self,
        _code: String,
        _cursor_pos: Option<usize>,
        _detail_level: u8,
    ) -> crossbeam_channel::Receiver<InterpreterResult> {
        self.ok_channel(InterpreterResult::Inspect(json!({"status": "ok"})))
    }

    fn is_complete(&self, _code: String) -> crossbeam_channel::Receiver<InterpreterResult> {
        self.ok_channel(InterpreterResult::IsComplete(json!({"status": "complete"})))
    }

    fn history(
        &self,
        _hist_access_type: String,
        _output: bool,
        _raw: bool,
        _session: i64,
        _start: i64,
        _stop: Option<i64>,
        _n: Option<i64>,
        _pattern: Option<String>,
        _unique: bool,
    ) -> crossbeam_channel::Receiver<InterpreterResult> {
        self.ok_channel(InterpreterResult::History(json!({"status": "ok", "history": []})))
    }

    fn comm_open(
        &self,
        _comm_id: String,
        _target_name: String,
        _data: Value,
        _metadata: Value,
    ) -> crossbeam_channel::Receiver<InterpreterResult> {
        self.ok_channel(InterpreterResult::Comm(json!({"status": "ok"})))
    }

    fn comm_msg(
        &self,
        _comm_id: String,
        _data: Value,
        _metadata: Value,
    ) -> crossbeam_channel::Receiver<InterpreterResult> {
        self.ok_channel(InterpreterResult::Comm(json!({"status": "ok"})))
    }

    fn comm_close(
        &self,
        _comm_id: String,
        _data: Value,
        _metadata: Value,
    ) -> crossbeam_channel::Receiver<InterpreterResult> {
        self.ok_channel(InterpreterResult::Comm(json!({"status": "ok"})))
    }

    fn comm_info(&self) -> crossbeam_channel::Receiver<InterpreterResult> {
        self.ok_channel(InterpreterResult::Comm(json!({"status": "ok", "comms": {}})))
    }

    fn debug_request(&self, _request: Value) -> crossbeam_channel::Receiver<InterpreterResult> {
        self.ok_channel(InterpreterResult::Debug(
            ipyrust::core::types::DebugResult {
                response: json!({"success": true}),
                events: vec![],
            },
        ))
    }

    fn input_requests(&self) -> crossbeam_channel::Receiver<InputRequest> {
        self.input_rx.clone()
    }
}

fn dummy_msg(msg_type: &str, content: Value) -> JupyterMessage {
    JupyterMessage {
        identities: Vec::new(),
        header: json!({"msg_id": "1", "session": "s", "username": "u", "msg_type": msg_type}),
        parent_header: json!({}),
        metadata: json!({}),
        content,
        buffers: Vec::new(),
    }
}

#[test]
fn kernel_info_publishes_status() {
    let interpreter = FakeInterpreter::new();
    let mut core = KernelCore::new(interpreter);
    let mut pubsub = CapturePublisher::default();
    let msg = dummy_msg("kernel_info_request", json!({}));

    core.handle_shell(&mut pubsub, "kernel_info_request", msg).unwrap();

    assert!(pubsub
        .messages
        .iter()
        .any(|(ch, msg_type, _)| ch == "shell" && msg_type == "kernel_info_reply"));
    let status_count = pubsub
        .messages
        .iter()
        .filter(|(ch, msg_type, _)| ch == "iopub" && msg_type == "status")
        .count();
    assert_eq!(status_count, 2);
}

#[test]
fn execute_flow_publishes_reply() {
    let interpreter = FakeInterpreter::new();
    let mut core = KernelCore::new(interpreter);
    let mut pubsub = CapturePublisher::default();

    let msg = dummy_msg(
        "execute_request",
        json!({"code": "1+1", "silent": false, "store_history": true, "user_expressions": {}}),
    );
    core.handle_shell(&mut pubsub, "execute_request", msg).unwrap();
    core.tick(&mut pubsub).unwrap();

    assert!(pubsub
        .messages
        .iter()
        .any(|(ch, msg_type, _)| ch == "shell" && msg_type == "execute_reply"));
    assert!(pubsub
        .messages
        .iter()
        .any(|(ch, msg_type, _)| ch == "iopub" && msg_type == "execute_result"));
}
