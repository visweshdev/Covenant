import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { CovenantCli, CovenantCliError } from "./cli";
import { protectingClause } from "./glob";
import { clauseLine, FileAccess, isReminder, locate, message } from "./locate";
import { RulesTree } from "./tree";
import type { CompileResult, Decision, Report, Status } from "./types";

let output: vscode.OutputChannel;

function config<T>(key: string, fallback: T): T {
  return vscode.workspace.getConfiguration("covenant").get<T>(key, fallback);
}

function findRepo(): string | undefined {
  const folders = vscode.workspace.workspaceFolders ?? [];
  const withCovenant = folders.find((f) => fs.existsSync(path.join(f.uri.fsPath, "covenant.yaml")));
  return (withCovenant ?? folders[0])?.uri.fsPath;
}

function rel(repo: string, file: string): string {
  return path.relative(repo, file).split(path.sep).join("/");
}

function fileAccess(repo: string): FileAccess {
  return {
    read(r) {
      try {
        return fs.readFileSync(path.join(repo, r), "utf8");
      } catch {
        return undefined;
      }
    },
    list(dir, ext) {
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

  provideFileDecoration(uri: vscode.Uri): vscode.FileDecoration | undefined {
    const status = this.status();
    if (!status || uri.scheme !== "file") return undefined;
    const r = rel(this.repo, uri.fsPath);
    if (r.startsWith("..")) return undefined;
    const clause = protectingClause(r, status.protected);
    if (!clause) return undefined;
    const span = status.clauses.find((c) => c.id === clause)?.source_span ?? "";
    return new vscode.FileDecoration("🔒", `Protected by ${clause} (${span}): agents can't delete or overwrite it`);
  }
}

class Controller {
  readonly cli: CovenantCli;
  readonly tree = new RulesTree();
  readonly diagnostics = vscode.languages.createDiagnosticCollection("covenant");
  readonly statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  readonly decorations: ProtectedFiles;
  status?: Status;
  preview?: Decision;
  private running?: Promise<void>;
  private again = false;
  private stateWatcher?: vscode.FileSystemWatcher;
  private warnedNotInstalled = false;

  constructor(readonly repo: string, readonly context: vscode.ExtensionContext) {
    this.cli = new CovenantCli(repo, () => config("pythonPath", "python"));
    this.decorations = new ProtectedFiles(repo, () => this.status);
    this.statusBar.command = "covenant.rules.focus";
    context.subscriptions.push(this.diagnostics, this.statusBar);
  }

  get covenantPath(): string {
    return path.join(this.repo, "covenant.yaml");
  }

  /** Re-check; calls made while a check is running coalesce into one more run. */
  refresh(): Promise<void> {
    if (this.running) {
      this.again = true;
      return this.running;
    }
    this.running = this.doRefresh().finally(() => {
      this.running = undefined;
      if (this.again) {
        this.again = false;
        void this.refresh();
      }
    });
    return this.running;
  }

  private async doRefresh(): Promise<void> {
    const hasCovenant = fs.existsSync(this.covenantPath);
    void vscode.commands.executeCommand("setContext", "covenant.hasCovenant", hasCovenant);
    if (!hasCovenant) {
      this.status = this.preview = undefined;
      this.tree.update(undefined, undefined);
      this.diagnostics.clear();
      this.statusBar.text = "$(shield) Covenant: set up";
      this.statusBar.tooltip = "No covenant.yaml. Click to compile rules from an instruction.";
      this.statusBar.command = "covenant.compile";
      this.statusBar.show();
      return;
    }
    this.statusBar.command = "covenant.rules.focus";
    try {
      this.status = await this.cli.json<Status>(["status", "--format", "json"]);
      this.preview = this.status.has_session ? await this.cli.json<Decision>(["check", "--preview"]) : undefined;
      this.tree.update(this.status, this.preview);
      this.watchState(this.status.state_dir);
    } catch (err) {
      const text = err instanceof Error ? err.message : String(err);
      this.tree.update(undefined, undefined, text);
      this.statusBar.text = "$(error) Covenant: error";
      this.statusBar.tooltip = text;
      this.statusBar.show();
      if (err instanceof CovenantCliError && err.notInstalled && !this.warnedNotInstalled) {
        this.warnedNotInstalled = true;
        void vscode.window
          .showErrorMessage("Covenant isn't installed for the configured Python.", "Set Python Path")
          .then((pick) => pick && vscode.commands.executeCommand("workbench.action.openSettings", "covenant.pythonPath"));
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
      const place = locate(report, files, "covenant.yaml");
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

  private watchState(stateDir: string): void {
    if (this.stateWatcher) return;
    const pattern = new vscode.RelativePattern(vscode.Uri.file(stateDir), "*.{jsonl,json}");
    this.stateWatcher = vscode.workspace.createFileSystemWatcher(pattern);
    const onChange = () => void this.refresh();
    this.stateWatcher.onDidChange(onChange);
    this.stateWatcher.onDidCreate(onChange);
    this.context.subscriptions.push(this.stateWatcher);
  }

  async openRule(id: string): Promise<void> {
    const doc = await vscode.workspace.openTextDocument(this.covenantPath);
    const line = clauseLine(doc.getText(), id);
    await vscode.window.showTextDocument(doc, { selection: new vscode.Range(line, 0, line, 0) });
  }

  async openViolation(report: Report): Promise<void> {
    const place = locate(report, fileAccess(this.repo), "covenant.yaml");
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
      output.appendLine(`\n$ covenant ${args.join(" ")}`);
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
  if (!instruction) return;
  const base = ["compile", instruction, "--unattended", "--format", "json"];
  if (config("offlineCompile", false)) base.push("--offline");

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
  if (choice !== "Write") return;
  const code = await ctrl.runLogged("Covenant: writing covenant.yaml…", [
    "compile", instruction, "--unattended", "--yes", ...(config("offlineCompile", false) ? ["--offline"] : []),
    ...answers.flatMap((a) => ["--answer", a]),
  ]);
  if (code !== 0) {
    void vscode.window.showErrorMessage("Covenant couldn't write covenant.yaml; see the Covenant output.");
    output.show();
    return;
  }
  const next = await vscode.window.showInformationMessage(
    "covenant.yaml written. Install the git and Claude Code hooks and start enforcing?",
    "Set Up",
  );
  if (next === "Set Up") await vscode.commands.executeCommand("covenant.init");
  await ctrl.refresh();
}

async function amendCommand(ctrl: Controller): Promise<void> {
  if (!fs.existsSync(ctrl.covenantPath)) {
    void vscode.window.showWarningMessage("There's no covenant.yaml yet; compile one first.", "Compile").then(
      (c) => c && vscode.commands.executeCommand("covenant.compile"),
    );
    return;
  }
  const instruction = await vscode.window.showInputBox({
    title: "Covenant: add a rule to the current task",
    prompt: "An extra instruction; the rules it adds apply from now on",
    placeHolder: "Also don't touch config/",
    ignoreFocusOut: true,
  });
  if (!instruction) return;
  const base = ["amend", instruction, "--unattended", ...(config("offlineCompile", false) ? ["--offline"] : [])];
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
  if (choice !== "Add") return;
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

export function activate(context: vscode.ExtensionContext): CovenantApi | undefined {
  output = vscode.window.createOutputChannel("Covenant");
  context.subscriptions.push(output);
  const repo = findRepo();
  if (!repo) return undefined;
  const ctrl = new Controller(repo, context);

  context.subscriptions.push(
    vscode.window.registerTreeDataProvider("covenant.rules", ctrl.tree),
    vscode.window.registerFileDecorationProvider(ctrl.decorations),
    vscode.commands.registerCommand("covenant.refresh", () => ctrl.refresh()),
    vscode.commands.registerCommand("covenant.openRule", (id: string) => ctrl.openRule(id)),
    vscode.commands.registerCommand("covenant.openViolation", (r: Report) => ctrl.openViolation(r)),
    vscode.commands.registerCommand("covenant.compile", () => compileCommand(ctrl)),
    vscode.commands.registerCommand("covenant.amend", () => amendCommand(ctrl)),
    vscode.commands.registerCommand("covenant.showLog", () => output.show()),
    vscode.commands.registerCommand("covenant.runTests", async () => {
      const code = await ctrl.runLogged("Covenant: running tests…", ["test"]);
      void (code === 0
        ? vscode.window.showInformationMessage("Tests passed and were recorded; a commit can follow.")
        : vscode.window.showWarningMessage("Tests failed or a rule was reported; see the Covenant output.", "Show").then((p) => p && output.show()));
      await ctrl.refresh();
    }),
    vscode.commands.registerCommand("covenant.approve", async () => {
      const choice = await vscode.window.showWarningMessage(
        "Approve covenant.yaml as it is now and start a new task?",
        { modal: true, detail: "This pins the current rules and baseline commit, snapshots protected data, and clears this task's event history." },
        "Approve",
      );
      if (choice !== "Approve") return;
      await ctrl.runLogged("Covenant: approving…", ["start"]);
      await ctrl.refresh();
    }),
    vscode.commands.registerCommand("covenant.init", async () => {
      const code = await ctrl.runLogged("Covenant: setting up hooks…", ["init"]);
      if (code !== 0) {
        void vscode.window.showErrorMessage("Covenant setup failed; see the Covenant output.");
        output.show();
      } else {
        void vscode.window.showInformationMessage("Covenant is enforcing this repo: git hooks and Claude Code hooks installed.");
      }
      await ctrl.refresh();
    }),
  );

  const covenantWatcher = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(repo, "covenant.yaml"));
  covenantWatcher.onDidChange(() => void ctrl.refresh());
  covenantWatcher.onDidCreate(() => void ctrl.refresh());
  covenantWatcher.onDidDelete(() => void ctrl.refresh());
  context.subscriptions.push(covenantWatcher);

  let timer: NodeJS.Timeout | undefined;
  context.subscriptions.push(
    vscode.workspace.onDidSaveTextDocument((doc) => {
      if (!config("checkOnSave", true) || !doc.uri.fsPath.startsWith(repo)) return;
      clearTimeout(timer);
      timer = setTimeout(() => void ctrl.refresh(), 1200);
    }),
    vscode.workspace.onDidChangeConfiguration((e) => e.affectsConfiguration("covenant") && void ctrl.refresh()),
    { dispose: () => clearTimeout(timer) },
  );

  void ctrl.refresh();
  return {
    refresh: () => ctrl.refresh(),
    state: () => ({
      status: ctrl.status,
      preview: ctrl.preview,
      statusBar: ctrl.statusBar.text,
      protectedBy: (relPath: string) => (ctrl.status ? protectingClause(relPath, ctrl.status.protected) : undefined),
    }),
  };
}

export function deactivate(): void {}
