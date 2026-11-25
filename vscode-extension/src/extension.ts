import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { CovenantCli, CovenantCliError } from "./cli";
import { protectingClause } from "./glob";
import { clauseLine, FileAccess, isReminder, locate, message } from "./locate";
import { selectPython, setting } from "./python";
import { RulesTree } from "./tree";
import { workspaceRelative } from "./workspace-path";
import type { CompileResult, Decision, Report, Status } from "./types";

let output: vscode.OutputChannel;

function findFolder(): vscode.WorkspaceFolder | undefined {
  const folders = (vscode.workspace.workspaceFolders ?? []).filter((folder) => folder.uri.scheme === "file");
  return folders.find((folder) => fs.existsSync(path.join(folder.uri.fsPath, "covenant.yaml"))) ?? folders[0];
}

function fileAccess(repo: string): FileAccess {
  return {
    read(r) {
      if (workspaceRelative(repo, path.resolve(repo, r)) === undefined) return undefined;
      try {
        return fs.readFileSync(path.join(repo, r), "utf8");
      } catch {
        return undefined;
      }
    },
    list(dir, ext) {
      if (workspaceRelative(repo, path.resolve(repo, dir)) === undefined) return [];
      const out: string[] = [];
      const walk = (d: string, depth: number) => {
        let entries: fs.Dirent[] = [];
        try {
          entries = fs.readdirSync(path.join(repo, d), { withFileTypes: true });
        } catch {
          return;
        }
        for (const e of entries) {
          const child = d ? `${d}/${e.name}` : e.name;
          if (e.isDirectory() && depth < 6 && !["node_modules", ".git", "__pycache__", ".venv"].includes(e.name)) walk(child, depth + 1);
          else if (e.isFile() && e.name.endsWith(ext)) out.push(child);
        }
      };
      walk(dir, 0);
      return out.sort((a, b) => a.length - b.length);
    },
  };
}

class ProtectedFiles implements vscode.FileDecorationProvider {
  private readonly changed = new vscode.EventEmitter<undefined>();
  readonly onDidChangeFileDecorations = this.changed.event;
  constructor(private readonly repo: string, private status: () => Status | undefined) {}

  refresh(): void {
    this.changed.fire(undefined);
  }

  dispose(): void { this.changed.dispose(); }

  provideFileDecoration(uri: vscode.Uri): vscode.FileDecoration | undefined {
    const status = this.status();
    if (!status || uri.scheme !== "file") return undefined;
    const r = workspaceRelative(this.repo, uri.fsPath);
    if (!r) return undefined;
    const clause = protectingClause(r, status.protected);
    if (!clause) return undefined;
    const span = status.clauses.find((c) => c.id === clause)?.source_span ?? "";
    return new vscode.FileDecoration("🔒", `Protected by ${clause} (${span}): agents can't delete or overwrite it`);
  }
}

class Controller implements vscode.Disposable {
  readonly cli: CovenantCli;
  readonly diagnostics = vscode.languages.createDiagnosticCollection("covenant");
  readonly statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  readonly decorations: ProtectedFiles;
  status?: Status;
  preview?: Decision;
  private running?: Promise<void>;
  private again = false;
  private stateWatcher?: vscode.FileSystemWatcher;
  private watchedStateDir?: string;
  private warnedNotInstalled = false;
  private readonly resources: vscode.Disposable[] = [];
  private disposed = false;
  private activeAction?: string;

