import assert from "node:assert/strict";
import { test } from "node:test";
import { compileGlob, protectingClause } from "../src/glob";

// The same cases as tests/test_effects.py, so the editor agrees with the enforcer.
const cases: [string, string, boolean][] = [
  ["data/**", "data", true],
  ["data/**", "data/users.db", true],
  ["data/**", "data/a/b/c.json", true],
  ["data/**", "database.py", false],
  ["data/*.db", "data/users.db", true],
  ["data/*.db", "data/x/users.db", false],
  ["**/secrets.json", "secrets.json", true],
  ["**/secrets.json", "a/b/secrets.json", true],
];

for (const [pattern, path, expected] of cases) {
  test(`glob ${pattern} vs ${path}`, () => {
    assert.equal(compileGlob(pattern).test(path), expected);
  });
}

test("protectingClause honours excludes, skips home paths and normalizes slashes", () => {
  const rules = [{ clause: "C2", paths: ["data/**", "~/.mylib/**"], exclude: ["data/cache/**"] }];
  assert.equal(protectingClause("data\\users.json", rules), "C2");
  assert.equal(protectingClause("./data/users.json", rules), "C2");
  assert.equal(protectingClause("data/cache/model.bin", rules), undefined);
  assert.equal(protectingClause("mylib/core.py", rules), undefined);
});

test("special characters in patterns are literal", () => {
  assert.equal(compileGlob("a+b/(x).txt").test("a+b/(x).txt"), true);
  assert.equal(compileGlob("a.b").test("aXb"), false);
});
