use crate::core::interpreter::Interpreter;
use crate::core::message::JupyterMessage;
use crate::core::types::{DebugResult, ExecuteResult, InputRequest, InterpreterResult, KernelInfo};
use anyhow::{anyhow, Result};
use crossbeam_channel::Receiver;
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::time::Duration;

const DEFAULT_INTERPRETER_TIMEOUT: Duration = Duration::from_secs(10);

pub trait Publisher {
    fn send_shell_reply(&mut self, msg: &JupyterMessage, msg_type: &str, content: &Value) -> Result<()>;
    fn send_control_reply(
        &mut self,
        msg: &JupyterMessage,
        msg_type: &str,
        content: &Value,
    ) -> Result<()>;
    fn send_iopub(&mut self, msg: &JupyterMessage, msg_type: &str, content: &Value) -> Result<()>;
    fn send_iopub_status(&mut self, msg: &JupyterMessage, state: &str) -> Result<()>;
    fn send_input_request(
        &mut self,
        msg: &JupyterMessage,
        prompt: &str,
        password: bool,
    ) -> Result<()>;
}

pub struct KernelCore<I: Interpreter> {
    interpreter: I,
    input_rx: Receiver<InputRequest>,
    pending_execute: Option<PendingExecute>,
    execute_queue: VecDeque<PendingExecuteRequest>,
    pending_input: Option<InputRequest>,
    input_queue: VecDeque<InputRequest>,
    execution_count: i64,
    debug_seq: i64,
    shutdown_requested: bool,
}

struct PendingExecuteRequest {
    msg: JupyterMessage,
    code: String,
    silent: bool,
    store_history: bool,
    user_expressions: Value,
    allow_stdin: bool,
}

struct PendingExecute {
    msg: JupyterMessage,
    silent: bool,
    rx: Receiver<InterpreterResult>,
}

#[derive(Debug, Deserialize)]
struct ExecuteRequest {
    code: String,
    #[serde(default)]
    silent: bool,
    #[serde(default = "default_true")]
    store_history: bool,
    #[serde(default)]
    user_expressions: Value,
    #[serde(default)]
    allow_stdin: bool,
}

fn default_true() -> bool {
    true
}

#[derive(Debug, Deserialize)]
struct CompleteRequest {
    code: String,
    #[serde(default)]
    cursor_pos: Option<usize>,
}

#[derive(Debug, Deserialize)]
struct InspectRequest {
    code: String,
    #[serde(default)]
    cursor_pos: Option<usize>,
    #[serde(default)]
    detail_level: u8,
}

#[derive(Debug, Deserialize)]
struct IsCompleteRequest {
    code: String,
}

#[derive(Debug, Deserialize)]
struct ShutdownRequest {
    #[serde(default)]
    restart: bool,
}

#[derive(Debug, Deserialize)]
struct CommOpenRequest {
    comm_id: String,
    target_name: String,
    #[serde(default)]
    data: Value,
    #[serde(default)]
    metadata: Value,
}

#[derive(Debug, Deserialize)]
struct CommMsgRequest {
    comm_id: String,
    #[serde(default)]
    data: Value,
    #[serde(default)]
    metadata: Value,
}

#[derive(Debug, Deserialize)]
struct CommCloseRequest {
    comm_id: String,
    #[serde(default)]
    data: Value,
    #[serde(default)]
    metadata: Value,
}

#[derive(Debug, Deserialize)]
struct HistoryRequest {
    hist_access_type: String,
    #[serde(default)]
    output: bool,
    #[serde(default)]
    raw: bool,
    #[serde(default)]
    session: i64,
    #[serde(default)]
    start: i64,
    #[serde(default)]
    stop: Option<i64>,
    #[serde(default)]
    n: Option<i64>,
    #[serde(default)]
    pattern: Option<String>,
    #[serde(default)]
    unique: bool,
}

#[derive(Debug, Deserialize)]
struct InputReply {
    #[serde(default)]
    value: String,
}

impl<I: Interpreter> KernelCore<I> {
    pub fn new(interpreter: I) -> Self {
        let input_rx = interpreter.input_requests();
        Self {
            interpreter,
            input_rx,
            pending_execute: None,
            execute_queue: VecDeque::new(),
            pending_input: None,
            input_queue: VecDeque::new(),
            execution_count: 0,
            debug_seq: 1,
            shutdown_requested: false,
        }
    }

    pub fn shutdown_requested(&self) -> bool {
        self.shutdown_requested
    }