  constructor(readonly folder: vscode.WorkspaceFolder, readonly tree: RulesTree) {
    this.cli = new CovenantCli(this.repo, () => setting(folder, "pythonPath", "python"));
    this.decorations = new ProtectedFiles(this.repo, () => this.status);
    this.statusBar.command = "covenant.rules.focus";
    const covenantWatcher = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(folder, "covenant.yaml"));
    const check = () => void this.refresh();
    covenantWatcher.onDidChange(check);
    covenantWatcher.onDidCreate(check);
    covenantWatcher.onDidDelete(check);
    let timer: NodeJS.Timeout | undefined;
    this.resources.push(
      this.diagnostics, this.statusBar, this.decorations, covenantWatcher,
      vscode.window.registerFileDecorationProvider(this.decorations),
      vscode.workspace.onDidSaveTextDocument((doc) => {
        if (!setting(folder, "checkOnSave", true) || doc.uri.scheme !== "file" || !workspaceRelative(this.repo, doc.uri.fsPath)) return;
        clearTimeout(timer);
        timer = setTimeout(check, 1200);
      }),
      vscode.workspace.onDidChangeConfiguration((event) => {
        if (event.affectsConfiguration("covenant", folder.uri)) check();
      }),
      { dispose: () => clearTimeout(timer) },
    );
  }

  dispose(): void {
    this.disposed = true;
    this.stateWatcher?.dispose();
    for (const resource of this.resources) resource.dispose();
  }

  get isOpen(): boolean { return !this.disposed; }

  async act(label: string, action: () => Promise<void>): Promise<void> {
    if (this.activeAction) {
      void vscode.window.showInformationMessage(`Covenant is already running “${this.activeAction}”.`);
      return;
    }
    this.activeAction = label;
    try {
      await action();
    } catch (error) {
      output.appendLine(error instanceof Error ? error.message : String(error));
      void this.failure(`${label} failed.`);
    } finally {
      this.activeAction = undefined;
      await this.refresh();
    }
  }

  async failure(message: string): Promise<void> {
    if (!this.isOpen) return;
    const choice = await vscode.window.showErrorMessage(`Covenant: ${message}`, "Show Output");
    if (choice === "Show Output") output.show();
  }

  get repo(): string { return this.folder.uri.fsPath; }

  get covenantPath(): string {
    return path.join(this.repo, "covenant.yaml");
  }

  /** Re-check; calls made while a check is running coalesce into one more run. */
  refresh(): Promise<void> {
    if (this.disposed) return Promise.resolve();
    if (this.running) {
      this.again = true;
      return this.running;
    }
    this.running = this.drainRefreshes().finally(() => { this.running = undefined; });
    return this.running;
  }

  private async drainRefreshes(): Promise<void> {
    do {
      this.again = false;
      await this.doRefresh();
    } while (this.again && !this.disposed);
  }

  private clearPreview(): void {
    this.status = this.preview = undefined;
    this.diagnostics.clear();
    this.decorations.refresh();
  }

  private async doRefresh(): Promise<void> {
    const hasCovenant = fs.existsSync(this.covenantPath);
    void vscode.commands.executeCommand("setContext", "covenant.hasCovenant", hasCovenant);
    if (!hasCovenant) {
      this.clearPreview();
      this.watchState(undefined);
      this.tree.update(undefined, undefined);
      this.statusBar.text = "$(shield) Covenant: set up";
      this.statusBar.tooltip = "No covenant.yaml. Click to compile rules from an instruction.";
      this.statusBar.command = "covenant.compile";
      this.statusBar.show();
      return;
    }
    this.statusBar.command = "covenant.rules.focus";
    try {
      const status = await this.cli.json<Status>(["status", "--format", "json"]);
      if (this.disposed) return;
      const preview = status.has_session ? await this.cli.json<Decision>(["check", "--preview"]) : undefined;
      if (this.disposed) return;
      if (!fs.existsSync(this.covenantPath)) {
        this.again = true;
        return;
      }
      this.status = status;
      this.preview = preview;
      this.warnedNotInstalled = false;
      this.tree.update(this.status, this.preview);
      this.watchState(this.status.state_dir);
    } catch (err) {
      if (this.disposed) return;
      const text = err instanceof Error ? err.message : String(err);
      this.clearPreview();
      this.tree.update(undefined, undefined, text);
      this.statusBar.text = "$(error) Covenant: error";
      this.statusBar.tooltip = text;
      this.statusBar.show();
      if (err instanceof CovenantCliError && err.notInstalled && !this.warnedNotInstalled) {
        this.warnedNotInstalled = true;
        void vscode.window
          .showErrorMessage("Covenant isn't installed for the configured Python.", "Select Python")
          .then((pick) => pick && vscode.commands.executeCommand("covenant.selectPython"));
      }
      output.appendLine(text);
      return;
    }
    this.render();
  }

  private render(): void {
    const status = this.status!;
    const reports = this.preview?.reports ?? [];
    const files = fileAccess(this.repo);
    const byFile = new Map<string, vscode.Diagnostic[]>();
    for (const report of reports) {
      const place = this.reportLocation(report, files);
      const severity = isReminder(report) ? vscode.DiagnosticSeverity.Warning : vscode.DiagnosticSeverity.Error;
      const diag = new vscode.Diagnostic(new vscode.Range(place.line, 0, place.line, 1000), message(report), severity);
      diag.source = "Covenant";
      diag.code = report.violation;
      const list = byFile.get(place.file) ?? [];
      list.push(diag);
      byFile.set(place.file, list);
    }
    this.diagnostics.clear();
    for (const [file, diags] of byFile) this.diagnostics.set(vscode.Uri.file(path.join(this.repo, file)), diags);
    this.decorations.refresh();

    const problems = reports.filter((r) => !isReminder(r));
    const reminders = reports.filter(isReminder);
    const enforced = status.clauses.filter((c) => (c.status ?? "enforced") === "enforced").length;
    if (!status.has_session) {
      this.statusBar.text = "$(debug-pause) Covenant: not started";
      this.statusBar.tooltip = "Approve the rules to start enforcing them.";
    } else if (problems.length) {
      this.statusBar.text = `$(error) Covenant: ${problems.length} issue${problems.length > 1 ? "s" : ""}`;
      this.statusBar.tooltip = problems.map(message).join("\n");
    } else if (status.repo_covenant_changed) {
      this.statusBar.text = "$(warning) Covenant: approve changed rules";
      this.statusBar.tooltip = "covenant.yaml changed since approval; the approved version still applies.";
    } else if (reminders.length) {
      this.statusBar.text = "$(clock) Covenant: tests needed before commit";
      this.statusBar.tooltip = reminders.map(message).join("\n");
    } else {
      this.statusBar.text = `$(pass) Covenant: ${enforced} rules hold`;
      this.statusBar.tooltip = "Every enforced rule holds right now.";
    }
    this.statusBar.show();
  }

  private watchState(stateDir: string | undefined): void {
    if (this.watchedStateDir === stateDir) return;
    this.stateWatcher?.dispose();
    this.stateWatcher = undefined;
    this.watchedStateDir = stateDir;
    if (!stateDir) return;
    const pattern = new vscode.RelativePattern(vscode.Uri.file(stateDir), "*.{jsonl,json}");
    this.stateWatcher = vscode.workspace.createFileSystemWatcher(pattern);
    const onChange = () => void this.refresh();
    this.stateWatcher.onDidChange(onChange);
    this.stateWatcher.onDidCreate(onChange);
    this.stateWatcher.onDidDelete(onChange);
  }

  async openRule(id: string): Promise<void> {
    const doc = await vscode.workspace.openTextDocument(this.covenantPath);
    const line = clauseLine(doc.getText(), id);
    await vscode.window.showTextDocument(doc, { selection: new vscode.Range(line, 0, line, 0) });
  }

  private reportLocation(report: Report, files: FileAccess) {
    const place = locate(report, files, "covenant.yaml");
    if (workspaceRelative(this.repo, path.resolve(this.repo, place.file))) return place;
    return { file: "covenant.yaml", line: clauseLine(files.read("covenant.yaml") ?? "", report.violation) };
  }

  async openViolation(report: Report): Promise<void> {
    const place = this.reportLocation(report, fileAccess(this.repo));
    const uri = vscode.Uri.file(path.join(this.repo, place.file));
    if (!fs.existsSync(uri.fsPath)) {
      void vscode.window.showInformationMessage(message(report));
      return;
    }
    const doc = await vscode.workspace.openTextDocument(uri);
    await vscode.window.showTextDocument(doc, { selection: new vscode.Range(place.line, 0, place.line, 0) });
  }

  async runLogged(title: string, args: string[]): Promise<number> {
    return vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title }, async () => {
      output.appendLine(`\n${this.folder.name}: covenant ${args[0]}`);
      const result = await this.cli.run(args, 900_000);
      output.append(result.stderr);
      output.append(result.stdout);
      return result.code;
    });
  }
}

