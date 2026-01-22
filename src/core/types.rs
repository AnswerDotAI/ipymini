use crossbeam_channel::Sender;
use serde::Deserialize;
use serde_json::Value;

#[derive(Debug)]
pub enum InterpreterResult {
    Execute(ExecuteResult),
    Complete(Value),
    Inspect(Value),
    IsComplete(Value),
    History(Value),
    Comm(Value),
    Debug(DebugResult),
    Error(String),
}

#[derive(Debug, Deserialize)]
pub struct ExecuteResult {
    #[serde(default)]
    pub stdout: String,
    #[serde(default)]
    pub stderr: String,
    #[serde(default)]
    pub streams: Vec<StreamEvent>,
    #[serde(default)]
    pub display: Vec<DisplayEvent>,
    #[serde(default)]
    pub result: Option<Value>,
    #[serde(default)]
    pub result_metadata: Value,
    #[serde(default)]
    pub execution_count: i64,
    #[serde(default)]
    pub error: Option<ErrorInfo>,
    #[serde(default)]
    pub user_expressions: Value,
    #[serde(default)]
    pub payload: Vec<Value>,
}

#[derive(Debug, Deserialize)]
pub struct DisplayEvent {
    #[serde(rename = "type")]
    pub kind: String,
    #[serde(default)]
    pub data: Option<Value>,
    #[serde(default)]
    pub metadata: Option<Value>,
    #[serde(default)]
    pub transient: Option<Value>,
    #[serde(default)]
    pub update: Option<bool>,
    #[serde(default)]
    pub wait: Option<bool>,
}

#[derive(Debug, Deserialize)]
pub struct StreamEvent {
    pub name: String,
    pub text: String,
}

#[derive(Debug, Deserialize)]
pub struct ErrorInfo {
    pub ename: String,
    pub evalue: String,
    #[serde(default)]
    pub traceback: Vec<String>,
}

#[derive(Debug, Deserialize)]
pub struct DebugResult {
    #[serde(default)]
    pub response: Value,
    #[serde(default)]
    pub events: Vec<Value>,
}

pub struct InputRequest {
    pub prompt: String,
    pub password: bool,
    pub reply: Sender<String>,
}

#[derive(Clone, Debug)]
pub struct KernelInfo {
    pub implementation: String,
    pub implementation_version: String,
    pub language_info: Value,
    pub banner: String,
    pub help_links: Vec<Value>,
    pub supported_features: Vec<String>,
}