    pub fn handle_shell(&mut self, publisher: &mut dyn Publisher, msg_type: &str, msg: JupyterMessage) -> Result<()> {
        match msg_type {
            "kernel_info_request" => self.reply_kernel_info(publisher, &msg),
            "execute_request" => self.queue_execute(publisher, msg),
            "complete_request" => self.reply_complete(publisher, &msg),
            "inspect_request" => self.reply_inspect(publisher, &msg),
            "history_request" => self.reply_history(publisher, &msg),
            "is_complete_request" => self.reply_is_complete(publisher, &msg),
            "comm_info_request" => self.reply_comm_info(publisher, &msg),
            "comm_open" => self.handle_comm_open(publisher, &msg),
            "comm_msg" => self.handle_comm_msg(publisher, &msg),
            "comm_close" => self.handle_comm_close(publisher, &msg),
            "shutdown_request" => self.reply_shutdown(publisher, &msg),
            _ => Ok(()),
        }
    }

    pub fn handle_control(
        &mut self,
        publisher: &mut dyn Publisher,
        msg_type: &str,
        msg: JupyterMessage,
    ) -> Result<()> {
        match msg_type {
            "interrupt_request" => self.reply_interrupt(publisher, &msg),
            "debug_request" => self.reply_debug(publisher, &msg),
            "shutdown_request" => self.reply_shutdown_control(publisher, &msg),
            _ => Ok(()),
        }
    }

    pub fn handle_stdin(
        &mut self,
        publisher: &mut dyn Publisher,
        msg_type: &str,
        msg: JupyterMessage,
    ) -> Result<()> {
        match msg_type {
            "input_reply" => self.handle_input_reply(publisher, &msg),
            _ => Ok(()),
        }
    }

    pub fn tick(&mut self, publisher: &mut dyn Publisher) -> Result<()> {
        self.process_input_requests(publisher)?;
        self.check_execute_result(publisher)
    }

    fn reply_kernel_info(&mut self, publisher: &mut dyn Publisher, msg: &JupyterMessage) -> Result<()> {
        let info: KernelInfo = self.interpreter.kernel_info();
        let mut content = json!({
            "status": "ok",
            "protocol_version": "5.3",
            "implementation": info.implementation,
            "implementation_version": info.implementation_version,
            "language_info": info.language_info,
            "banner": info.banner,
            "help_links": info.help_links,
        });
        if !info.supported_features.is_empty() {
            content["supported_features"] = json!(info.supported_features);
        }
        publisher.send_shell_reply(msg, "kernel_info_reply", &content)?;
        publisher.send_iopub_status(msg, "busy")?;
        publisher.send_iopub_status(msg, "idle")?;
        Ok(())
    }

    fn reply_comm_info(&mut self, publisher: &mut dyn Publisher, msg: &JupyterMessage) -> Result<()> {
        let rx = self.interpreter.comm_info();
        let content = self.recv_interpreter(rx, |res| match res {
            InterpreterResult::Comm(val) => Some(val.clone()),
            _ => None,
        })?;
        publisher.send_shell_reply(msg, "comm_info_reply", &content)
    }

    fn reply_complete(&mut self, publisher: &mut dyn Publisher, msg: &JupyterMessage) -> Result<()> {
        let req: CompleteRequest = serde_json::from_value(msg.content.clone())?;
        let rx = self.interpreter.complete(req.code, req.cursor_pos);
        let content = self.recv_interpreter(rx, |res| match res {
            InterpreterResult::Complete(val) => Some(val.clone()),
            _ => None,
        })?;
        publisher.send_shell_reply(msg, "complete_reply", &content)
    }

    fn reply_inspect(&mut self, publisher: &mut dyn Publisher, msg: &JupyterMessage) -> Result<()> {
        let req: InspectRequest = serde_json::from_value(msg.content.clone())?;
        let rx = self
            .interpreter
            .inspect(req.code, req.cursor_pos, req.detail_level);
        let content = self.recv_interpreter(rx, |res| match res {
            InterpreterResult::Inspect(val) => Some(val.clone()),
            _ => None,
        })?;
        publisher.send_shell_reply(msg, "inspect_reply", &content)
    }

    fn reply_history(&mut self, publisher: &mut dyn Publisher, msg: &JupyterMessage) -> Result<()> {
        let req: HistoryRequest = serde_json::from_value(msg.content.clone())?;
        let rx = self.interpreter.history(
            req.hist_access_type,
            req.output,
            req.raw,
            req.session,
            req.start,
            req.stop,
            req.n,
            req.pattern,
            req.unique,
        );
        let content = self.recv_interpreter(rx, |res| match res {
            InterpreterResult::History(val) => Some(val.clone()),
            _ => None,
        })?;
        publisher.send_shell_reply(msg, "history_reply", &content)
    }

