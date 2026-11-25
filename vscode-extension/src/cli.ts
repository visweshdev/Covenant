import { execFile } from "child_process";

export interface CliResult {
  code: number;
  stdout: string;
  stderr: string;
  missingExecutable?: boolean;
}

export class CovenantCli {
  constructor(
    private readonly repo: string,
    private readonly python: () => string,
  ) {}

  run(args: string[], timeoutMs = 300_000): Promise<CliResult> {
    return new Promise((resolve) => {
      execFile(
        this.python(),
        ["-m", "covenant", "--repo", this.repo, ...args],
        { cwd: this.repo, timeout: timeoutMs, maxBuffer: 32 * 1024 * 1024, windowsHide: true, env: { ...process.env, PYTHONIOENCODING: "utf-8" } },
        (error, stdout, stderr) => {
          const code = error ? (typeof error.code === "number" ? error.code : -1) : 0;
          resolve({
            code,
            stdout: String(stdout),
            stderr: String(stderr || (error && code === -1 ? error.message : "")),
            missingExecutable: error?.code === "ENOENT",
          });
        },
      );
    });
  }

  /** Runs a command whose stdout is JSON. Exit codes 0-2 are normal (2 = needs a human). */
  async json<T>(args: string[], timeoutMs?: number, maxExitCode = 2): Promise<T> {
    const result = await this.run(args, timeoutMs);
    if (result.code < 0 || result.code > maxExitCode || !result.stdout.trim()) {
      throw new CovenantCliError(args[0], result);
    }
    try {
      return JSON.parse(result.stdout) as T;
    } catch {
      throw new CovenantCliError(args[0], result);
    }
  }
}

export class CovenantCliError extends Error {
  constructor(
    readonly command: string,
    readonly result: CliResult,
  ) {
    const detail = result.stderr.trim().split(/\r?\n/).slice(-3).join(" ") || `exit code ${result.code}`;
    super(`covenant ${command} failed: ${detail}`);
  }

  get notInstalled(): boolean {
    return Boolean(this.result.missingExecutable) || /No module named covenant(?:\s|$)/i.test(this.result.stderr);
  }
}
