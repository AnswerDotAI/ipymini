use crossbeam_channel::Receiver;
use serde_json::Value;

use crate::core::types::{InputRequest, InterpreterResult, KernelInfo};

pub trait Interpreter: Send {
    fn kernel_info(&self) -> KernelInfo;
    fn interrupt(&self);
    fn shutdown(&self);

    fn execute(
        &self,
        code: String,
        silent: bool,
        store_history: bool,
        user_expressions: Value,
        allow_stdin: bool,
    ) -> Receiver<InterpreterResult>;

    fn complete(&self, code: String, cursor_pos: Option<usize>) -> Receiver<InterpreterResult>;
    fn inspect(
        &self,
        code: String,
        cursor_pos: Option<usize>,
        detail_level: u8,
    ) -> Receiver<InterpreterResult>;
    fn is_complete(&self, code: String) -> Receiver<InterpreterResult>;
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
    ) -> Receiver<InterpreterResult>;
    fn comm_open(
        &self,
        comm_id: String,
        target_name: String,
        data: Value,
        metadata: Value,
    ) -> Receiver<InterpreterResult>;
    fn comm_msg(&self, comm_id: String, data: Value, metadata: Value)
        -> Receiver<InterpreterResult>;
    fn comm_close(
        &self,
        comm_id: String,
        data: Value,
        metadata: Value,
    ) -> Receiver<InterpreterResult>;
    fn comm_info(&self) -> Receiver<InterpreterResult>;
    fn debug_request(&self, request: Value) -> Receiver<InterpreterResult>;

    fn input_requests(&self) -> Receiver<InputRequest>;
}