    fn reply_is_complete(&mut self, publisher: &mut dyn Publisher, msg: &JupyterMessage) -> Result<()> {
        let req: IsCompleteRequest = serde_json::from_value(msg.content.clone())?;
        let rx = self.interpreter.is_complete(req.code);
        let content = self.recv_interpreter(rx, |res| match res {
            InterpreterResult::IsComplete(val) => Some(val.clone()),
            _ => None,
        })?;
        publisher.send_shell_reply(msg, "is_complete_reply", &content)
    }

    fn reply_interrupt(&mut self, publisher: &mut dyn Publisher, msg: &JupyterMessage) -> Result<()> {
        self.interpreter.interrupt();
        if self.pending_input.is_some() {
            if let Some(pending) = self.pending_input.take() {
                let _ = pending.reply.send(String::new());
            }
            self.input_queue.clear();
        }
        let content = json!({"status": "ok"});
        publisher.send_control_reply(msg, "interrupt_reply", &content)
    }

    fn reply_shutdown(&mut self, publisher: &mut dyn Publisher, msg: &JupyterMessage) -> Result<()> {
        self.handle_shutdown(publisher, msg, |pubs, message, content| {
            pubs.send_shell_reply(message, "shutdown_reply", content)
        })
    }

    fn reply_shutdown_control(
        &mut self,
        publisher: &mut dyn Publisher,
        msg: &JupyterMessage,
    ) -> Result<()> {
        self.handle_shutdown(publisher, msg, |pubs, message, content| {
            pubs.send_control_reply(message, "shutdown_reply", content)
        })
    }

    fn reply_debug(&mut self, publisher: &mut dyn Publisher, msg: &JupyterMessage) -> Result<()> {
        let rx = self.interpreter.debug_request(msg.content.clone());
        match rx.recv_timeout(Duration::from_secs(30))? {
            InterpreterResult::Debug(DebugResult { response, events }) => {
                publisher.send_control_reply(msg, "debug_reply", &response)?;
                for event in events {
                    publisher.send_iopub(msg, "debug_event", &event)?;
                }
                Ok(())
            }
            InterpreterResult::Error(err) => {
                let reply = json!({
                    "seq": self.debug_seq,
                    "type": "response",
                    "request_seq": msg.content.get("seq").and_then(|v| v.as_i64()).unwrap_or(0),
                    "success": false,
                    "command": msg.content.get("command").and_then(|v| v.as_str()).unwrap_or(""),
                    "message": err
                });
                self.debug_seq += 1;
                publisher.send_control_reply(msg, "debug_reply", &reply)
            }
            other => {
                let reply = json!({
                    "seq": self.debug_seq,
                    "type": "response",
                    "request_seq": msg.content.get("seq").and_then(|v| v.as_i64()).unwrap_or(0),
                    "success": false,
                    "command": msg.content.get("command").and_then(|v| v.as_str()).unwrap_or(""),
                    "message": format!("unexpected reply: {other:?}")
                });
                self.debug_seq += 1;
                publisher.send_control_reply(msg, "debug_reply", &reply)
            }
        }
    }

    fn handle_comm_open(&mut self, publisher: &mut dyn Publisher, msg: &JupyterMessage) -> Result<()> {
        let req: CommOpenRequest = serde_json::from_value(msg.content.clone())?;
        let content = json!({
            "comm_id": req.comm_id,
            "target_name": req.target_name,
            "data": req.data,
            "metadata": req.metadata,
        });
        let rx = self.interpreter.comm_open(
            req.comm_id,
            req.target_name,
            content["data"].clone(),
            content["metadata"].clone(),
        );
        self.publish_comm(publisher, msg, "comm_open", &content, rx)
    }

    fn handle_comm_msg(&mut self, publisher: &mut dyn Publisher, msg: &JupyterMessage) -> Result<()> {
        let req: CommMsgRequest = serde_json::from_value(msg.content.clone())?;
        let content = json!({
            "comm_id": req.comm_id,
            "data": req.data,
            "metadata": req.metadata,
        });
        let rx = self.interpreter.comm_msg(
            req.comm_id,
            content["data"].clone(),
            content["metadata"].clone(),
        );
        self.publish_comm(publisher, msg, "comm_msg", &content, rx)
    }

    fn handle_comm_close(&mut self, publisher: &mut dyn Publisher, msg: &JupyterMessage) -> Result<()> {
        let req: CommCloseRequest = serde_json::from_value(msg.content.clone())?;
        let content = json!({
            "comm_id": req.comm_id,
            "data": req.data,
            "metadata": req.metadata,
        });
        let rx = self.interpreter.comm_close(
            req.comm_id,
            content["data"].clone(),
            content["metadata"].clone(),
        );
        self.publish_comm(publisher, msg, "comm_close", &content, rx)
    }

