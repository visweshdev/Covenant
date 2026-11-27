// Runs inside the VS Code extension host. Checks what a user would see, through the real API.
import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import * as path from "path";
import * as vscode from "vscode";
import { CovenantCli } from "../src/cli";
import type { CovenantApi } from "../src/extension";
import { starters } from "../src/prompts";
import type { CompileResult, Status } from "../src/types";

async function waitFor<T>(what: string, probe: () => T | undefined, timeoutMs = 20_000): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = probe();
    if (value !== undefined) return value;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 500));
  }
}

async function verifyExtension(): Promise<void> {
  const repo = vscode.workspace.workspaceFolders![0].uri.fsPath;
  const python = vscode.workspace.getConfiguration("covenant").get("pythonPath", "python");
  const cli = new CovenantCli(repo, () => python);
  const status = await cli.json<Status>(["status", "--format", "json"]);
  assert.ok(status.has_session, `No active Covenant session in ${status.state_dir}`);
  const ext = vscode.extensions.getExtension<CovenantApi>("covenant.covenant");
  assert.ok(ext, "extension not found");
  const api = (await ext.activate())!;
  assert.ok(api, "extension did not activate on a repo with covenant.yaml");

  const commands = await vscode.commands.getCommands(true);
  for (const c of [
    "covenant.refresh", "covenant.compile", "covenant.approve", "covenant.runTests", "covenant.init", "covenant.amend",
    "covenant.openPolicy", "covenant.draftStarter", "covenant.copyStarter", "covenant.selectPython", "covenant.selectWorkspace",
    "covenant.copyMcp", "covenant.copyHandoff", "covenant.openDraft", "covenant.saveDraft", "covenant.discardDraft",
  ]) {
    assert.ok(commands.includes(c), `command ${c} missing`);
  }

  await api.refresh();
  const state = await waitFor("a preview", () => (api.state().preview ? api.state() : undefined));
  const ids = [...new Set(state.preview!.reports.map((r) => r.violation))].sort();
  console.log("status bar:", state.statusBar);
  console.log("violations:", ids.join(", "));
  assert.deepEqual(ids, ["C1", "C2", "C3"]);
  assert.match(state.statusBar, /2 issues/);

  const diag = (rel: string) => vscode.languages.getDiagnostics(vscode.Uri.file(path.join(repo, rel)));
  const core = await waitFor("diagnostics on mylib/core.py", () => (diag("mylib/core.py").length ? diag("mylib/core.py") : undefined));
  console.log("mylib/core.py:", core.map((d) => `line ${d.range.start.line + 1}: ${d.message}`).join(" | "));
  assert.equal(core[0].source, "Covenant");
  assert.equal(core[0].severity, vscode.DiagnosticSeverity.Error);
  assert.match(core[0].message, /^C1 .*mylib\.parse changed/);

  const data = diag("data/users.json");
  console.log("data/users.json:", data.map((d) => d.message).join(" | "));
  assert.match(data[0].message, /^C2 /);
  const yaml = diag("covenant.yaml");
  console.log("covenant.yaml:", yaml.map((d) => `line ${d.range.start.line + 1}: ${d.message}`).join(" | "));
  assert.equal(yaml[0].severity, vscode.DiagnosticSeverity.Warning);
  assert.match(yaml[0].message, /^C3 /);

  assert.equal(api.state().protectedBy("data/users.json"), "C2");
  assert.equal(api.state().protectedBy("mylib/core.py"), undefined);

  // Refreshing must leave protected files as they were.
  const usersBefore = (await vscode.workspace.fs.readFile(vscode.Uri.file(path.join(repo, "data", "users.json")))).toString();
  const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(path.join(repo, "README.md")).with({ scheme: "untitled" }));
  await vscode.window.showTextDocument(doc);
  await api.refresh();
  const usersAfter = (await vscode.workspace.fs.readFile(vscode.Uri.file(path.join(repo, "data", "users.json")))).toString();
  assert.equal(usersAfter, usersBefore);

  await vscode.commands.executeCommand("covenant.openRule", "C3");
  assert.equal(vscode.window.activeTextEditor?.document.fileName, path.join(repo, "covenant.yaml"));
  await vscode.commands.executeCommand("covenant.openViolation", state.preview!.reports.find((report) => report.violation === "C1"));
  assert.equal(vscode.window.activeTextEditor?.document.fileName, path.join(repo, "mylib", "core.py"));
  await vscode.commands.executeCommand("covenant.rules.focus");

  if (process.env.COVENANT_E2E_FIXTURE === "1") {
    await verifyStarters(cli);
    // A missing policy must clear violations, badges, and the previous task state.
    const from = vscode.Uri.file(path.join(repo, "covenant.yaml"));
    const to = vscode.Uri.file(path.join(repo, "covenant-test-backup.yaml"));
    await vscode.workspace.fs.rename(from, to);
    try {
      await api.refresh();
      assert.equal(api.state().status, undefined);
      assert.equal(api.state().protectedBy("data/users.json"), undefined);
      assert.equal(diag("mylib/core.py").length, 0);
      assert.match(api.state().statusBar, /set up/i);
    } finally {
      await vscode.workspace.fs.rename(to, from);
    }
    await api.refresh();
    assert.equal(api.state().status?.has_session, true);
  }
}

async function verifyStarters(cli: CovenantCli): Promise<void> {
  const expected = [
    ["api_surface_diff", "trace_monitor"],
    ["test_guard", "trace_monitor"],
    ["dependency_diff", "no_debug_code", "trace_monitor"],
    ["effect_monitor", "secret_scan"],
  ];
  for (const [index, starter] of starters.entries()) {
    const draft = await cli.json<CompileResult>(["compile", "--offline", "--unattended", "--format", "json", "--dry-run", "--", starter.instruction], 30_000, 3);
    assert.ok(draft.issues.every((issue) => issue.level !== "error"), JSON.stringify(draft.issues));
    const checkers = draft.covenant.clauses.map((clause) => clause.checker);
    for (const checker of expected[index]) assert.ok(checkers.includes(checker), `${starter.id}: expected ${checker}, got ${checkers}`);
  }
}

export async function run(): Promise<void> {
  const resultFile = process.env.COVENANT_E2E_RESULT;
  try {
    await verifyExtension();
    if (resultFile) await writeFile(resultFile, "passed\n");
    console.log("E2E OK");
  } catch (error) {
    if (resultFile) await writeFile(resultFile, error instanceof Error ? error.stack ?? error.message : String(error));
    throw error;
  }
}