async function compileCommand(ctrl: Controller): Promise<void> {
  const instruction = await vscode.window.showInputBox({
    title: "Covenant: compile rules",
    prompt: "The instruction you would give the coding agent",
    placeHolder: "Update dependencies, but don't change public APIs, don't delete user data, and run tests before committing.",
    ignoreFocusOut: true,
  });
  if (!instruction || !ctrl.isOpen) return;
  const compiler = setting(ctrl.folder, "offlineCompile", false) ? ["--offline"] : [];
  const base = ["compile", instruction, "--unattended", "--format", "json", ...compiler];

  const draft = async (answers: string[]) =>
    vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: "Covenant: compiling…" }, () =>
      ctrl.cli.json<CompileResult>([...base, "--dry-run", ...answers.flatMap((a) => ["--answer", a])]),
    );

  let result: CompileResult;
  try {
    result = await draft([]);
  } catch (err) {
    void vscode.window.showErrorMessage(err instanceof Error ? err.message : String(err));
    return;
  }

  // Ask the clarification questions the compiler found (highest impact first, at most three).
  const answers: string[] = [];
  for (const q of result.questions.slice(0, 3)) {
    const items = q.options.map((label, i) => ({ label, description: i === q.default ? "safer default" : undefined, index: i }));
    const pick = await vscode.window.showQuickPick(items, { title: `${q.id}: ${q.text}`, ignoreFocusOut: true });
    if (!pick) return;
    if (pick.index !== q.default) answers.push(`${q.id}=${pick.index + 1}`);
  }
  if (answers.length) result = await draft(answers);

  const errors = result.issues.filter((i) => i.level === "error");
  const header = [
    "# Draft Covenant (not written yet).",
    ...result.issues.map((i) => `# [${i.level}] ${i.clause}: ${i.message}`),
    "",
  ].join("\n");
  const doc = await vscode.workspace.openTextDocument({ language: "yaml", content: header + result.yaml });
  await vscode.window.showTextDocument(doc, { preview: true });
  if (errors.length) {
    void vscode.window.showErrorMessage(`The draft has ${errors.length} error(s); see the comments at the top.`);
    return;
  }
  const exists = fs.existsSync(ctrl.covenantPath);
  const choice = await vscode.window.showInformationMessage(
    exists ? "Replace covenant.yaml with this draft?" : "Write this draft to covenant.yaml?",
    { modal: true },
    "Write",
  );
  if (choice !== "Write" || !ctrl.isOpen) return;
  const code = await ctrl.runLogged("Covenant: writing covenant.yaml…", [
    "compile", instruction, "--unattended", "--yes", ...compiler,
    ...answers.flatMap((a) => ["--answer", a]),
  ]);
  if (code !== 0) {
    void vscode.window.showErrorMessage("Covenant couldn't write covenant.yaml; see the Covenant output.");
    output.show();
    return;
  }
  void vscode.window.showInformationMessage("Covenant rules saved to covenant.yaml.");

}

