// Typed client for the local Manifold backend. The dashboard is a thin
// consumer: no business logic here, just fetch + types + error surfacing.

import { API_BASE } from "./backend";

export class ApiError extends Error {
  status: number;
  body?: Record<string, unknown>;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

// Requests that ride the SSH connection to an instance (sidecar calls,
// file listings) can be slow when the instance is struggling; a timeout
// turns a silent hang into an honest error that names the real culprit.
const DEFAULT_TIMEOUT_MS = 30_000;

async function request<T>(
  path: string,
  init?: RequestInit & { timeoutMs?: number },
): Promise<T> {
  const timeoutMs = init?.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal: ctrl.signal,
      headers: { "content-type": "application/json", ...init?.headers },
    });
  } catch {
    if (ctrl.signal.aborted) {
      // The backend accepted the connection but did not answer in time:
      // usually the instance/sidecar side of the call, not the backend.
      throw new ApiError(
        0,
        `No answer after ${Math.round(timeoutMs / 1000)}s (${path}). ` +
          "The backend is likely up but the instance or its sidecar is " +
          "slow or unreachable.",
      );
    }
    throw new ApiError(0, "Backend unreachable. Is it running on :8000?");
  } finally {
    clearTimeout(timer);
  }
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new ApiError(
      resp.status,
      body.detail ?? `HTTP ${resp.status}`,
    );
    err.body = body;
    throw err;
  }
  return body as T;
}

export type UnpersistedFile = {
  path: string;
  size_bytes: number;
  modified: string;
};

export type InstanceTypeInfo = {
  description: string;
  gpu_description: string;
  price_usd_per_hour: number;
  specs: { vcpus: number; memory_gib: number; storage_gib: number; gpus: number };
  regions_with_capacity: string[];
};

export type Region = { code: string; name: string };

export type ModelPreset = {
  label: string;
  model_id: string;
  vram_gib: number;
  tier: string;
  note: string;
  // Extra vllm-serve parameters the preset needs (e.g. tensor_parallel: 8
  // for models that shard across a whole 8-GPU cluster).
  parameters?: Record<string, unknown>;
};

export type SidecarDiagnosis = {
  cause: string;
  summary: string;
  port: number;
  checks: { label: string; command: string; output: string }[];
};

export type Filesystem = {
  name: string;
  region: string;
  mount_point: string;
  is_in_use: boolean;
  bytes_used: number;
};

export type Instance = {
  id: string;
  name: string;
  status: string;
  ip: string | null;
  region: string;
  instance_type: string;
  gpu_description: string;
  hourly_rate_usd: number;
  filesystems: string[];
  connection_mode: string | null;
  connection_state: string;
  connection_error: string;
  launch_id: string | null;
  idle: {
    idle_seconds: number;
    timeout_seconds: number;
    keep_alive: boolean;
  } | null;
};

export type Launch = {
  id: string;
  created_at: string;
  requested_type: string;
  launched_type: string | null;
  region: string;
  filesystem: string | null;
  connection_mode: string;
  hourly_rate_cents: number | null;
  status: string;
  attempts: number;
  error: string | null;
  lambda_instance_id: string | null;
  launched_at: string | null;
  active_at: string | null;
  terminated_at: string | null;
};

export type StoredFile = {
  key: string;
  size_bytes: number;
  last_modified: string;
};

export type LaunchRequest = {
  instance_type: string;
  region: string;
  filesystem: string;
  connection_mode: string;
  ssh_key_name?: string;
  name?: string;
};

export type TemplateParameter = {
  name: string;
  type: "string" | "integer" | "number" | "boolean";
  description: string;
  default: string | number | boolean | null;
  required: boolean;
};

export type Template = {
  name: string;
  description: string;
  image: string;
  command: string;
  parameters: TemplateParameter[];
  gpu: { min_vram_gib?: number; recommended_types?: string[] };
};

export type Lifecycle =
  | "queued"
  | "waiting"
  | "launching"
  | "ready"
  | "running"
  | "syncing"
  | "terminating"
  | "done"
  | "failed"
  | "cancelled";

