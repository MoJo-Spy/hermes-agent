use std::fs::File;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;

use anyhow::{anyhow, Context, Result};
use sha2::{Digest, Sha256};
use tauri::AppHandle;
use tempfile::TempDir;
use tokio::sync::{mpsc, Mutex};
use zip::ZipArchive;

use crate::bootstrap::{emit_event, spawn_installed_desktop, StartBootstrapArgs};
use crate::events::{BootstrapEvent, LogStream, StageInfo, StageState};
use crate::powershell::{self, StreamSink};

const MAGIC: &[u8; 16] = b"HERMES_OFFLINE_1";
const FOOTER_LEN: u64 = 16 + 8 + 32;

pub struct EmbeddedPayload {
    executable: PathBuf,
    offset: u64,
    length: u64,
    sha256: [u8; 32],
}

#[derive(Debug)]
struct ExtractedPayload {
    _temp: TempDir,
    root: PathBuf,
}

pub fn discover_current_payload() -> Result<Option<EmbeddedPayload>> {
    discover_payload(&std::env::current_exe().context("resolving installer path")?)
}

fn discover_payload(executable: &Path) -> Result<Option<EmbeddedPayload>> {
    let mut file = File::open(executable)
        .with_context(|| format!("opening installer {}", executable.display()))?;
    let file_len = file.metadata()?.len();
    if file_len < FOOTER_LEN {
        return Ok(None);
    }

    file.seek(SeekFrom::End(-(FOOTER_LEN as i64)))?;
    let mut footer = [0_u8; FOOTER_LEN as usize];
    file.read_exact(&mut footer)?;
    if &footer[..16] != MAGIC {
        return Ok(None);
    }

    let length = u64::from_le_bytes(footer[16..24].try_into().expect("fixed footer"));
    if length == 0 || length > file_len - FOOTER_LEN {
        return Err(anyhow!("payload length is outside the installer"));
    }

    let mut sha256 = [0_u8; 32];
    sha256.copy_from_slice(&footer[24..]);
    Ok(Some(EmbeddedPayload {
        executable: executable.to_path_buf(),
        offset: file_len - FOOTER_LEN - length,
        length,
        sha256,
    }))
}

impl EmbeddedPayload {
    fn verify_and_extract(&self) -> Result<ExtractedPayload> {
        let temp = tempfile::tempdir().context("creating offline payload directory")?;
        let zip_path = temp.path().join("payload.zip");
        let root = temp.path().join("payload");
        std::fs::create_dir(&root)?;

        let mut source = File::open(&self.executable)?;
        source.seek(SeekFrom::Start(self.offset))?;
        let mut remaining = self.length;
        let mut digest = Sha256::new();
        let mut output = File::create(&zip_path)?;
        let mut buffer = vec![0_u8; 1024 * 1024];

        while remaining > 0 {
            let chunk_size = remaining.min(buffer.len() as u64) as usize;
            let count = source.read(&mut buffer[..chunk_size])?;
            if count == 0 {
                return Err(anyhow!("offline payload ended unexpectedly"));
            }
            digest.update(&buffer[..count]);
            output.write_all(&buffer[..count])?;
            remaining -= count as u64;
        }
        output.flush()?;

        if digest.finalize().as_slice() != self.sha256 {
            return Err(anyhow!("offline payload SHA-256 mismatch"));
        }

        extract_zip(&zip_path, &root)?;
        for required in [
            "offline-manifest.json",
            "offline_install.py",
            "python/python.exe",
        ] {
            if !root.join(required).is_file() {
                return Err(anyhow!("offline payload is missing {required}"));
            }
        }

        Ok(ExtractedPayload { _temp: temp, root })
    }
}

fn extract_zip(zip_path: &Path, destination: &Path) -> Result<()> {
    let mut archive =
        ZipArchive::new(File::open(zip_path)?).context("opening offline payload ZIP")?;
    for index in 0..archive.len() {
        let mut entry = archive.by_index(index)?;
        let relative = entry
            .enclosed_name()
            .ok_or_else(|| anyhow!("unsafe ZIP path: {}", entry.name()))?
            .to_path_buf();
        let output = destination.join(relative);

        if entry.is_dir() {
            std::fs::create_dir_all(&output)?;
            continue;
        }
        if !entry.is_file() {
            return Err(anyhow!("unsupported ZIP entry: {}", entry.name()));
        }
        if let Some(parent) = output.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let mut file = File::create(&output)?;
        std::io::copy(&mut entry, &mut file)?;
    }
    Ok(())
}