async function amendCommand(ctrl: Controller): Promise<void> {
  if (!fs.existsSync(ctrl.covenantPath)) {
    void vscode.window.showWarningMessage("Create covenant.yaml before adding a rule.");
    return;
  }
  const instruction = await vscode.window.showInputBox({
    title: "Covenant: add a rule to the current task",
    prompt: "An extra instruction; the rules it adds apply from now on",
    placeHolder: "Also don't touch config/",
    ignoreFocusOut: true,
  });
  if (!instruction || !ctrl.isOpen) return;
  const base = ["amend", instruction, "--unattended", ...(setting(ctrl.folder, "offlineCompile", false) ? ["--offline"] : [])];
  const draft = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "Covenant: compiling…" },
    () => ctrl.cli.run([...base, "--dry-run"], 900_000),
  );
  output.append(draft.stderr);
  if (draft.code !== 0) {
    void vscode.window.showErrorMessage("Covenant couldn't compile that instruction; see the Covenant output.");
    output.show();
    return;
  }
  const rules = draft.stderr.slice(draft.stderr.indexOf("New rules:"));
  const choice = await vscode.window.showInformationMessage(
    "Add these rules to covenant.yaml and approve them?",
    { modal: true, detail: rules },
    "Add",
  );
  if (choice !== "Add" || !ctrl.isOpen) return;
  const code = await ctrl.runLogged("Covenant: adding rules…", [...base, "--yes"]);
  if (code !== 0) {
    void vscode.window.showErrorMessage("Covenant couldn't add the rules; see the Covenant output.");
    output.show();
  }
  await ctrl.refresh();
}

