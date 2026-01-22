use anyhow::Result;
use serde_json::{json, Value};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread;
use std::time::Duration;

use crate::core::interpreter::Interpreter;
use crate::core::kernel_core::{KernelCore, Publisher};
use crate::core::message::{build_reply, header_field, header_msg_type, parse_message, ConnectionInfo, JupyterMessage};

pub struct ZmqKernel<I: Interpreter> {
    #[allow(dead_code)]
    _ctx: zmq::Context,
    publisher: ZmqPublisher,
    core: KernelCore<I>,
    shutdown: Arc<AtomicBool>,
}

struct ZmqPublisher {
    shell: zmq::Socket,
    control: zmq::Socket,
    iopub: zmq::Socket,
    stdin: zmq::Socket,
    key: String,
    signature_scheme: String,
    session_id: String,
}

impl<I: Interpreter> ZmqKernel<I> {
    pub fn new(conn: ConnectionInfo, interpreter: I) -> Result<Self> {
        let ctx = zmq::Context::new();
        let shell = ctx.socket(zmq::ROUTER)?;
        let control = ctx.socket(zmq::ROUTER)?;
        let iopub = ctx.socket(zmq::XPUB)?;
        let stdin = ctx.socket(zmq::ROUTER)?;

        iopub.set_xpub_verbose(true)?;

        shell.bind(&endpoint(&conn.transport, &conn.ip, conn.shell_port))?;
        control.bind(&endpoint(&conn.transport, &conn.ip, conn.control_port))?;
        iopub.bind(&endpoint(&conn.transport, &conn.ip, conn.iopub_port))?;
        stdin.bind(&endpoint(&conn.transport, &conn.ip, conn.stdin_port))?;

        let shutdown = Arc::new(AtomicBool::new(false));
        start_heartbeat(&ctx, &conn, shutdown.clone())?;

        let publisher = ZmqPublisher {
            shell,
            control,
            iopub,
            stdin,
            key: conn.key,
            signature_scheme: conn.signature_scheme,
            session_id: uuid::Uuid::new_v4().to_string(),
        };

        Ok(Self {
            _ctx: ctx,
            publisher,
            core: KernelCore::new(interpreter),
            shutdown,
        })
    }

    pub fn run(&mut self) -> Result<()> {
        loop {
            if self.shutdown.load(Ordering::SeqCst) || self.core.shutdown_requested() {
                break;
            }

            let (shell_ready, control_ready, iopub_ready, stdin_ready) = {
                let mut items = [
                    self.publisher.shell.as_poll_item(zmq::POLLIN),
                    self.publisher.control.as_poll_item(zmq::POLLIN),
                    self.publisher.iopub.as_poll_item(zmq::POLLIN),
                    self.publisher.stdin.as_poll_item(zmq::POLLIN),
                ];
                zmq::poll(&mut items, 100)?;
                (
                    items[0].is_readable(),
                    items[1].is_readable(),
                    items[2].is_readable(),
                    items[3].is_readable(),
                )
            };

            if iopub_ready {
                let frames = self.publisher.iopub.recv_multipart(0)?;
                self.handle_iopub_subscriptions(frames)?;
            }
            if shell_ready {
                let frames = self.publisher.shell.recv_multipart(0)?;
                self.handle_frames(frames, Channel::Shell)?;
            }
            if control_ready {
                let frames = self.publisher.control.recv_multipart(0)?;
                self.handle_frames(frames, Channel::Control)?;
            }
            if stdin_ready {
                let frames = self.publisher.stdin.recv_multipart(0)?;
                self.handle_frames(frames, Channel::Stdin)?;
            }

            self.core.tick(&mut self.publisher)?;
        }
        Ok(())
    }

    fn handle_frames(&mut self, frames: Vec<Vec<u8>>, channel: Channel) -> Result<()> {
        let msg = parse_message(frames, &self.publisher.key, &self.publisher.signature_scheme)?;
        let msg_type = header_msg_type(&msg.header).unwrap_or("").to_string();
        match channel {
            Channel::Shell => self.core.handle_shell(&mut self.publisher, &msg_type, msg),
            Channel::Control => self.core.handle_control(&mut self.publisher, &msg_type, msg),
            Channel::Stdin => self.core.handle_stdin(&mut self.publisher, &msg_type, msg),
        }
    }

