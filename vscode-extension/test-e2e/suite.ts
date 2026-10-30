// Runs inside the VS Code extension host. Checks what a user would see, through the real API.
import assert from "node:assert/strict";
import * as path from "path";
import * as vscode from "vscode";
import type { CovenantApi } from "../src/extension";

async function waitFor<T>(what: string, probe: () => T | undefined, timeoutMs = 90_000): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = probe();
    if (value !== undefined) return value;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 500));
  }
}

export async function run(): Promise<void> {
  const repo = vscode.workspace.workspaceFolders![0].uri.fsPath;
  const ext = vscode.extensions.getExtension<CovenantApi>("covenant.covenant");
  assert.ok(ext, "extension not found");
  const api = (await ext.activate())!;
  assert.ok(api, "extension did not activate on a repo with covenant.yaml");

  const commands = await vscode.commands.getCommands(true);
  for (const c of ["covenant.refresh", "covenant.compile", "covenant.approve", "covenant.runTests", "covenant.init", "covenant.amend"]) {
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

  // Saving must never change protected files: the editor's check is a preview.
  const usersBefore = (await vscode.workspace.fs.readFile(vscode.Uri.file(path.join(repo, "data", "users.json")))).toString();
  const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(path.join(repo, "README.md")).with({ scheme: "untitled" }));
  await vscode.window.showTextDocument(doc);
  await api.refresh();
  const usersAfter = (await vscode.workspace.fs.readFile(vscode.Uri.file(path.join(repo, "data", "users.json")))).toString();
  assert.equal(usersAfter, usersBefore);

  // The Rules view opens.
  await vscode.commands.executeCommand("covenant.rules.focus");
  console.log("E2E OK");
}
