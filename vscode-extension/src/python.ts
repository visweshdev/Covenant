import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";

export function setting<T>(folder: vscode.WorkspaceFolder, key: string, fallback: T): T {
  return vscode.workspace.getConfiguration("covenant", folder.uri).get(key, fallback);
}

export async function selectPython(folder: vscode.WorkspaceFolder): Promise<void> {
  const config = vscode.workspace.getConfiguration("covenant", folder.uri);
  const current = config.get("pythonPath", "python");
  const environment = process.platform === "win32" ? ["Scripts", "python.exe"] : ["bin", "python"];
  const candidates = [".venv", "venv"]
    .map((directory) => path.join(folder.uri.fsPath, directory, ...environment))
    .filter((candidate) => fs.existsSync(candidate) && candidate !== current);
  const choice = await vscode.window.showQuickPick(
    [
      { label: "Current interpreter", description: current, value: current },
      ...candidates.map((candidate) => ({ label: "Workspace environment", description: candidate, value: candidate })),
      { label: "Browse for Python…", description: "", value: undefined },
    ],
    { title: "Covenant: Select Python", placeHolder: "Choose a Python environment that has Covenant installed" },
  );
  if (!choice) return;
  const interpreter = choice.value ?? (await vscode.window.showOpenDialog({
    title: "Select Python executable",
    defaultUri: folder.uri,
    openLabel: "Select Python",
    canSelectMany: false,
  }))?.[0]?.fsPath;
  if (interpreter && interpreter !== current) {
    await config.update("pythonPath", interpreter, vscode.ConfigurationTarget.WorkspaceFolder);
  }
}
