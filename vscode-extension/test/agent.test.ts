import assert from "node:assert/strict";
import { test } from "node:test";
import { mcpConfig } from "../src/agent";
import { workspaceRelative } from "../src/workspace-path";

test("MCP snippets preserve spaces and separators in the selected interpreter and repository", () => {
  const python = "C:\\Project ENV\\Scripts\\python.exe";
  const directory = "C:\\My Projects\\répo";
  const args = ["-m", "covenant", "--repo", directory, "mcp"];
  assert.deepEqual(JSON.parse(mcpConfig("vscode", python, directory)).servers.covenant, { type: "stdio", command: python, args });
  assert.deepEqual(JSON.parse(mcpConfig("cursor", python, directory)).mcpServers.covenant, { command: python, args });
  assert.equal(mcpConfig("codex", python, directory), `[mcp_servers.covenant]\ncommand = ${JSON.stringify(python)}\nargs = ${JSON.stringify(args)}\n`);
});

test("workspace relative paths reject siblings while preserving local names that start with dots", () => {
  assert.equal(workspaceRelative("repo", "repository/file.ts"), undefined);
  assert.equal(workspaceRelative("repo", "repo/..safe/file.ts"), "..safe/file.ts");
});
