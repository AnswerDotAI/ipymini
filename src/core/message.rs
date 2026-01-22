use anyhow::{anyhow, Result};
use chrono::{SecondsFormat, Utc};
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::Sha256;
use uuid::Uuid;

const IDS_MSG: &[u8] = b"<IDS|MSG>";

#[derive(Debug, Deserialize)]
pub struct ConnectionInfo {
    pub transport: String,
    pub ip: String,
    pub shell_port: u16,
    pub iopub_port: u16,
    pub stdin_port: u16,
    pub control_port: u16,
    pub hb_port: u16,
    pub key: String,
    pub signature_scheme: String,
}

#[derive(Debug, Serialize)]
struct Header<'a> {
    msg_id: String,
    username: &'a str,
    session: &'a str,
    date: String,
    msg_type: &'a str,
    version: &'a str,
}

#[derive(Debug)]
pub struct JupyterMessage {
    pub identities: Vec<Vec<u8>>,
    pub header: Value,
    pub parent_header: Value,
    pub metadata: Value,
    pub content: Value,
    pub buffers: Vec<Vec<u8>>,
}

pub fn parse_message(frames: Vec<Vec<u8>>, key: &str, scheme: &str) -> Result<JupyterMessage> {
    let split = frames
        .iter()
        .position(|frame| frame.as_slice() == IDS_MSG)
        .ok_or_else(|| anyhow!("missing IDS delimiter"))?;

    if frames.len() < split + 6 {
        return Err(anyhow!("message missing required frames"));
    }

    let identities = frames[..split].to_vec();
    let signature = &frames[split + 1];
    let header = &frames[split + 2];
    let parent_header = &frames[split + 3];
    let metadata = &frames[split + 4];
    let content = &frames[split + 5];
    let buffers = frames[split + 6..].to_vec();

    if !key.is_empty() {
        let expected = sign(key, scheme, header, parent_header, metadata, content)?;
        let sig_str = std::str::from_utf8(signature).unwrap_or_default();
        if expected != sig_str {
            return Err(anyhow!("invalid signature"));
        }
    }

    Ok(JupyterMessage {
        identities,
        header: serde_json::from_slice(header)?,
        parent_header: serde_json::from_slice(parent_header)?,
        metadata: serde_json::from_slice(metadata)?,
        content: serde_json::from_slice(content)?,
        buffers,
    })
}

pub fn build_reply(
    identities: &[Vec<u8>],
    msg_type: &str,
    content: &Value,
    parent_header: &Value,
    session: &str,
    username: &str,
    key: &str,
    scheme: &str,
) -> Result<Vec<Vec<u8>>> {
    let header = Header {
        msg_id: Uuid::new_v4().to_string(),
        username,
        session,
        date: Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true),
        msg_type,
        version: "5.3",
    };

    let header_bytes = serde_json::to_vec(&header)?;
    let parent_bytes = serde_json::to_vec(parent_header)?;
    let metadata_bytes = serde_json::to_vec(&serde_json::json!({}))?;
    let content_bytes = serde_json::to_vec(content)?;

    let signature = if key.is_empty() {
        String::new()
    } else {
        sign(key, scheme, &header_bytes, &parent_bytes, &metadata_bytes, &content_bytes)?
    };

    let mut frames = Vec::with_capacity(identities.len() + 6);
    frames.extend(identities.iter().cloned());
    frames.push(IDS_MSG.to_vec());
    frames.push(signature.into_bytes());
    frames.push(header_bytes);
    frames.push(parent_bytes);
    frames.push(metadata_bytes);
    frames.push(content_bytes);
    Ok(frames)
}

fn sign(key: &str, scheme: &str, header: &[u8], parent: &[u8], metadata: &[u8], content: &[u8]) -> Result<String> {
    let scheme = scheme.trim();
    if scheme.is_empty() || scheme == "none" {
        return Ok(String::new());
    }
    if scheme != "hmac-sha256" {
        return Err(anyhow!("unsupported signature scheme: {scheme}"));
    }
    let mut mac = Hmac::<Sha256>::new_from_slice(key.as_bytes())
        .map_err(|_| anyhow!("invalid HMAC key"))?;
    mac.update(header);
    mac.update(parent);
    mac.update(metadata);
    mac.update(content);
    let result = mac.finalize();
    Ok(hex::encode(result.into_bytes()))
}

pub fn header_field<'a>(header: &'a Value, name: &str) -> Option<&'a str> {
    header.get(name).and_then(|v| v.as_str())
}

pub fn header_msg_type(header: &Value) -> Option<&str> {
    header_field(header, "msg_type")
}
