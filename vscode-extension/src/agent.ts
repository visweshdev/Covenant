export const mcpClients = [
  { id: "vscode", label: "VS Code / Copilot", file: ".vscode/mcp.json" },
  { id: "cursor", label: "Cursor", file: ".cursor/mcp.json" },
  { id: "codex", label: "Codex", file: "~/.codex/config.toml" },
] as const;

export type McpClient = (typeof mcpClients)[number]["id"];

export function mcpConfig(client: McpClient, python: string, repo: string): string {
  const args = ["-m", "covenant", "--repo", repo, "mcp"];
  if (client === "codex") {
    return `[mcp_servers.covenant]\ncommand = ${JSON.stringify(python)}\nargs = ${JSON.stringify(args)}\n`;
  }
  const server = { command: python, args };
  const config = client === "vscode"
    ? { servers: { covenant: { type: "stdio", ...server } } }
    : { mcpServers: { covenant: server } };
  return JSON.stringify(config, null, 2) + "\n";
}

export const agentHandoff = [
  "Use this repository's active Covenant task while working on my request.",
  "Before editing, read the approved rules with covenant_rules if MCP is available. Otherwise, run `covenant status` from the repository and review any rules that need human input.",
  "Use covenant_check_command before running shell commands when MCP is available. If a rule blocks an action, explain the finding and ask before changing the rules or trying that action again.",
  "Run the declared tests through Covenant, then review the commit preflight. Summarize the checks you actually ran and any remaining findings before committing.",
].join("\n\n");
