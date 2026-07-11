// Typed client for the local Manifold backend. The dashboard is a thin
// consumer: no business logic here, just fetch + types + error surfacing.

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { "content-type": "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError(0, "Backend unreachable. Is it running on :8000?");
  }
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new ApiError(resp.status, body.detail ?? `HTTP ${resp.status}`);
  }
  return body as T;
}

export type InstanceTypeInfo = {
  description: string;
  gpu_description: string;
  price_usd_per_hour: number;
  specs: { vcpus: number; memory_gib: number; storage_gib: number; gpus: number };
  regions_with_capacity: string[];
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

export const api = {
  instanceTypes: () =>
    request<Record<string, InstanceTypeInfo>>("/instance-types"),

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

  terminate: (instanceId: string) =>
    request<{ terminated: boolean }>(`/instances/${instanceId}`, {
      method: "DELETE",
    }),

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
