import * as vscode from "vscode";
import type { Decision, Status } from "./types";

export interface SidebarState {
  folder?: string;
  policy?: boolean;
  restricted?: boolean;
  error?: string;
  missingPython?: boolean;
  busy?: string;
  status?: Status;
  preview?: Decision;
}

const commands = new Map([
  ["compile", "covenant.compile"],
  ["approve", "covenant.approve"],
  ["init", "covenant.init"],
  ["refresh", "covenant.refresh"],
  ["openFolder", "workbench.action.files.openFolder"],
  ["trust", "workbench.trust.manage"],
  ["python", "covenant.selectPython"],
  ["openPolicy", "covenant.openPolicy"],
  ["copyInstall", "covenant.copyInstall"],
  ["output", "covenant.showLog"],
]);

export class Sidebar implements vscode.WebviewViewProvider {
  private state: SidebarState = {};
  private view?: vscode.WebviewView;

  constructor(private readonly extensionUri: vscode.Uri) {}

  update(state: SidebarState): void {
    this.state = state;
    void this.view?.webview.postMessage({ type: "state", state });
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    const media = vscode.Uri.joinPath(this.extensionUri, "media");
    const webview = view.webview;
    webview.options = { enableScripts: true, localResourceRoots: [media] };
    const messages = webview.onDidReceiveMessage(async (message: { action?: unknown }) => {
      if (message?.action === "ready") {
        await webview.postMessage({ type: "state", state: this.state });
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
