<p align="center"><img src="docs/logo.png" width="72" alt="Covenant logo"></p>

<h1 align="center">Security</h1>

---

Covenant is a guard between a coding agent and your repository, so a way for an agent to get
past it is a security bug. For example:

- an action that breaks an enforced rule without being blocked, reported or rolled back;
- changing Covenant's own files, hooks or state without the tamper check noticing;
- a crash that lets a tool call through instead of failing closed;
- the hook server or dashboard answering requests from outside the machine.

## Reporting

> [!IMPORTANT]
> Please report privately using GitHub's **Report a vulnerability** button on the repository's
> **Security** tab, not in a public issue.

Include:

- the steps to reproduce;
- the Covenant version (`covenant --version`);
- your OS.

You should hear back within a week. Once a fix is released, you'll be credited unless you'd
rather not be.

## Known limits

The [safety model](docs/safety.md) describes what Covenant does not protect against, such as an
agent that runs with the same permissions as Covenant itself. Those limits aren't
vulnerabilities, but ideas for closing them are welcome.