pub async fn run_offline_bootstrap(
    app: AppHandle,
    args: StartBootstrapArgs,
    cancel_rx_holder: Arc<Mutex<Option<mpsc::Receiver<()>>>>,
    payload: EmbeddedPayload,
) -> Result<String> {
    let stages = vec![
        StageInfo {
            name: "verify".into(),
            title: "Verify offline package".into(),
            category: "install".into(),
            needs_user_input: false,
        },
        StageInfo {
            name: "install".into(),
            title: "Install Hermes Desktop".into(),
            category: "install".into(),
            needs_user_input: false,
        },
        StageInfo {
            name: "launch".into(),
            title: "Launch Hermes Desktop".into(),
            category: "install".into(),
            needs_user_input: false,
        },
    ];
    emit_event(
        &app,
        BootstrapEvent::Manifest {
            stages: stages.clone(),
            protocol_version: Some(1),
        },
    );

    if cancellation_signalled(&cancel_rx_holder).await {
        return fail(&app, "verify", "offline installation cancelled");
    }

    let verify_started = Instant::now();
    emit_stage(&app, "verify", StageState::Running, None, None);
    let extracted = match tokio::task::spawn_blocking(move || payload.verify_and_extract()).await {
        Ok(Ok(extracted)) => extracted,
        Ok(Err(err)) => return fail(&app, "verify", &format!("{err:#}")),
        Err(err) => return fail(&app, "verify", &format!("verification task failed: {err}")),
    };
    emit_stage(
        &app,
        "verify",
        StageState::Succeeded,
        Some(verify_started.elapsed().as_millis() as u64),
        None,
    );

    if cancellation_signalled(&cancel_rx_holder).await {
        return fail(&app, "install", "offline installation cancelled");
    }

    let hermes_home = args
        .hermes_home
        .unwrap_or_else(|| crate::paths::hermes_home().to_string_lossy().into_owned());
    let install_root = PathBuf::from(&hermes_home).join("hermes-agent");
    let python = extracted.root.join("python").join("python.exe");
    let installer = extracted.root.join("offline_install.py");
    let command_args = offline_installer_args(&installer, &extracted.root, &hermes_home);

    let app_stdout = app.clone();
    let app_stderr = app.clone();
    let sink = StreamSink {
        on_stdout_line: Box::new(move |line| {
            emit_log(&app_stdout, "install", line, LogStream::Stdout)
        }),
        on_stderr_line: Box::new(move |line| {
            emit_log(&app_stderr, "install", line, LogStream::Stderr)
        }),
    };

    let install_started = Instant::now();
    emit_stage(&app, "install", StageState::Running, None, None);
    // Once the transactional runtime swap begins, do not kill portable Python:
    // it owns rollback and must be allowed to restore the previous runtime.
    let result = match powershell::run_program(
        &python,
        &command_args,
        sink,
        Some(&extracted.root),
        &[("HERMES_HOME", hermes_home.as_str())],
        None,
    )
    .await
    {
        Ok(result) => result,
        Err(err) => {
            return fail(
                &app,
                "install",
                &format!("offline installer failed to start: {err:#}"),
            )
        }
    };

    if result.exit_code != Some(0) {
        return fail(
            &app,
            "install",
            &format!(
                "offline installer exited {:?}: {}",
                result.exit_code,
                result.stderr.trim()
            ),
        );
    }
    emit_stage(
        &app,
        "install",
        StageState::Succeeded,
        Some(install_started.elapsed().as_millis() as u64),
        None,
    );

    let launch_started = Instant::now();
    emit_stage(&app, "launch", StageState::Running, None, None);
    if let Err(err) = spawn_installed_desktop(&install_root) {
        return fail(
            &app,
            "launch",
            &format!("failed to launch installed desktop: {err}"),
        );
    }
    emit_stage(
        &app,
        "launch",
        StageState::Succeeded,
        Some(launch_started.elapsed().as_millis() as u64),
        None,
    );

    emit_event(
        &app,
        BootstrapEvent::Complete {
            install_root: install_root.to_string_lossy().into_owned(),
            marker: Some(serde_json::json!({ "offline": true })),
        },
    );

    tokio::time::sleep(std::time::Duration::from_millis(200)).await;
    app.exit(0);
    Ok(install_root.to_string_lossy().into_owned())
}