    fn queue_execute(&mut self, publisher: &mut dyn Publisher, msg: JupyterMessage) -> Result<()> {
        let req: ExecuteRequest = serde_json::from_value(msg.content.clone())?;
        let request = PendingExecuteRequest {
            msg,
            code: req.code,
            silent: req.silent,
            store_history: req.store_history,
            user_expressions: req.user_expressions,
            allow_stdin: req.allow_stdin,
        };
        if self.pending_execute.is_some() {
            self.execute_queue.push_back(request);
        } else {
            self.start_execute(publisher, request)?;
        }
        Ok(())
    }

    fn start_execute(
        &mut self,
        publisher: &mut dyn Publisher,
        req: PendingExecuteRequest,
    ) -> Result<()> {
        let rx = self.interpreter.execute(
            req.code.clone(),
            req.silent,
            req.store_history,
            req.user_expressions.clone(),
            req.allow_stdin,
        );

        publisher.send_iopub_status(&req.msg, "busy")?;
        if !req.silent {
            let exec_count = if req.store_history {
                self.execution_count + 1
            } else {
                self.execution_count
            };
            let content = json!({"code": req.code, "execution_count": exec_count});
            publisher.send_iopub(&req.msg, "execute_input", &content)?;
        }

        self.pending_execute = Some(PendingExecute {
            msg: req.msg,
            silent: req.silent,
            rx,
        });
        Ok(())
    }

    fn check_execute_result(&mut self, publisher: &mut dyn Publisher) -> Result<()> {
        let mut finished = None;
        if let Some(pending) = &self.pending_execute {
            match pending.rx.try_recv() {
                Ok(result) => finished = Some(result),
                Err(crossbeam_channel::TryRecvError::Empty) => {}
                Err(_) => {
                    finished = Some(InterpreterResult::Error("interpreter dropped".to_string()))
                }
            }
        }

        if let Some(result) = finished {
            let pending = self
                .pending_execute
                .take()
                .ok_or_else(|| anyhow!("no pending execute"))?;
            match result {
                InterpreterResult::Execute(res) => self.handle_execute_result(publisher, &pending, res)?,
                InterpreterResult::Error(err) => self.handle_execute_error(publisher, &pending, err)?,
                other => {
                    self.handle_execute_error(publisher, &pending, format!("unexpected reply: {other:?}"))?
                }
            }
            if let Some(next) = self.execute_queue.pop_front() {
                self.start_execute(publisher, next)?;
            }
        }
        Ok(())
    }

    fn handle_execute_result(
        &mut self,
        publisher: &mut dyn Publisher,
        pending: &PendingExecute,
        res: ExecuteResult,
    ) -> Result<()> {
        self.execution_count = res.execution_count;

        if !pending.silent {
            if !res.streams.is_empty() {
                for stream in res.streams {
                    let content = json!({"name": stream.name, "text": stream.text});
                    publisher.send_iopub(&pending.msg, "stream", &content)?;
                }
            } else {
                if !res.stdout.is_empty() {
                    let content = json!({"name": "stdout", "text": res.stdout});
                    publisher.send_iopub(&pending.msg, "stream", &content)?;
                }
                if !res.stderr.is_empty() {
                    let content = json!({"name": "stderr", "text": res.stderr});
                    publisher.send_iopub(&pending.msg, "stream", &content)?;
                }
            }

            for event in res.display {
                match event.kind.as_str() {
                    "display" => {
                        let data = event.data.unwrap_or_else(|| json!({}));
                        let metadata = event.metadata.unwrap_or_else(|| json!({}));
                        let transient = event.transient.unwrap_or_else(|| json!({}));
                        let content = json!({"data": data, "metadata": metadata, "transient": transient});
                        let msg_type = if event.update.unwrap_or(false) {
                            "update_display_data"
                        } else {
                            "display_data"
                        };
                        publisher.send_iopub(&pending.msg, msg_type, &content)?;
                    }
                    "clear_output" => {
                        let content = json!({"wait": event.wait.unwrap_or(false)});
                        publisher.send_iopub(&pending.msg, "clear_output", &content)?;
                    }
                    _ => {}
                }
            }

            if let Some(data) = res.result {
                if !data.as_object().map_or(true, |obj| obj.is_empty()) {
                    let content = json!({
                        "execution_count": res.execution_count,
                        "data": data,
                        "metadata": res.result_metadata
                    });
                    publisher.send_iopub(&pending.msg, "execute_result", &content)?;
                }
            }

            if let Some(err) = res.error.as_ref() {
                let content = json!({
                    "ename": err.ename,
                    "evalue": err.evalue,
                    "traceback": err.traceback
                });
                publisher.send_iopub(&pending.msg, "error", &content)?;
            }
        }

        let reply = if let Some(err) = res.error {
            json!({
                "status": "error",
                "ename": err.ename,
                "evalue": err.evalue,
                "traceback": err.traceback,
                "execution_count": res.execution_count,
                "payload": res.payload,
                "user_expressions": res.user_expressions
            })
        } else {
            json!({
                "status": "ok",
                "execution_count": res.execution_count,
                "payload": res.payload,
                "user_expressions": res.user_expressions
            })
        };

        publisher.send_shell_reply(&pending.msg, "execute_reply", &reply)?;
        publisher.send_iopub_status(&pending.msg, "idle")?;
        Ok(())
    }

