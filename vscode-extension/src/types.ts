// Shapes of the JSON the Covenant CLI prints (`status --format json`, `check --preview`, `compile --format json`).

export interface Clause {
  id: string;
  kind: "EffectProhibition" | "SemanticInvariant" | "TemporalObligation";
  source_span: string;
  scope?: string[];
  checker?: string | null;
  params?: Record<string, unknown>;
  on_violation?: string;
  assumptions?: string[];
  status?: "enforced" | "unenforceable" | "pending_clarification";
}

export interface Report {
  violation: string;
  clause: string;
  kind?: Clause["kind"] | null;
  checker: string;
  on_violation: string;
  blocked_action?: string | null;
  evidence: Record<string, unknown>;
  repair_hints: string[];
  options: string[];
  escalated: boolean;
}

export interface Decision {
  event: string;
  allowed: boolean;
  reports: Report[];
}

export interface LoggedDecision extends Decision {
  ts: string;
  counted: string[];
}

export interface Status {
  repo: string;
  covenant_path: string;
  state_dir: string;
  has_session: boolean;
  session: { started_at?: string | null; baselines?: Record<string, string> | null };
  repo_covenant_changed: boolean;
  clauses: Clause[];
  protected: { clause: string; paths: string[]; exclude: string[] }[];
  test_commands: string[];
  trace_length: number;
  last_events: { type: string; ts: string; [key: string]: unknown }[];
  block_counts: Record<string, number>;
  escalate_after: number;
  recent_reports: LoggedDecision[];
}

export interface CompileQuestion {
  id: string;
  clause: string;
  text: string;
  options: string[];
  default: number;
  chosen: number;
  how: "answered" | "default";
}

export interface CompileResult {
  covenant: { clauses: Clause[] };
  yaml: string;
  questions: CompileQuestion[];
  issues: { clause: string; level: "error" | "warning"; message: string }[];
  facts: string;
}