    fn handle_iopub_subscriptions(&mut self, frames: Vec<Vec<u8>>) -> Result<()> {
        for frame in frames {
            if frame.is_empty() {
                continue;
            }
            if frame[0] != 1 {
                continue;
            }
            let topic = if frame.len() > 1 {
                String::from_utf8_lossy(&frame[1..]).to_string()
            } else {
                String::new()
            };
            self.send_iopub_welcome(&topic)?;
        }
        Ok(())
    }

    fn send_iopub_welcome(&self, subscription: &str) -> Result<()> {
        let content = json!({"subscription": subscription});
        let identities = if subscription.is_empty() {
            Vec::new()
        } else {
            vec![subscription.as_bytes().to_vec()]
        };
        let parent = json!({});
        let frames = build_reply(
            &identities,
            "iopub_welcome",
            &content,
            &parent,
            &self.publisher.session_id,
            "ipyrust",
            &self.publisher.key,
            &self.publisher.signature_scheme,
        )?;
        send_multipart(&self.publisher.iopub, frames)
    }
}

impl Publisher for ZmqPublisher {
    fn send_shell_reply(&mut self, msg: &JupyterMessage, msg_type: &str, content: &Value) -> Result<()> {
        self.send_reply(&self.shell, &msg.identities, msg_type, content, &msg.header)
    }

    fn send_control_reply(
        &mut self,
        msg: &JupyterMessage,
        msg_type: &str,
        content: &Value,
    ) -> Result<()> {
        self.send_reply(&self.control, &msg.identities, msg_type, content, &msg.header)
    }

    fn send_iopub(&mut self, msg: &JupyterMessage, msg_type: &str, content: &Value) -> Result<()> {
        let topic = vec![msg_type.as_bytes().to_vec()];
        self.send_reply(&self.iopub, &topic, msg_type, content, &msg.header)
    }

    fn send_iopub_status(&mut self, msg: &JupyterMessage, state: &str) -> Result<()> {
        let content = json!({"execution_state": state});
        self.send_iopub(msg, "status", &content)
    }

    fn send_input_request(
        &mut self,
        msg: &JupyterMessage,
        prompt: &str,
        password: bool,
    ) -> Result<()> {
        let content = json!({"prompt": prompt, "password": password});
        self.send_reply(&self.stdin, &msg.identities, "input_request", &content, &msg.header)
    }
}

impl ZmqPublisher {
    fn send_reply(
        &self,
        socket: &zmq::Socket,
        identities: &Vec<Vec<u8>>,
        msg_type: &str,
        content: &Value,
        parent_header: &Value,
    ) -> Result<()> {
        let session = header_field(parent_header, "session").unwrap_or("");
        let username = header_field(parent_header, "username").unwrap_or("ipyrust");
        let frames = build_reply(
            identities,
            msg_type,
            content,
            parent_header,
            session,
            username,
            &self.key,
            &self.signature_scheme,
        )?;
        send_multipart(socket, frames)
    }
}

#[derive(Clone, Copy)]
enum Channel {
    Shell,
    Control,
    Stdin,
}

fn endpoint(transport: &str, ip: &str, port: u16) -> String {
    format!("{transport}://{ip}:{port}")
}

fn send_multipart(socket: &zmq::Socket, frames: Vec<Vec<u8>>) -> Result<()> {
    for (idx, frame) in frames.iter().enumerate() {
        let flags = if idx + 1 == frames.len() { 0 } else { zmq::SNDMORE };
        socket.send(frame, flags)?;
    }
    Ok(())
}

fn start_heartbeat(ctx: &zmq::Context, conn: &ConnectionInfo, shutdown: Arc<AtomicBool>) -> Result<()> {
    let endpoint = endpoint(&conn.transport, &conn.ip, conn.hb_port);
    let ctx = ctx.clone();
    thread::spawn(move || {
        let hb_socket = match ctx.socket(zmq::REP) {
            Ok(sock) => sock,
            Err(_) => return,
        };
        if hb_socket.bind(&endpoint).is_err() {
            return;
        }
        loop {
            if shutdown.load(Ordering::SeqCst) {
                break;
            }
            match hb_socket.recv_msg(0) {
                Ok(msg) => {
                    let _ = hb_socket.send(msg, 0);
                }
                Err(_) => thread::sleep(Duration::from_millis(10)),
            }
        }
    });
    Ok(())
}