export type Task = {
  id: string;
  created_at: string;
  template: string;
  parameters: Record<string, unknown>;
  status: "queued" | "running" | "succeeded" | "failed";
  instance_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  exit_code: number | null;
  error: string | null;
  output_paths: string[];
  // Auto-manage (Phase 24): Manifold owns this job's instance lifecycle.
  auto_manage: boolean;
  gpu_type: string | null;
  region: string | null;
  filesystem: string | null;
  launch_id: string | null;
  target_instance_id: string | null;
  lifecycle: Lifecycle | null;
  lifecycle_detail: string | null;
  lifecycle_events: Record<string, string>;
  launch_to_ready_seconds: number | null;
};

export type AutoManageConfig = {
  gpu_type: string;
  region: string;
  filesystem: string;
};

export type Watch = {
  id: string;
  created_at: string;
  instance_type: string;
  region: string;
  filesystem: string | null;
  auto_launch: number;
  status: "watching" | "available" | "launched" | "cancelled";
  last_checked: string | null;
  triggered_at: string | null;
};

export type Estimate = {
  template: string;
  instance_type: string;
  minutes: number | null;
  cost_usd: number | null;
  confidence: "measured" | "rough" | "none";
  basis: string;
  sample_size: number;
};

export type Utilization = {
  available: boolean;
  reason?: string;
  gpu_description?: string;
  runtime_seconds?: number | null;
  peak_vram_used_mib?: number;
  vram_total_mib?: number;
  avg_util_pct?: number;
  sample_count?: number;
  right_size_hint?: boolean;
  verdict?: string;
  hint?: string;
};

export type Brain = {
  ref: string; // "instance:<id>" | "local:<endpoint>/<model>" | "api:<name>"
  kind: "instance" | "local" | "api";
  label: string;
  model: string;
  detail: string;
  ready: boolean;
};

export type Approval = {
  id: string;
  run_id: string;
  run_goal: string | null;
  seq: number;
  action: string;
  args: Record<string, unknown>;
  status: "pending" | "approved" | "denied" | "expired";
  created_at: string;
};

export type AgentRun = {
  id: string;
  created_at: string;
  goal: string;
  brain_instance_id: string;
  brain_model: string | null;
  status: "running" | "succeeded" | "failed" | "cancelled" | "exhausted";
  max_steps: number;
  steps_taken: number;
  summary: string | null;
  error: string | null;
  finished_at: string | null;
};

export type AgentStep = {
  seq: number;
  at: string;
  thought: string | null;
  action: string;
  args: Record<string, unknown>;
  result: Record<string, unknown>;
};

