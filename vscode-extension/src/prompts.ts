export const starters = [
  {
    id: "safe-refactor",
    title: "Refactor without breaking callers",
    goal: "Preserve public APIs and run tests before committing.",
    instruction: "Do not change public APIs. Run tests before committing.",
  },
  {
    id: "test-integrity",
    title: "Improve or fix tests",
    goal: "Keep existing tests enabled and require a passing run before committing.",
    instruction: "Do not delete or skip tests. Run tests before committing.",
  },
  {
    id: "clean-diff",
    title: "Keep a bug fix focused",
    goal: "Keep new dependencies and debug output out of the diff.",
    instruction: "Do not add new dependencies. Do not leave debug prints. Run tests before committing.",
  },
  {
    id: "sensitive-data",
    title: "Protect data and credentials",
    goal: "Guard data/ from destructive changes and keep secrets out of commits.",
    instruction: "Refactor repository code. Do not delete or overwrite files in data/. Do not commit secrets.",
  },
] as const;

export type Starter = (typeof starters)[number];
