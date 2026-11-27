import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { parse } from "yaml";
import { preservePolicies, readPolicy, writeDraft } from "../src/draft";

const fresh = "covenant:\n  instruction: Keep tests\n  clauses: []\n";

test("the draft preserves inherited policies in wrapped and flat files", () => {
  for (const current of ["covenant: { extends: ['team rules.yml'] }", "extends: ['team rules.yml']"]) {
    assert.deepEqual(parse(preservePolicies(fresh, current)).covenant.extends, ["team rules.yml"]);
  }
  assert.equal(preservePolicies(fresh, "covenant: { extends: [] }"), fresh);
  assert.throws(() => preservePolicies(fresh, "covenant: { extends: [100] }"), /extends list/);
});

test("writing a reviewed draft preserves concurrent edits and saves exact content", (context) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "covenant-draft-test-"));
  context.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const file = path.join(directory, "covenant.yaml");
  assert.equal(readPolicy(file), undefined);
  writeDraft(file, "# initial\n");
  assert.throws(() => writeDraft(file, fresh), { code: "EEXIST" });
  assert.throws(() => writeDraft(file, fresh, "# changed\n"), /changed during review/);
  assert.equal(readPolicy(file), "# initial\n");
  writeDraft(file, fresh, "# initial\n");
  assert.equal(readPolicy(file), fresh);
  assert.deepEqual(fs.readdirSync(directory), ["covenant.yaml"]);
});
