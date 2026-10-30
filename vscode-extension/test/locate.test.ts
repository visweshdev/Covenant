import assert from "node:assert/strict";
import { test } from "node:test";
import { clauseLine, FileAccess, isReminder, locate, message } from "../src/locate";
import type { Report } from "../src/types";

const files: Record<string, string> = {
  "covenant.yaml": "covenant:\n  clauses:\n    - id: C1\n      kind: SemanticInvariant\n    - id: C2\n    - id: C3\n",
  "mylib/__init__.py": "from .core import Doc, parse\n",
  "mylib/core.py": "from dataclasses import dataclass\n\n@dataclass\nclass Doc:\n    text: str\n\n    def render(self):\n        pass\n\ndef parse(text, strict=False):\n    pass\n",
  "src/index.ts": "import x from './x';\n\nexport function parse(text: string): Doc {\n}\nexport class Client {\n  get(path: string) {}\n}\n",
  "package.json": '{\n  "dependencies": {\n    "react": "^18.2.0"\n  }\n}\n',
  "alembic/versions/002.py": "def upgrade():\n    op.drop_column('users', 'email')\n",
};

const access: FileAccess = {
  read: (r) => files[r],
  list: (dir, ext) => Object.keys(files).filter((f) => f.startsWith(dir ? `${dir}/` : "") && f.endsWith(ext)),
};

function report(partial: Partial<Report>): Report {
  return {
    violation: "C1", clause: "don't change public APIs", checker: "api_surface_diff", on_violation: "block",
    evidence: {}, repair_hints: ["Keep the old signature"], options: [], escalated: false, ...partial,
  };
}

test("clause lines in covenant.yaml", () => {
  assert.equal(clauseLine(files["covenant.yaml"], "C1"), 2);
  assert.equal(clauseLine(files["covenant.yaml"], "C3"), 5);
});

test("a changed Python function points at its definition", () => {
  const r = report({ evidence: { symbol: "mylib.parse", before: "def parse(text, strict=False)", after: "def parse(text)", exported_from: "mylib/__init__.py" } });
  assert.deepEqual(locate(r, access, "covenant.yaml"), { file: "mylib/core.py", line: 9 });
  assert.match(message(r), /^C1 \(don't change public APIs\): mylib\.parse changed: .* Fix: Keep the old signature$/);
});

test("a method points at the method; a removed one falls back to its class", () => {
  const method = report({ evidence: { symbol: "mylib.Doc.render", before: "def render(self)", after: null, exported_from: "mylib/__init__.py" } });
  assert.deepEqual(locate(method, access, "covenant.yaml"), { file: "mylib/core.py", line: 6 });
  const gone = report({ evidence: { symbol: "mylib.Doc.vanished", before: "x", after: null, exported_from: "mylib/__init__.py" } });
  assert.deepEqual(locate(gone, access, "covenant.yaml"), { file: "mylib/core.py", line: 3 });
  assert.match(message(gone), /mylib\.Doc\.vanished was removed/);
});

test("TypeScript symbols point into the entry file", () => {
  const fn = report({ checker: "ts_api_surface_diff", evidence: { symbol: "src/index.ts:parse", before: "a", after: "b" } });
  assert.deepEqual(locate(fn, access, "covenant.yaml"), { file: "src/index.ts", line: 2 });
  const method = report({ checker: "ts_api_surface_diff", evidence: { symbol: "src/index.ts:Client.get", before: "a", after: "b" } });
  assert.deepEqual(locate(method, access, "covenant.yaml"), { file: "src/index.ts", line: 5 });
});

test("protected files, migrations and dependencies", () => {
  const effect = report({ violation: "C2", checker: "effect_monitor", kind: "EffectProhibition", evidence: { op: "overwrite", target: "data/users.json", channel: "fs", pending: true } });
  assert.deepEqual(locate(effect, access, "covenant.yaml"), { file: "data/users.json", line: 0 });
  assert.match(message(effect), /changed data\/users\.json \(will be restored/);
  const migration = report({ checker: "schema_guard", evidence: { target: "alembic/versions/002.py", line: 2, operation: "drop_column", code: "op.drop_column(...)" } });
  assert.deepEqual(locate(migration, access, "covenant.yaml"), { file: "alembic/versions/002.py", line: 1 });
  const dep = report({ checker: "dependency_diff", evidence: { symbol: "react", ecosystem: "js", change: "major bump", before: "18.2.0", after: "19.0.0" } });
  assert.deepEqual(locate(dep, access, "covenant.yaml"), { file: "package.json", line: 2 });
});

test("rules without a file land on their clause; database and home paths too", () => {
  const trace = report({ violation: "C3", checker: "trace_monitor", kind: "TemporalObligation", evidence: { edits_since_last_passing_test: ["a.py"], last_passing_test: null } });
  assert.deepEqual(locate(trace, access, "covenant.yaml"), { file: "covenant.yaml", line: 5 });
  assert.equal(isReminder(trace), true);
  assert.match(message(trace), /no passing test run yet/);
  const db = report({ violation: "C2", checker: "effect_monitor", evidence: { op: "drop", target: "users", channel: "db" } });
  assert.equal(locate(db, access, "covenant.yaml").file, "covenant.yaml");
  const home = report({ violation: "C2", checker: "effect_monitor", evidence: { op: "delete", target: "~/.mylib/x", channel: "fs" } });
  assert.equal(locate(home, access, "covenant.yaml").file, "covenant.yaml");
});

test("tampering points at the tampered file when it is inside the repo", () => {
  const r = report({ violation: "INTEGRITY", checker: "integrity", evidence: { path: ".claude/settings.json", problem: "disableAllHooks is set" } });
  assert.deepEqual(locate(r, access, "covenant.yaml"), { file: ".claude/settings.json", line: 0 });
  assert.match(message(r), /disableAllHooks is set/);
});

test("Go, Rust and Java API changes use the line the checker found", () => {
  const go = report({ checker: "source_api_diff", evidence: { symbol: "store:Store.Size", before: "int", after: "int64", file: "store/store.go", line: 12 } });
  assert.deepEqual(locate(go, access, "covenant.yaml"), { file: "store/store.go", line: 11 });
  const unplaced = report({ violation: "C3", checker: "source_api_diff", evidence: { symbol: "crate::gone", before: "fn()", after: null } });
  assert.deepEqual(locate(unplaced, access, "covenant.yaml"), { file: "covenant.yaml", line: 5 });
});

test("a used-up action limit says so", () => {
  const r = report({ checker: "trace_monitor", kind: "TemporalObligation", repair_hints: [],
    evidence: { formula: "G(#push <= 1)", counts_so_far: { push: 1 }, edits_since_last_passing_test: [] } });
  assert.match(message(r), /limit reached \(so far: push 1\)/);
});
