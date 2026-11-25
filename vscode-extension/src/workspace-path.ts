import * as path from "node:path";

/** Return a Git-style relative path only for a path inside this workspace folder. */
export function workspaceRelative(root: string, target: string): string | undefined {
  const relative = path.relative(root, target);
  if (path.isAbsolute(relative) || relative === ".." || relative.startsWith(`..${path.sep}`)) return undefined;
  return relative.split(path.sep).join("/");
}
