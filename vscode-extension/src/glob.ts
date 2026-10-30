// Protected-path globs, matching Covenant's own rules (monitors/effects.py):
// `*` stays inside one folder, `**` crosses folders, and "data/**" also covers "data" itself.

function escape(ch: string): string {
  return ch.replace(/[.+^${}()|[\]\\?*]/g, "\\$&");
}

function body(pattern: string): string {
  let out = "";
  let i = 0;
  while (i < pattern.length) {
    if (pattern.startsWith("**/", i)) {
      out += "(?:.*/)?";
      i += 3;
    } else if (pattern.startsWith("**", i)) {
      out += ".*";
      i += 2;
    } else if (pattern[i] === "*") {
      out += "[^/]*";
      i += 1;
    } else if (pattern[i] === "?") {
      out += "[^/]";
      i += 1;
    } else {
      out += escape(pattern[i]);
      i += 1;
    }
  }
  return out;
}

export function normalizePattern(pattern: string): string {
  let p = pattern.replace(/\\/g, "/");
  while (p.startsWith("./")) p = p.slice(2);
  return p;
}

export function compileGlob(pattern: string): RegExp {
  const p = normalizePattern(pattern);
  let b = body(p);
  if (p.endsWith("/**")) b = `(?:${body(p.slice(0, -3))}|${b})`;
  return new RegExp(`^${b}$`);
}

export interface ProtectedRule {
  clause: string;
  paths: string[];
  exclude: string[];
}

/** The id of the first rule protecting `relPath` (repo-relative, any slashes), if any. */
export function protectingClause(relPath: string, rules: ProtectedRule[]): string | undefined {
  const path = relPath.replace(/\\/g, "/").replace(/^\.\//, "");
  for (const rule of rules) {
    const hit = rule.paths.some((p) => !p.startsWith("~") && compileGlob(p).test(path));
    const excluded = rule.exclude.some((p) => !p.startsWith("~") && compileGlob(p).test(path));
    if (hit && !excluded) return rule.clause;
  }
  return undefined;
}
