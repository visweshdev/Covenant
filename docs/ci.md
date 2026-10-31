<sub>[Covenant](../README.md) › [Docs](README.md) › CI</sub>

# ✅ CI

Every pull request is judged against the rules, wherever it came from.

---

## GitHub Action

```yaml
# .github/workflows/covenant.yml
on: pull_request
permissions:
  contents: read
  pull-requests: write          # for the comments
jobs:
  covenant:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: visweshdev/Covenant@main
        # with: { comment: "false", run-tests: "false", agent: payments }
```

| Input | Default | What it does |
|---|---|---|
| `comment` | `true` | Comment on the PR |
| `run-tests` | `true` | Run the declared tests on the PR's final state |
| `agent` | | The agent name for [path ownership](rules.md#refused-commands) rules |
| `covenant` | `covenant.yaml` | Path to the rules file |
| `base` | the PR's target | The commit to compare against |
| `python-version` | `3.12` | Python used to run Covenant |

### Comments

- The action comments on the **exact lines** that break a rule, once; re-runs don't repeat a
  comment.
- It keeps **one summary comment** on the PR up to date.
- Problems with no changed line to point at, like a deleted protected file, go in the summary.

## Without the action

```bash
covenant ci --format github --comment     # with GITHUB_TOKEN set
```

| Flag | What it does |
|---|---|
| `--base REF` | The commit to compare against |
| `--format github\|json\|text` | Output format; `github` writes annotations |
| `--no-tests` | Skip the test run |
| `--comment` | Post the PR comments |

## How a pull request is judged

- The rules come from the **base** commit, so a PR can't weaken them. A PR that changes
  `covenant.yaml` is flagged for human review.
- API and migration checkers compare against the base.
- Protected files are checked against the diff.
- Test commands run on the PR's final state.
- [Team policy](tools.md#team-policies) rules come from the base commit too, and a PR that
  changes them is flagged.

---

<sub>Previous: [🤖 Agents](agents.md) · Next: [🧰 Tools](tools.md)</sub>
