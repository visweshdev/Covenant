// Launches a real VS Code with this extension (development copy) on a repo and runs suite.ts inside it.
// Usage: node out/test-e2e/run.js <path to Code executable> <repo with covenant.yaml>
import * as os from "os";
import * as path from "path";
import { runTests } from "@vscode/test-electron";

async function main(): Promise<void> {
  const [codeExe, repo] = process.argv.slice(2);
  if (!codeExe || !repo) throw new Error("usage: run.js <Code executable> <repo>");
  const userData = path.join(os.tmpdir(), `covenant-e2e-${process.pid}`);
  await runTests({
    vscodeExecutablePath: codeExe,
    extensionDevelopmentPath: path.resolve(__dirname, "..", ".."),
    extensionTestsPath: path.resolve(__dirname, "suite.js"),
    launchArgs: [repo, "--disable-extensions", "--user-data-dir", userData, "--skip-welcome", "--skip-release-notes"],
  });
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
