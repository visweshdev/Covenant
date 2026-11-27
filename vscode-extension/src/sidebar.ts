import * as vscode from "vscode";
import { isReminder, message as reportMessage } from "./locate";
import { starters } from "./prompts";
import type { Decision, Status } from "./types";

export interface SidebarState {
  folder?: string;
  folderKey?: string;
  canSwitch?: boolean;
  policy?: boolean;
  restricted?: boolean;
  error?: string;
  missingPython?: boolean;
  busy?: string;
  status?: Status;
  preview?: Decision;
}

const commands = new Map([
  ["selectWorkspace", "covenant.selectWorkspace"],
  ["compile", "covenant.compile"],
  ["approve", "covenant.approve"],
  ["init", "covenant.init"],
  ["refresh", "covenant.refresh"],
  ["openFolder", "workbench.action.files.openFolder"],
  ["trust", "workbench.trust.manage"],
  ["python", "covenant.selectPython"],
  ["amend", "covenant.amend"],
  ["test", "covenant.runTests"],
  ["openPolicy", "covenant.openPolicy"],
  ["copyInstall", "covenant.copyInstall"],
  ["copyMcp", "covenant.copyMcp"],
  ["copyHandoff", "covenant.copyHandoff"],
  ["output", "covenant.showLog"],
]);

export class Sidebar implements vscode.WebviewViewProvider {
  private state: SidebarState = {};
  private view?: vscode.WebviewView;
  private revision = 0;
  private workspaceId = 0;

  constructor(private readonly extensionUri: vscode.Uri) {}

  update(state: SidebarState): void {
    if (this.state.folderKey !== state.folderKey) this.workspaceId += 1;
    this.state = state;
    this.revision += 1;
    if (this.view) void this.send(this.view.webview);
  }

  private send(webview: vscode.Webview): Thenable<boolean> {
    const { status, preview, folderKey: _folderKey, ...workspace } = this.state;
    return webview.postMessage({
      type: "state", revision: this.revision, workspaceId: this.workspaceId,
      state: {
        ...workspace, starters,
        status: status && {
          has_session: status.has_session,
          repo_covenant_changed: status.repo_covenant_changed,
          testCount: status.test_commands.length,
          rules: status.clauses.map(({ id, source_span, status: ruleStatus, checker, on_violation, scope, assumptions, origin }) => ({
            id, text: source_span, status: ruleStatus ?? "enforced", checker,
            response: on_violation, scope, assumptions, origin, blocks: status.block_counts[id] ?? 0,
          })),
        },
        reports: (preview?.reports ?? []).map((report, index) => ({
          index, id: report.violation, text: reportMessage(report), reminder: isReminder(report),
        })),
      },
    });
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    const media = vscode.Uri.joinPath(this.extensionUri, "media");
    const webview = view.webview;
    webview.options = { enableScripts: true, localResourceRoots: [media] };
    const messages = webview.onDidReceiveMessage(async (message: { action?: unknown; value?: unknown; revision?: unknown; workspaceId?: unknown }) => {
      if (message?.action === "ready") {
        await this.send(webview);
      } else if (message?.workspaceId !== this.workspaceId) {
        return;
      } else if (message?.action === "draftStarter" || message?.action === "copyStarter") {
        const starter = starters.find((item) => item.id === message.value);
        if (!starter) return;
        const command = message.action === "draftStarter" ? "covenant.draftStarter" : "covenant.copyStarter";
        await vscode.commands.executeCommand(command, starter.id);
      } else if (message?.action === "rule" && this.revision === message.revision) {
        const rule = this.state.status?.clauses.find((clause) => clause.id === message.value);
        if (rule && !rule.origin) await vscode.commands.executeCommand("covenant.openRule", rule.id);
      } else if (message?.action === "report" && this.revision === message.revision && typeof message.value === "string" && /^\d+$/.test(message.value)) {
        const index = Number(message.value);
        const report = Number.isInteger(index) ? this.state.preview?.reports[index] : undefined;
        if (report) await vscode.commands.executeCommand("covenant.openViolation", report);
      } else if (typeof message?.action === "string") {
        const command = commands.get(message.action);
        if (command) await vscode.commands.executeCommand(command);
      }
    });
    view.onDidDispose(() => {
      messages.dispose();
      if (this.view === view) this.view = undefined;
    });
    const resource = (name: string) => webview.asWebviewUri(vscode.Uri.joinPath(media, name));
    webview.html = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src ${webview.cspSource}">
  <link rel="stylesheet" href="${resource("sidebar.css")}">
  <title>Covenant</title>
</head>
<body>
  <main id="panel" aria-label="Covenant workspace"><p class="muted">Loading workspace…</p></main>
  <script src="${resource("sidebar.js")}" defer></script>
</body>
</html>`;
  }
}