fn offline_installer_args(installer: &Path, root: &Path, hermes_home: &str) -> Vec<String> {
    vec![
        "-B".into(),
        installer.to_string_lossy().into_owned(),
        "install-desktop".into(),
        "--bundle".into(),
        root.to_string_lossy().into_owned(),
        "--home".into(),
        hermes_home.into(),
    ]
}

async fn cancellation_signalled(holder: &Arc<Mutex<Option<mpsc::Receiver<()>>>>) -> bool {
    let mut guard = holder.lock().await;
    guard
        .as_mut()
        .is_some_and(|receiver| receiver.try_recv().is_ok())
}

fn emit_stage(
    app: &AppHandle,
    name: &str,
    state: StageState,
    duration_ms: Option<u64>,
    error: Option<String>,
) {
    emit_event(
        app,
        BootstrapEvent::Stage {
            name: name.into(),
            state,
            duration_ms,
            result: None,
            error,
        },
    );
}

fn emit_log(app: &AppHandle, stage: &str, line: &str, stream: LogStream) {
    emit_event(
        app,
        BootstrapEvent::Log {
            stage: Some(stage.into()),
            line: line.into(),
            stream,
        },
    );
}

fn fail<T>(app: &AppHandle, stage: &str, message: &str) -> Result<T> {
    emit_stage(app, stage, StageState::Failed, None, Some(message.into()));
    emit_event(
        app,
        BootstrapEvent::Failed {
            stage: Some(stage.into()),
            error: message.into(),
        },
    );
    Err(anyhow!(message.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use zip::write::SimpleFileOptions;

    fn embedded_exe(entries: &[(&str, &[u8])]) -> tempfile::NamedTempFile {
        let mut zip = std::io::Cursor::new(Vec::new());
        {
            let mut writer = zip::ZipWriter::new(&mut zip);
            for (name, content) in entries {
                writer
                    .start_file(*name, SimpleFileOptions::default())
                    .unwrap();
                writer.write_all(content).unwrap();
            }
            writer.finish().unwrap();
        }
        let payload = zip.into_inner();
        let digest = Sha256::digest(&payload);
        let mut exe = tempfile::NamedTempFile::new().unwrap();
        exe.write_all(b"MZ fake executable").unwrap();
        exe.write_all(&payload).unwrap();
        exe.write_all(MAGIC).unwrap();
        exe.write_all(&(payload.len() as u64).to_le_bytes())
            .unwrap();
        exe.write_all(&digest).unwrap();
        exe.flush().unwrap();
        exe
    }

    #[test]
    fn discovers_verifies_and_extracts_appended_payload() {
        let exe = embedded_exe(&[
            ("offline-manifest.json", b"{}"),
            ("offline_install.py", b"pass"),
            ("python/python.exe", b"python"),
        ]);
        let payload = discover_payload(exe.path()).unwrap().unwrap();
        let extracted = payload.verify_and_extract().unwrap();
        assert_eq!(
            std::fs::read(extracted.root.join("python/python.exe")).unwrap(),
            b"python"
        );
    }

    #[test]
    fn rejects_payload_with_wrong_hash() {
        let mut exe = embedded_exe(&[("offline-manifest.json", b"{}")]);
        exe.as_file_mut().seek(SeekFrom::End(-1)).unwrap();
        exe.as_file_mut().write_all(b"x").unwrap();
        let payload = discover_payload(exe.path()).unwrap().unwrap();
        assert!(payload
            .verify_and_extract()
            .unwrap_err()
            .to_string()
            .contains("SHA-256"));
    }

    #[test]
    fn rejects_parent_traversal_entry() {
        let exe = embedded_exe(&[("../escape", b"bad")]);
        let payload = discover_payload(exe.path()).unwrap().unwrap();
        assert!(payload
            .verify_and_extract()
            .unwrap_err()
            .to_string()
            .contains("unsafe ZIP path"));
    }

    #[test]
    fn offline_installer_disables_bytecode_writes_before_loading_script() {
        let args = offline_installer_args(
            Path::new("offline_install.py"),
            Path::new("payload"),
            "hermes-home",
        );

        assert_eq!(args.first().map(String::as_str), Some("-B"));
    }
}
