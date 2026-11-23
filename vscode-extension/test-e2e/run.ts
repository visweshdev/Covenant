// Launches a real VS Code with this extension (development copy) on a repo and runs suite.ts inside it.
// With no arguments this builds a scratch fixture and uses the oldest supported VS Code minor.
import { execFileSync } from "node:child_process";
import * as fs from "node:fs/promises";
import * as os from "os";
import * as path from "path";
import { downloadAndUnzipVSCode, runTests } from "@vscode/test-electron";

const extension = path.resolve(__dirname, "..", "..");
const vscodeVersion = "1.100.3";

async function makeFixture(repo: string, python: string, env: NodeJS.ProcessEnv): Promise<void> {
  await fs.cp(path.join(extension, "..", "examples", "mylib"), repo, { recursive: true });
  const git = (...args: string[]) => execFileSync("git", ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", ...args], { cwd: repo, env });
  git("init", "-q");
  git("add", ".");
  git("commit", "-qm", "baseline");
  execFileSync(python, ["-m", "covenant", "start"], { cwd: repo, env });
  const core = path.join(repo, "mylib", "core.py");
  const source = await fs.readFile(core, "utf8");
  await fs.writeFile(core, source.replace("strict: bool = False) -> Doc:", "strict: bool) -> Doc:"));
  await fs.writeFile(path.join(repo, "data", "users.json"), "[]\n");
}

async function main(): Promise<void> {
  const [codeExe, givenRepo] = process.argv.slice(2);
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), "covenant-e2e-"));
  const resultFile = path.join(temp, "suite-result");
  try {
    const repo = givenRepo ? path.resolve(givenRepo) : path.join(temp, "repo");
    const state = givenRepo ? {} : { COVENANT_HOME: path.join(temp, "state"), COVENANT_SNAPSHOTS: "1" };
    const python = givenRepo ? undefined : execFileSync("python", ["-c", "import sys; print(sys.executable)"], { encoding: "utf8" }).trim();
    if (python) await makeFixture(repo, python, { ...process.env, ...state });
    const executable = codeExe ? path.resolve(codeExe) : await downloadAndUnzipVSCode(vscodeVersion);
    const tests = path.resolve(__dirname, "suite.js");
    const testEnv = { ...state, COVENANT_E2E_RESULT: resultFile };
    const profile = path.join(temp, "profile");
    await fs.mkdir(path.join(profile, "User"), { recursive: true });
    await fs.writeFile(path.join(profile, "User", "settings.json"), JSON.stringify({
      "update.mode": "none", "extensions.autoCheckUpdates": false, "security.workspace.trust.enabled": false,
      "covenant.pythonPath": python,
    }));
    const launchArgs = [repo, "--disable-extensions", "--disable-gpu", "--user-data-dir", profile,
      "--extensions-dir", path.join(temp, "extensions"), "--skip-welcome", "--skip-release-notes"];
    if (process.platform === "win32") {
      // test-electron 2.4.1 starts cmd.exe on Windows, which misquotes executable paths with spaces.
      execFileSync(executable, [...launchArgs, `--extensionDevelopmentPath=${extension}`, `--extensionTestsPath=${tests}`], {
        env: { ...process.env, ...testEnv }, stdio: "inherit", timeout: 180_000,
      });
    } else await runTests({
      vscodeExecutablePath: executable,
      extensionDevelopmentPath: extension,
      extensionTestsPath: tests,
      extensionTestsEnv: testEnv,
      launchArgs,
    });
    if ((await fs.readFile(resultFile, "utf8").catch(() => undefined)) !== "passed\n") {
      throw new Error("VS Code exited before the Covenant test suite completed");
    }
  } catch (error) {
    const failure = await fs.readFile(resultFile, "utf8").catch(() => undefined);
    if (failure && failure !== "passed\n") throw new Error(failure.trim(), { cause: error });
    throw error;
  } finally {
    await fs.rm(temp, { recursive: true, force: true, maxRetries: 8, retryDelay: 250 });
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
