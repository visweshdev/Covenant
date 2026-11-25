import * as fs from "node:fs";
import * as path from "node:path";
import { parse, parseDocument } from "yaml";

export function readPolicy(file: string): string | undefined {
  const stats = fs.lstatSync(file, { throwIfNoEntry: false });
  if (!stats) return undefined;
  if (!stats.isFile()) throw new Error("covenant.yaml must be a regular file in the workspace.");
  return fs.readFileSync(file, "utf8");
}

export function preservePolicies(yaml: string, current?: string): string {
  if (current === undefined) return yaml;
  const inherits: unknown = parse(current, { merge: true })?.covenant?.extends;
  if (inherits === undefined || (Array.isArray(inherits) && inherits.length === 0)) return yaml;
  if (!Array.isArray(inherits) || !inherits.every((item) => typeof item === "string")) {
    throw new Error("Fix the existing policy's extends list before replacing it.");
  }
  const draft = parseDocument(yaml);
  if (draft.errors.length) throw draft.errors[0];
  draft.setIn(["covenant", "extends"], inherits);
  return draft.toString();
}

export function writeDraft(file: string, yaml: string, original?: string): void {
  if (original === undefined) {
    fs.writeFileSync(file, yaml, { flag: "wx" });
    return;
  }
  const directory = fs.mkdtempSync(path.join(path.dirname(file), ".covenant-draft-"));
  const temporary = path.join(directory, "covenant.yaml");
  try {
    fs.writeFileSync(temporary, yaml, { mode: fs.statSync(file).mode & 0o777 });
    if (readPolicy(file) !== original) throw new Error("covenant.yaml changed during review. Draft it again.");
    fs.renameSync(temporary, file);
  } finally {
    if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
    fs.rmdirSync(directory);
  }
}
