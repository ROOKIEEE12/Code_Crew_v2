// Typed client for the CodeCrew local API (codecrew/server).
//
// In production the UI is served by `codecrew serve` itself, so requests go
// to the same origin. Under `next dev` the API runs separately on :8765 and
// allows the dev origin explicitly.

export const API_BASE =
  process.env.NEXT_PUBLIC_CODECREW_API ??
  (process.env.NODE_ENV === "development" ? "http://127.0.0.1:8765" : "");

export type Verdict =
  | "VERIFIED"
  | "PARTIALLY VERIFIED"
  | "UNVERIFIED"
  | "FAILED TO BUILD"
  | "NOT VERIFIED";

export type GateStatus = "PASS" | "FAIL" | "SKIP" | "ERROR";

export interface Finding {
  severity: "info" | "low" | "medium" | "high" | "critical";
  code: string;
  message: string;
  file: string | null;
  line: number | null;
}

export interface GateResult {
  gate_id: string;
  name: string;
  status: GateStatus;
  summary: string;
  score: number | null;
  findings: Finding[];
  duration_s: number;
  blocking: boolean;
  evidence: string | null;
  note: string | null;
}

export interface GateReport {
  run_id: string;
  workspace: string;
  sandbox: string;
  results: GateResult[];
  duration_s: number;
  passed: boolean;
  verdict: Verdict;
  counts: Record<GateStatus, number>;
}

export interface FileBadge {
  path: string;
  badge: "VERIFIED" | "PARTIALLY VERIFIED" | "UNVERIFIED";
  reasons: string[];
  notes: string[];
}

export interface Example {
  call: string;
  result?: string;
  raises?: string;
}

export interface SpecFunction {
  signature: string;
  description: string;
  raises: string[];
  examples: Example[];
}

export interface Spec {
  summary: string;
  module: string;
  functions: SpecFunction[];
  constraints: string[];
  security: string[];
  assumptions: string[];
  acceptance_criteria: string[];
  uses: string[];
}

export interface PlanModule {
  name: string;
  responsibility: string;
  requirement: string;
  depends_on: string[];
}

export interface Plan {
  summary: string;
  modules: PlanModule[];
  dependencies: { name: string; purpose: string }[];
  notes: string[];
}

export interface RepairStep {
  round: number;
  verdict: string | null;
  targets: string[];
  instruction: string;
  outcome: string;
  note: string;
}

export interface SourceFileView {
  path: string;
  role: "source" | "test";
  lines: number;
  content: string;
  truncated: boolean;
}

export interface RunResult {
  verdict: Verdict;
  verdict_note: string | null;
  initial_verdict: string | null;
  passed: boolean;
  failure: string | null;
  mode: string;
  provider: string;
  duration_s: number;
  cost: {
    llm_calls?: number;
    total_tokens?: number;
    retries?: number;
    llm_seconds?: number;
  };
  workspace: string;
  report: GateReport | null;
  badges: FileBadge[];
  files: SourceFileView[];
  spec: Spec | null;
  specs: Record<string, Spec>;
  plan: Plan | null;
  repairs: RepairStep[];
  agent_attempts: number;
  contract_rejections: string[];
  unverified_claim: string | null;
  usage: string | null;
}

export type JobStatus =
  | "queued"
  | "running"
  | "awaiting_approval"
  | "done"
  | "failed"
  | "cancelled";

export interface TraceEvent {
  kind: string;
  t?: number;
  [key: string]: unknown;
}

export interface JobSummary {
  id: string;
  kind: "build" | "verify" | "repair";
  status: JobStatus;
  workspace: string;
  created: number;
  started: number | null;
  finished: number | null;
  title: string;
  verdict: Verdict | null;
  error: string | null;
}

export interface PendingApproval {
  type: "spec" | "plan";
  payload: Spec | Plan;
  module: string | null;
  requested: number;
}

export interface JobDetail extends JobSummary {
  params: Record<string, unknown>;
  pending: PendingApproval | null;
  result: RunResult | null;
  events: TraceEvent[];
  next: number;
}

export interface Health {
  version: string;
  python?: string;
  providers: { name: string; ready: boolean; reason: string }[];
  replays: string[];
  sandbox: { docker: boolean };
  tools?: { ready: boolean; path: string };
  roots: string[];
}

export interface WorkspaceSummary {
  name: string;
  path: string;
  modified: number;
  verdict: Verdict | null;
}

export interface BuildRequest {
  kind: "build";
  requirement: string;
  mode: "single" | "multi" | "project";
  provider: string;
  repair: number;
  approval: "review" | "auto";
  script?: string;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: init?.body ? { "Content-Type": "application/json" } : undefined,
      cache: "no-store",
    });
  } catch {
    throw new ApiError(
      "Cannot reach the CodeCrew API. Start it with `codecrew serve`.",
      0,
    );
  }
  const data = (await response.json().catch(() => ({}))) as { error?: string };
  if (!response.ok) {
    throw new ApiError(data.error ?? `HTTP ${response.status}`, response.status);
  }
  return data as T;
}

