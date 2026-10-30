import * as vscode from "vscode";
import { message } from "./locate";
import type { Clause, Decision, LoggedDecision, Report, Status } from "./types";

type Node =
  | { kind: "session" }
  | { kind: "rules" }
  | { kind: "rule"; clause: Clause }
  | { kind: "detail"; text: string; tooltip?: string }
  | { kind: "violation"; report: Report }
  | { kind: "recent" }
  | { kind: "logged"; entry: LoggedDecision };

export class RulesTree implements vscode.TreeDataProvider<Node> {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;
  status?: Status;
  preview?: Decision;
  error?: string;

  update(status: Status | undefined, preview: Decision | undefined, error?: string): void {
    this.status = status;
    this.preview = preview;
    this.error = error;
    this.changed.fire();
  }

  private violationsFor(id: string): Report[] {
    return (this.preview?.reports ?? []).filter((r) => r.violation === id);
  }

  getChildren(node?: Node): Node[] {
    if (!this.status) return this.error ? [{ kind: "detail", text: this.error }] : [];
    if (!node) {
      const roots: Node[] = [{ kind: "session" }, { kind: "rules" }];
      if (this.status.recent_reports.length) roots.push({ kind: "recent" });
      return roots;
    }
    switch (node.kind) {
      case "session": {
        const out: Node[] = [];
        for (const r of this.violationsFor("INTEGRITY")) out.push({ kind: "violation", report: r });
        const tests = this.status.test_commands.join(", ");
        if (tests) out.push({ kind: "detail", text: `Tests: ${tests}` });
        out.push({ kind: "detail", text: `${this.status.trace_length} events recorded this task` });
        out.push({ kind: "detail", text: `State: ${this.status.state_dir}` });
        return out;
      }
      case "rules":
        return this.status.clauses.map((clause) => ({ kind: "rule", clause }));
      case "rule": {
        const c = node.clause;
        const out: Node[] = this.violationsFor(c.id).map((report) => ({ kind: "violation", report }));
        out.push({ kind: "detail", text: `Checker: ${c.checker ?? "none"}${c.on_violation ? ` · on violation: ${c.on_violation}` : ""}` });
        for (const s of c.scope ?? []) out.push({ kind: "detail", text: `Applies to ${s}` });
        for (const a of c.assumptions ?? []) out.push({ kind: "detail", text: `Assumes ${a}`, tooltip: a });
        const blocks = this.status.block_counts[c.id];
        if (blocks) out.push({ kind: "detail", text: `Blocked the agent ${blocks}× this task (hands over to you at ${this.status.escalate_after})` });
        return out;
      }
      case "recent":
        return [...this.status.recent_reports].reverse().slice(0, 20).map((entry) => ({ kind: "logged", entry }));
      case "logged":
        return node.entry.reports.map((report) => ({ kind: "violation", report }));
      default:
        return [];
    }
  }

  getTreeItem(node: Node): vscode.TreeItem {
    const C = vscode.TreeItemCollapsibleState;
    switch (node.kind) {
      case "session": {
        const s = this.status!;
        const item = new vscode.TreeItem("Task", C.Expanded);
        if (!s.has_session) {
          item.description = "not started: approve the rules to begin";
          item.iconPath = new vscode.ThemeIcon("debug-pause");
          item.command = { command: "covenant.approve", title: "Approve" };
        } else if (s.repo_covenant_changed) {
          item.description = "covenant.yaml changed; the approved version still applies";
          item.iconPath = new vscode.ThemeIcon("warning", new vscode.ThemeColor("problemsWarningIcon.foreground"));
          item.command = { command: "covenant.approve", title: "Approve" };
        } else {
          item.description = s.session.started_at ? `started ${new Date(s.session.started_at).toLocaleString()}` : "running";
          item.iconPath = new vscode.ThemeIcon("shield");
        }
        return item;
      }
      case "rules": {
        const enforced = this.status!.clauses.filter((c) => (c.status ?? "enforced") === "enforced").length;
        const item = new vscode.TreeItem("Rules", C.Expanded);
        item.description = `${enforced} enforced of ${this.status!.clauses.length}`;
        return item;
      }
      case "rule": {
        const c = node.clause;
        const violations = this.violationsFor(c.id);
        const item = new vscode.TreeItem(`${c.id}  ${c.source_span}`, violations.length ? C.Expanded : C.Collapsed);
        const status = c.status ?? "enforced";
        if (status !== "enforced") {
          item.description = status === "unenforceable" ? "not checked automatically" : "needs your input";
          item.iconPath = new vscode.ThemeIcon("circle-slash");
        } else if (violations.some((r) => r.kind !== "TemporalObligation")) {
          item.description = `${violations.length} issue${violations.length > 1 ? "s" : ""}`;
          item.iconPath = new vscode.ThemeIcon("error", new vscode.ThemeColor("problemsErrorIcon.foreground"));
        } else if (violations.length) {
          item.description = "to do before the next commit";
          item.iconPath = new vscode.ThemeIcon("clock", new vscode.ThemeColor("problemsWarningIcon.foreground"));
        } else {
          item.description = "holds";
          item.iconPath = new vscode.ThemeIcon("pass", new vscode.ThemeColor("testing.iconPassed"));
        }
        item.tooltip = new vscode.MarkdownString(`**${c.id}** · ${c.kind}\n\n"${c.source_span}"`);
        item.command = { command: "covenant.openRule", title: "Open rule", arguments: [c.id] };
        item.contextValue = "rule";
        return item;
      }
      case "violation": {
        const r = node.report;
        const item = new vscode.TreeItem(message(r).replace(/^\S+ \([^)]*\): /, ""), C.None);
        item.tooltip = message(r);
        item.iconPath = new vscode.ThemeIcon(r.escalated ? "person" : r.kind === "TemporalObligation" ? "clock" : "error");
        item.command = { command: "covenant.openViolation", title: "Show", arguments: [r] };
        return item;
      }
      case "recent": {
        const item = new vscode.TreeItem("Recent blocks", C.Collapsed);
        item.description = `${this.status!.recent_reports.length} this task`;
        item.iconPath = new vscode.ThemeIcon("history");
        return item;
      }
      case "logged": {
        const e = node.entry;
        const ids = [...new Set(e.reports.map((r) => r.violation))].join(", ");
        const verb = e.allowed ? "reported" : "refused";
        const item = new vscode.TreeItem(`${e.event} ${verb}: ${ids}`, C.Collapsed);
        item.description = new Date(e.ts).toLocaleTimeString();
        item.iconPath = new vscode.ThemeIcon(e.allowed ? "info" : "circle-slash");
        return item;
      }
      case "detail": {
        const item = new vscode.TreeItem(node.text, C.None);
        item.tooltip = node.tooltip ?? node.text;
        return item;
      }
    }
  }
}