/** What `activate` returns: read-only state, for tests and other extensions. */
export interface CovenantApi {
  refresh(): Promise<void>;
  state(): { status?: Status; preview?: Decision; statusBar: string; protectedBy(relPath: string): string | undefined };
}

export function activate(context: vscode.ExtensionContext): CovenantApi {
  output = vscode.window.createOutputChannel("Covenant");
  const tree = new RulesTree();
  let controller: Controller | undefined;
  const rebind = () => {
    const folder = vscode.workspace.isTrusted ? findFolder() : undefined;
    if (folder && folder.uri.toString() === controller?.folder.uri.toString()) return;
    controller?.dispose();
    controller = undefined;
    if (!folder) {
      void vscode.commands.executeCommand("setContext", "covenant.hasCovenant", false);
      tree.update(undefined, undefined, vscode.workspace.isTrusted
        ? "Open a local repository folder to use Covenant."
        : "Trust this workspace to run Covenant.");
      return;
    }
    tree.update(undefined, undefined);
    controller = new Controller(folder, tree);
    void controller.refresh();
  };

  function register<A extends unknown[]>(id: string, run: (ctrl: Controller, ...args: A) => unknown): vscode.Disposable {
    return vscode.commands.registerCommand(id, (...args: A) => {
      if (controller) return run(controller, ...args);
      return vscode.window.showInformationMessage(vscode.workspace.isTrusted
        ? "Open a local repository folder to use Covenant."
        : "Trust this workspace to run Covenant.");
    });
  }

  context.subscriptions.push(
    output, tree,
    { dispose: () => controller?.dispose() },
    vscode.workspace.onDidChangeWorkspaceFolders(rebind),
    vscode.workspace.onDidGrantWorkspaceTrust(rebind),
    vscode.window.registerTreeDataProvider("covenant.rules", tree),
    register("covenant.refresh", (ctrl) => ctrl.refresh()),
    register("covenant.openRule", (ctrl, id: string) => ctrl.openRule(id)),
    register("covenant.openViolation", (ctrl, report: Report) => ctrl.openViolation(report)),
    register("covenant.compile", (ctrl) => ctrl.act("Draft rules", () => compileCommand(ctrl))),
    register("covenant.amend", (ctrl) => ctrl.act("Add a rule", () => amendCommand(ctrl))),
    register("covenant.selectPython", (ctrl) => selectPython(ctrl.folder)),
    vscode.commands.registerCommand("covenant.showLog", () => output.show()),
    register("covenant.runTests", (ctrl) => ctrl.act("Run tests", async () => {
      const code = await ctrl.runLogged("Covenant: running tests…", ["test"]);
      if (code === 0) {
        void vscode.window.showInformationMessage("Covenant recorded a passing test run. Use Check Now to review the remaining rules.");
      } else {
        void ctrl.failure("The test run did not complete successfully.");
      }
    })),
    register("covenant.approve", (ctrl) => ctrl.act("Start task", async () => {
      const choice = await vscode.window.showWarningMessage(
        "Approve covenant.yaml as it is now and start a new task?",
        { modal: true, detail: "This pins the current rules and baseline commit, snapshots protected data, and clears this task's event history." },
        "Approve",
      );
      if (choice !== "Approve" || !ctrl.isOpen) return;
      const code = await ctrl.runLogged("Covenant: approving…", ["start"]);
      if (code === 0) void vscode.window.showInformationMessage("Covenant task started.");
      else void ctrl.failure("The task could not start.");
    })),
    register("covenant.init", (ctrl) => ctrl.act("Set up repository", async () => {
      const code = await ctrl.runLogged("Covenant: setting up hooks…", ["init"]);
      if (code !== 0) void ctrl.failure("Repository setup failed.");
      else void vscode.window.showInformationMessage("Covenant task started. See the output for hook setup details.");
    })),
  );

  rebind();
  return {
    refresh: () => controller?.refresh() ?? Promise.resolve(),
    state: () => ({
      status: controller?.status,
      preview: controller?.preview,
      statusBar: controller?.statusBar.text ?? "",
      protectedBy: (relPath: string) => controller?.status && protectingClause(relPath, controller.status.protected),
    }),
  };
}

export function deactivate(): void {}