    fn handle_execute_error(
        &mut self,
        publisher: &mut dyn Publisher,
        pending: &PendingExecute,
        err: String,
    ) -> Result<()> {
        let content = json!({
            "status": "error",
            "ename": "PythonError",
            "evalue": err,
            "traceback": [],
            "execution_count": self.execution_count,
            "payload": [],
            "user_expressions": {}
        });
        publisher.send_shell_reply(&pending.msg, "execute_reply", &content)?;
        publisher.send_iopub_status(&pending.msg, "idle")?;
        Ok(())
    }

    fn process_input_requests(&mut self, publisher: &mut dyn Publisher) -> Result<()> {
        while let Ok(req) = self.input_rx.try_recv() {
            if self.pending_execute.is_none() {
                let _ = req.reply.send(String::new());
                continue;
            }
            if self.pending_input.is_none() {
                if let Some(parent) = self.pending_execute.as_ref() {
                    publisher.send_input_request(&parent.msg, &req.prompt, req.password)?;
                    self.pending_input = Some(req);
                } else {
                    let _ = req.reply.send(String::new());
                }
            } else {
                self.input_queue.push_back(req);
            }
        }
        Ok(())
    }

    fn handle_input_reply(
        &mut self,
        publisher: &mut dyn Publisher,
        msg: &JupyterMessage,
    ) -> Result<()> {
        let reply: InputReply = serde_json::from_value(msg.content.clone())?;
        if let Some(pending) = self.pending_input.take() {
            let _ = pending.reply.send(reply.value);
        }
        if let Some(next) = self.input_queue.pop_front() {
            if let Some(parent) = self.pending_execute.as_ref() {
                publisher.send_input_request(&parent.msg, &next.prompt, next.password)?;
                self.pending_input = Some(next);
            } else {
                let _ = next.reply.send(String::new());
            }
        }
        Ok(())
    }

    fn recv_interpreter<F>(&self, rx: Receiver<InterpreterResult>, map: F) -> Result<Value>
    where
        F: FnOnce(&InterpreterResult) -> Option<Value>,
    {
        let res = rx.recv_timeout(DEFAULT_INTERPRETER_TIMEOUT)?;
        match &res {
            InterpreterResult::Error(err) => Ok(self.interpreter_error(err)),
            _ => Ok(map(&res).unwrap_or_else(|| {
                self.interpreter_error(format!("unexpected reply: {res:?}"))
            })),
        }
    }

    fn interpreter_error(&self, err: impl std::fmt::Display) -> Value {
        json!({"status": "error", "ename": "PythonError", "evalue": err.to_string(), "traceback": []})
    }

    fn handle_shutdown<F>(
        &mut self,
        publisher: &mut dyn Publisher,
        msg: &JupyterMessage,
        send_reply: F,
    ) -> Result<()>
    where
        F: FnOnce(&mut dyn Publisher, &JupyterMessage, &Value) -> Result<()>,
    {
        let req: ShutdownRequest = serde_json::from_value(msg.content.clone())?;
        let content = json!({"status": "ok", "restart": req.restart});
        send_reply(publisher, msg, &content)?;
        self.shutdown_requested = true;
        self.interpreter.shutdown();
        Ok(())
    }

    fn publish_comm(
        &mut self,
        publisher: &mut dyn Publisher,
        msg: &JupyterMessage,
        msg_type: &str,
        content: &Value,
        rx: Receiver<InterpreterResult>,
    ) -> Result<()>
    {
        publisher.send_iopub(msg, msg_type, content)?;
        let _ = rx.recv_timeout(DEFAULT_INTERPRETER_TIMEOUT);
        Ok(())
    }
}
