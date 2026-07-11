# Running an AI agent on your GPU instance

This guide assumes nothing. By the end you will have opened a terminal into
a cloud GPU from the Manifold dashboard, run an AI coding agent (Claude
Code) directly on that machine, and understood what it can and cannot touch.

## 1. What you are looking at

When you launch an instance from the Instances page, Manifold provisions it
automatically (via cloud-init, before you ever touch it):

- **Docker + NVIDIA Container Toolkit** — job templates run in containers
- **The Manifold sidecar** — reports GPU telemetry and file activity to the
  dashboard; listens only on the instance's loopback, never the internet
- **Claude Code CLI** — an AI agent you can run on the box itself
- **Two kinds of disk:**
  - `/workspace/ephemeral` — scratch space. Dies with the instance.
  - `/lambda/nfs/<filesystem-name>` — your persistent filesystem. Survives
    termination; this is where models, datasets, and outputs belong.

## 2. Open a terminal

On a running instance's card, wait for the SSH badge to show **connected**,
then click **Open Terminal**. That is a real shell on the GPU machine,
running over the same managed SSH connection the backend uses for
everything else. (Nothing web-facing was installed on the instance — if you
close the dashboard, there is no terminal server left running anywhere.)

Try it:

```bash
nvidia-smi                       # see your GPU, live
ls /lambda/nfs/manifold-data     # your persistent files
nvcc --version                   # CUDA compiler, preinstalled on Lambda images
```

Terminal activity counts as "not idle" — an instance with an active shell
will not be auto-terminated under you.

## 3. Run Claude Code on the box

In the terminal:

```bash
claude
```

The first time, it will ask you to log in interactively (Manifold never
injects credentials into instances — this is deliberate). After that, you
have an AI agent whose hands are on the GPU machine itself. Things you can
ask it to do:

- "Compile and run a CUDA kernel that benchmarks memory bandwidth"
- "Watch nvidia-smi while my training job runs and tell me if utilization drops"
- "Organize /lambda/nfs/manifold-data/outputs by date and dedupe checkpoints"
- "Write and run a script that converts every .wav in datasets/ to 16 kHz"

The agent sees what the machine sees: the GPU, the CUDA toolkit, Docker,
your persistent filesystem, and the ephemeral scratch space. It cannot see
your laptop's files, and it cannot spend your money — launching and
terminating instances happens only through the Manifold backend's guarded
API, which the on-box agent has no credentials for.

### Watching what it does

While the agent (or any job) works, the instance card gives you:

- **Telemetry** — live GPU utilization / VRAM / temperature sparklines
- **Files** — a newest-first list of files recently created or modified on
  both volumes, so you can watch outputs appear as they are produced
- The **Storage page** — browse and clean up the persistent filesystem,
  even after the instance is gone

### Other agents (Gemini CLI, Codex, ...)

Any CLI agent installs the same way — open the terminal and follow that
tool's install instructions (e.g. `npm install -g @google/gemini-cli`).
Claude Code is preinstalled because provisioning it is scriptable without
credentials; the pattern works for whatever agent you prefer.

## 4. Training workflows (TAO, axolotl, or your own)

For repeatable work, prefer job templates over typing in the terminal: the
Jobs page renders a form for any template in `templates/`, runs it in a
container with the GPU attached, streams logs, and records outputs.

- **tao-train** — NVIDIA TAO Toolkit training driven by a spec file you put
  on the persistent filesystem under `specs/`
- **axolotl-finetune** — LoRA fine-tune from an axolotl config
- **whisper-batch / vllm-serve** — transcription and model serving
- Adding your own is one YAML file; the dashboard needs zero changes.

The terminal and templates compose: use a template for the long training
run, and the terminal (with or without an agent) to inspect results, debug,
and iterate.

## 5. Note on tailscale mode

If you launch with connection mode **tailscale** (requires
`TAILSCALE_AUTHKEY` in `.env`), the instance joins your tailnet with SSH
enabled. Then OTHER machines of yours — a laptop elsewhere, a second
workstation, an agent running on either — can SSH to the instance directly
(`ssh ubuntu@manifold-<launch-id>`) without going through this dashboard.
Everything else behaves identically; the dashboard terminal still works.

## 6. Security posture, in one paragraph

The instance's only public listener is sshd. The dashboard terminal is
xterm.js in your browser talking to the local backend, which owns an SSH
connection to the instance — there is no web terminal service (ttyd, gotty,
etc.) on the box. The sidecar binds to 127.0.0.1 and is reachable only
through an SSH port forward. Shell access therefore exists exactly two
ways: through the backend's managed connection, or through your own SSH
client with your own key (plus tailnet SSH if you enabled tailscale mode).
