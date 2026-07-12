// Manifold desktop shell (Tauri v2).
//
// Responsibilities - deliberately tiny, per the thin-client rule:
// 1. Spawn the PyInstaller'd backend (bundle.externalBin sidecar). ALL
//    logic lives there; this window is just chrome around localhost.
// 2. Show the splash until the backend's port answers, then navigate the
//    window to it.
// 3. Kill the backend when the window closes, so nothing keeps running
//    (and billing awareness stays honest) after the app quits.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::TcpStream;
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, RunEvent};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

const PORT: u16 = 8000;

struct Backend(Mutex<Option<CommandChild>>);

fn port_open(port: u16) -> bool {
    TcpStream::connect_timeout(
        &format!("127.0.0.1:{port}").parse().unwrap(),
        Duration::from_millis(400),
    )
    .is_ok()
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            // If the port is already serving (a dev backend, or a second
            // app launch), reuse it instead of spawning a duplicate that
            // would fail to bind anyway.
            if !port_open(PORT) {
                // MANIFOLD_PARENT_WATCHDOG: the backend watches its stdin
                // pipe and self-terminates on EOF - i.e. whenever this shell
                // exits, however it exits. Killing the CommandChild alone is
                // not enough: PyInstaller --onefile is bootloader -> real
                // process, and only the bootloader dies from kill().
                let (_rx, child) = app
                    .shell()
                    .sidecar("manifold-backend")
                    .expect("sidecar binary missing from bundle")
                    .env("MANIFOLD_PORT", PORT.to_string())
                    .env("MANIFOLD_PARENT_WATCHDOG", "1")
                    .spawn()
                    .expect("failed to spawn manifold-backend");
                app.manage(Backend(Mutex::new(Some(child))));
            }

            let handle = app.handle().clone();
            std::thread::spawn(move || {
                // Wait up to 60s for the backend to answer, then leave the
                // splash for the real dashboard.
                for _ in 0..150 {
                    if port_open(PORT) {
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(400));
                }
                if let Some(window) = handle.get_webview_window("main") {
                    let url = format!("http://127.0.0.1:{PORT}").parse().unwrap();
                    let _ = window.navigate(url);
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building manifold")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                // Take the child out and kill it; instances on Lambda keep
                // running (that is backend policy), but the local process
                // must not outlive its window.
                if let Some(backend) = app.try_state::<Backend>() {
                    if let Some(child) = backend.0.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
        });
}
