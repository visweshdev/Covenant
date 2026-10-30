// Where in the repo a violation belongs, so the editor can underline it.
// Pure functions: the extension passes in file access, tests pass in fixtures.

import type { Report } from "./types";

export interface Place {
  file: string; // repo-relative, forward slashes
  line: number; // 0-based
}

export interface FileAccess {
  read(rel: string): string | undefined;
  /** Repo-relative files under a folder, filtered by extension. */
  list(dir: string, ext: string): string[];
}

const str = (v: unknown): string | undefined => (typeof v === "string" && v ? v : undefined);

export function lineOf(text: string, pattern: RegExp): number {
  const lines = text.split(/\r?\n/);
  const index = lines.findIndex((l) => pattern.test(l));
  return index < 0 ? 0 : index;
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** The line of `- id: C1` in covenant.yaml. */
export function clauseLine(covenantText: string, clauseId: string): number {
  return lineOf(covenantText, new RegExp(`^\\s*-?\\s*id:\\s*["']?${escapeRe(clauseId)}["']?\\s*$`));
}

function pythonDefinition(files: FileAccess, dir: string, name: string): Place | undefined {
  const re = new RegExp(`^\\s*(?:async\\s+)?(?:def|class)\\s+${escapeRe(name)}\\b`);
  for (const file of files.list(dir, ".py")) {
    const text = files.read(file);
    if (text && text.split(/\r?\n/).some((l) => re.test(l))) return { file, line: lineOf(text, re) };
  }
  return undefined;
}

function tsDefinition(files: FileAccess, file: string, name: string): Place {
  const text = files.read(file) ?? "";
  const re = new RegExp(`\\b(?:function|class|interface|type|const|let|enum)\\s+${escapeRe(name)}\\b|\\b${escapeRe(name)}\\s*[(:<]`);
  return { file, line: lineOf(text, re) };
}

function dirname(rel: string): string {
  const i = rel.lastIndexOf("/");
  return i < 0 ? "" : rel.slice(0, i);
}

export function locate(report: Report, files: FileAccess, covenantRel: string): Place {
  const ev = report.evidence ?? {};
  const fallback: Place = { file: covenantRel, line: clauseLine(files.read(covenantRel) ?? "", report.violation) };

  // Protected files, migrations, tampering: the evidence names the file (and maybe the line).
  const target = str(ev.target) ?? (report.violation === "INTEGRITY" ? str(ev.path) : undefined);
  if (target && ev.channel !== "db" && !target.startsWith("~") && !/^[A-Za-z]:[\\/]|^\//.test(target)) {
    const line = typeof ev.line === "number" ? Math.max(0, ev.line - 1) : 0;
    return { file: target.replace(/\\/g, "/"), line };
  }

  // Go, Rust and Java API changes: the checker found the definition itself.
  const file = str(ev.file);
  if (file && typeof ev.line === "number" && report.checker === "source_api_diff") {
    return { file: file.replace(/\\/g, "/"), line: Math.max(0, ev.line - 1) };
  }

  const symbol = str(ev.symbol);
  if (symbol && report.checker === "api_surface_diff") {
    const parts = symbol.split(".");
    const exportedFrom = str(ev.exported_from) ?? `${parts[0]}/__init__.py`;
    // "pkg.Class.method" -> look for the method first, then the class.
    for (const name of [parts[parts.length - 1], parts[Math.max(1, parts.length - 2)]]) {
      const hit = pythonDefinition(files, dirname(exportedFrom), name);
      if (hit) return hit;
    }
    return { file: exportedFrom, line: 0 };
  }
  if (symbol && report.checker === "ts_api_surface_diff") {
    const [file, path] = symbol.split(":");
    const names = (path ?? "").split(".");
    return tsDefinition(files, file, names[names.length - 1] === "static" ? names[0] : names[names.length - 1]);
  }
  if (symbol && report.checker === "dependency_diff") {
    const candidates = ev.ecosystem === "js" ? ["package.json", "package-lock.json"] : ["pyproject.toml", "requirements.txt", "poetry.lock", "uv.lock"];
    for (const file of candidates) {
      const text = files.read(file);
      if (text !== undefined) return { file, line: lineOf(text, new RegExp(`["']?${escapeRe(symbol)}["'\\s=<>~^!:]`, "i")) };
    }
  }
  return fallback;
}

/** One line for the Problems panel. */
export function message(report: Report): string {
  const ev = report.evidence ?? {};
  let detail = "";
  if (str(ev.symbol) && (ev.before !== undefined || ev.after !== undefined)) {
    detail = ev.after === null || ev.after === undefined
      ? `${ev.symbol} was removed`
      : ev.before === null || ev.before === undefined
        ? `${ev.symbol} was added`
        : `${ev.symbol} changed: ${ev.before} -> ${ev.after}`;
  } else if (str(ev.operation)) {
    detail = `${ev.operation}: ${ev.code ?? ""}`;
  } else if (str(ev.target)) {
    detail = `${ev.pending ? "changed" : String(ev.op ?? "changed")} ${ev.target}${ev.pending ? " (will be restored at the agent's next step)" : ""}`;
  } else if (ev.counts_so_far && typeof ev.counts_so_far === "object") {
    const counts = Object.entries(ev.counts_so_far as Record<string, number>).map(([a, n]) => `${a} ${n}`).join(", ");
    detail = `limit reached (so far: ${counts})`;
  } else if (Array.isArray(ev.edits_since_last_passing_test)) {
    detail = ev.last_passing_test ? "files changed since the last passing test run" : "no passing test run yet";
  } else if (str(ev.problem)) {
    detail = `${ev.path}: ${ev.problem}`;
  } else if (str(ev.error)) {
    detail = `checker could not run: ${ev.error}`;
  }
  const hint = report.repair_hints?.[0] ? ` Fix: ${report.repair_hints[0]}` : "";
  return `${report.violation} (${report.clause})${detail ? `: ${detail}` : ""}.${hint}`;
}

/** Ordering rules (tests before commit) are something still to do, not a defect in the code. */
export function isReminder(report: Report): boolean {
  return report.kind === "TemporalObligation";
}