export const api = {
  health: () => call<Health>("/api/health"),
  jobs: () => call<{ jobs: JobSummary[] }>("/api/jobs"),
  job: (id: string, since = 0) =>
    call<JobDetail>(`/api/jobs/${encodeURIComponent(id)}?since=${since}`),
  submit: (body: BuildRequest | { kind: "verify"; workspace: string }) =>
    call<JobDetail>("/api/jobs", { method: "POST", body: JSON.stringify(body) }),
  decide: (id: string, approve: boolean, edited?: Spec | Plan) =>
    call<{ ok: true }>(`/api/jobs/${encodeURIComponent(id)}/approval`, {
      method: "POST",
      body: JSON.stringify(edited ? { approve, edited } : { approve }),
    }),
  cancel: (id: string) =>
    call<JobSummary>(`/api/jobs/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
      body: "{}",
    }),
  workflow: (id: string) =>
    call<{ workflow: string }>(`/api/jobs/${encodeURIComponent(id)}/ci`),
  workspaces: () => call<{ workspaces: WorkspaceSummary[] }>("/api/workspaces"),
  workspace: (path: string) =>
    call<RunResult>(`/api/workspace?path=${encodeURIComponent(path)}`),
  evaluations: () => call<{ evaluations: Evaluation[] }>("/api/evaluations"),
};

export interface RunSummary {
  run_id: string;
  verdict: Verdict | null;
  passed: boolean | null;
  time: number;
  duration_s: number;
  counts: Partial<Record<GateStatus, number>>;
  coverage: number | null;
  mutation: number | null;
  tests: number | null;
  profile: "quick" | "full";
  gates: Record<string, GateStatus>;
}

export interface ProjectSummary {
  name: string;
  path: string;
  registered: boolean;
  kind: "project" | "generated";
  exists: boolean;
  latest: RunSummary | null;
  /** Oldest first, for trends. */
  history: RunSummary[];
  runs: number;
}

export interface FindingView {
  gate: string;
  gate_name: string;
  gate_status: GateStatus;
  blocking: boolean;
  severity: Finding["severity"];
  code: string;
  message: string;
  file: string | null;
  line: number | null;
  excerpt: { start: number; line: number; lines: string[] } | null;
}

export interface ProjectInfo {
  config_error: string | null;
  source_root?: string;
  settings?: Record<string, unknown>;
  settings_file?: string | null;
  dependencies?: number;
  requirement_files?: string[];
  generated?: boolean;
  install_dependencies?: boolean;
  warnings?: string[];
}

export interface ProjectDetail extends Omit<ProjectSummary, "history"> {
  /** Newest first. */
  history: RunSummary[];
  info: ProjectInfo;
  report: (GateReport & { notes?: string[]; sources?: string[] }) | null;
  badges: FileBadge[];
  findings: FindingView[];
  verdict_note: string | null;
}

export const projectsApi = {
  list: () => call<{ projects: ProjectSummary[] }>("/api/projects"),
  add: (path: string) =>
    call<ProjectSummary>("/api/projects", { method: "POST", body: JSON.stringify({ path }) }),
  remove: (path: string) =>
    call<{ ok: true }>("/api/projects/remove", { method: "POST", body: JSON.stringify({ path }) }),
  detail: (path: string, run?: string) =>
    call<ProjectDetail>(
      `/api/project?path=${encodeURIComponent(path)}${run ? `&run=${encodeURIComponent(run)}` : ""}`,
    ),
  file: (path: string, file: string) =>
    call<{ path: string; content: string; modified: number }>(
      `/api/project/file?path=${encodeURIComponent(path)}&file=${encodeURIComponent(file)}`,
    ),
  verify: (path: string, profile: "quick" | "full") =>
    call<JobDetail>("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ kind: "verify", workspace: path, profile }),
    }),
};

export interface ConditionSummary {
  condition: string;
  judge: string;
  runs: number;
  built: number;
  claimed_ok: number;
  false_confidence: number;
  false_confidence_rate: number | null;
  fc_ci: [number, number] | null;
  hidden_pass_rate: number | null;
  hp_ci: [number, number] | null;
  mean_mutation_score: number | null;
  mean_tokens: number | null;
}

export interface PairedComparison {
  a: string;
  b: string;
  pairs: number;
  a_only_pass: number;
  b_only_pass: number;
  p_value: number;
}

export interface GateValue {
  condition: string;
  own_tests_false_confidence: number;
  caught_by_full_gates: number;
  extra_rejections_of_correct_code: number;
}

export interface Evaluation {
  name: string;
  path: string;
  meta: { provider?: string; reps?: number; tasks?: string[]; conditions?: string[]; [k: string]: unknown };
  conditions: ConditionSummary[];
  paired: PairedComparison[];
  gate_value: GateValue[];
  per_task: Record<string, Record<string, string>>;
  pending: number;
  completed: number;
}
