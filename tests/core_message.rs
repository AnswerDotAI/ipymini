use ipyrust::core::message::{build_reply, parse_message};
use serde_json::json;

#[test]
fn message_roundtrip_with_signature() {
    let identities = vec![b"identity".to_vec()];
    let parent = json!({"msg_id": "parent"});
    let content = json!({"key": "value"});
    let frames = build_reply(
        &identities,
        "execute_request",
        &content,
        &parent,
        "session",
        "user",
        "secret",
        "hmac-sha256",
    )
    .expect("build reply");

    let msg = parse_message(frames, "secret", "hmac-sha256").expect("parse");
    assert_eq!(msg.identities, identities);
    assert_eq!(msg.content, content);
    assert_eq!(msg.parent_header, parent);
}

#[test]
fn message_signature_mismatch() {
    let identities = vec![b"identity".to_vec()];
    let parent = json!({"msg_id": "parent"});
    let content = json!({"key": "value"});
    let mut frames = build_reply(
        &identities,
        "execute_request",
        &content,
        &parent,
        "session",
        "user",
        "secret",
        "hmac-sha256",
    )
    .expect("build reply");

    // Corrupt the content so the signature no longer matches.
    *frames.last_mut().unwrap() = b"{\"key\":\"other\"}".to_vec();
    let result = parse_message(frames, "secret", "hmac-sha256");
    assert!(result.is_err());
}