export const api = {
  instanceTypes: () =>
    request<Record<string, InstanceTypeInfo>>("/instance-types"),

  regions: () =>
    request<{ regions: Region[] }>("/regions").then((r) => r.regions),

  filesystems: () =>
    request<{ filesystems: Filesystem[] }>("/filesystems").then(
      (r) => r.filesystems,
    ),

  sshKeys: () =>
    request<{ ssh_keys: string[]; default: string }>("/ssh-keys"),

  instances: () =>
    request<{ instances: Instance[] }>("/instances").then((r) => r.instances),

  launches: () =>
    request<{ launches: Launch[] }>("/launches").then((r) => r.launches),

  launch: (body: LaunchRequest) =>
    request<{ launch: Launch }>("/instances", {
      method: "POST",
      body: JSON.stringify(body),
    }).then((r) => r.launch),

  terminate: (instanceId: string, force = false) =>
    request<{ terminated: boolean }>(
      `/instances/${instanceId}${force ? "?force=true" : ""}`,
      { method: "DELETE" },
    ),

  syncEphemeral: (instanceId: string) =>
    request<{ synced_to: string }>(`/instances/${instanceId}/sync`, {
      method: "POST",
    }),

  setKeepAlive: (instanceId: string, enabled: boolean) =>
    request<{ keep_alive: boolean }>(`/instances/${instanceId}/keep-alive`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),

  diagnoseSidecar: (instanceId: string) =>
    request<SidecarDiagnosis>(`/instances/${instanceId}/sidecar/diagnose`),

  templates: () =>
    request<{ templates: Template[]; errors: Record<string, string> }>(
      "/templates",
    ),

  tasks: () => request<{ tasks: Task[] }>("/tasks").then((r) => r.tasks),

  enqueueTask: (
    template: string,
    parameters: Record<string, unknown>,
    auto?: AutoManageConfig,
    targetInstanceId?: string,
  ) =>
    request<{ task: Task }>("/tasks", {
      method: "POST",
      body: JSON.stringify({
        template,
        parameters,
        ...(auto ? { auto_manage: true, ...auto } : {}),
        ...(!auto && targetInstanceId
          ? { target_instance_id: targetInstanceId }
          : {}),
      }),
    }).then((r) => r.task),

  cancelTask: (taskId: string) =>
    request<{ cancelled: string }>(`/tasks/${taskId}/cancel`, {
      method: "POST",
    }),

  taskLogs: (taskId: string, tail?: number) =>
    request<{ lines: { seq: number; at: string; line: string }[] }>(
      `/tasks/${taskId}/logs${tail ? `?tail=${tail}` : ""}`,
    ).then((r) => r.lines),

  deleteTask: (taskId: string) =>
    request<{ deleted: string }>(`/tasks/${taskId}`, { method: "DELETE" }),

  clearFinishedTasks: () =>
    request<{ cleared: number }>("/tasks/finished", { method: "DELETE" }),

  modelPresets: () =>
    request<{ presets: ModelPreset[] }>("/model-presets").then(
      (r) => r.presets,
    ),

  estimate: (template: string, instanceType: string) =>
    request<Estimate>(
      `/estimate?template=${encodeURIComponent(template)}` +
        `&instance_type=${encodeURIComponent(instanceType)}`,
    ),

  launchUtilization: (launchId: string) =>
    request<Utilization>(`/launches/${launchId}/utilization`),

  settingsStatus: () =>
    request<{
      mock: boolean;
      lambda_configured: boolean;
      s3_configured: boolean;
      tailscale_available: boolean;
      env_path: string;
    }>("/settings/status"),

  setLambdaKey: (apiKey: string) =>
    request<{ valid: boolean; instance_types_visible: number; applied_live: boolean }>(
      "/settings/lambda-key",
      { method: "POST", body: JSON.stringify({ api_key: apiKey }) },
    ),

  setS3Keys: (accessKeyId: string, secretAccessKey: string) =>
    request<{ saved: boolean; validated: boolean }>("/settings/s3-keys", {
      method: "POST",
      body: JSON.stringify({
        access_key_id: accessKeyId,
        secret_access_key: secretAccessKey,
      }),
    }),

  brains: () => request<{ brains: Brain[] }>("/brains").then((r) => r.brains),

  approvals: () =>
    request<{ approvals: Approval[] }>("/autopilot/approvals").then(
      (r) => r.approvals,
    ),

  decideApproval: (id: string, approve: boolean) =>
    request<{ approval: Approval }>(`/autopilot/approvals/${id}`, {
      method: "POST",
      body: JSON.stringify({ approve }),
    }),

  autopilotRuns: () =>
    request<{ runs: AgentRun[] }>("/autopilot/runs").then((r) => r.runs),

  autopilotRun: (runId: string) =>
    request<AgentRun & { steps: AgentStep[] }>(`/autopilot/runs/${runId}`),

  startAutopilot: (body: {
    goal: string;
    brain?: string; // full brain ref (instance:/local:/api:)
    brain_instance_id?: string; // legacy spelling for instance brains
    max_steps?: number;
    require_approval?: boolean;
  }) =>
    request<{ run: AgentRun }>("/autopilot/runs", {
      method: "POST",
      body: JSON.stringify(body),
    }).then((r) => r.run),

  cancelAutopilot: (runId: string) =>
    request<{ cancelling: boolean }>(`/autopilot/runs/${runId}/cancel`, {
      method: "POST",
    }),

  audit: (actor?: string, limit = 200) =>
    request<{
      entries: {
        id: number;
        at: string;
        actor: string;
        action: string;
        detail: string;
      }[];
    }>(
      `/audit?limit=${limit}${actor ? `&actor=${encodeURIComponent(actor)}` : ""}`,
    ).then((r) => r.entries),

  modelStatus: (instanceId: string) =>
    request<{
      serving: boolean;
      ready: boolean;
      status_detail?: string;
      task_id?: string;
      template?: string;
      model_id?: string;
      port?: number;
    }>(`/instances/${instanceId}/model`),

  listDir: (instanceId: string, rootName: string, path: string) =>
    request<{
      root: string;
      path: string;
      entries: {
        name: string;
        is_dir: boolean;
        size_bytes: number;
        modified: string;
      }[];
    }>(
      `/instances/${instanceId}/files/list?root_name=${rootName}&path=${encodeURIComponent(path)}`,
    ),

  dirUsage: (instanceId: string, rootName: string, path: string) =>
    request<{
      children: {
        name: string;
        is_dir: boolean;
        total_bytes: number;
        file_count: number;
      }[];
      truncated: boolean;
    }>(
      `/instances/${instanceId}/files/usage?root_name=${rootName}&path=${encodeURIComponent(path)}`,
    ),

  deletePath: (
    instanceId: string,
    rootName: string,
    path: string,
    recursive: boolean,
  ) =>
    request<{ deleted: string }>(
      `/instances/${instanceId}/files?root_name=${rootName}&path=${encodeURIComponent(path)}&recursive=${recursive}`,
      { method: "DELETE" },
    ),

  archiveUrl: (instanceId: string, absolutePath: string) =>
    `${API_BASE}/instances/${instanceId}/files/archive?path=${encodeURIComponent(absolutePath)}`,

  uploadFile: async (instanceId: string, file: File, dest = "inbox/") => {
    const form = new FormData();
    form.append("file", file);
    form.append("dest", dest);
    // No content-type header: the browser sets the multipart boundary.
    const resp = await fetch(
      `${API_BASE}/instances/${instanceId}/files/upload`,
      { method: "POST", body: form },
    );
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new ApiError(resp.status, body.detail ?? `HTTP ${resp.status}`);
    }
    return body as { path: string; bytes: number };
  },

  downloadUrl: (instanceId: string, absolutePath: string) =>
    `${API_BASE}/instances/${instanceId}/files/download?path=${encodeURIComponent(absolutePath)}`,

  recentFiles: (instanceId: string, hours = 24, limit = 50) =>
    request<{
      files: { root: string; path: string; size_bytes: number; modified: string }[];
      truncated: boolean;
      hours: number;
    }>(`/instances/${instanceId}/files/recent?hours=${hours}&limit=${limit}`),

  watches: () =>
    request<{ watches: Watch[]; auto_launch_enabled: boolean }>("/watches"),

  createWatch: (body: {
    instance_type: string;
    region: string;
    filesystem?: string;
    auto_launch?: boolean;
  }) =>
    request<{ watch: Watch }>("/watches", {
      method: "POST",
      body: JSON.stringify(body),
    }).then((r) => r.watch),

  cancelWatch: (watchId: string) =>
    request<{ watch: Watch }>(`/watches/${watchId}`, { method: "DELETE" }),

  storageFiles: (filesystem: string, prefix = "") =>
    request<{ files: StoredFile[] }>(
      `/storage/files?filesystem=${encodeURIComponent(filesystem)}&prefix=${encodeURIComponent(prefix)}`,
    ).then((r) => r.files),

  deleteFile: (filesystem: string, key: string) =>
    request<{ deleted: string }>(
      `/storage/files/${encodeURI(key)}?filesystem=${encodeURIComponent(filesystem)}`,
      { method: "DELETE" },
    ),
};
