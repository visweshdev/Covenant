(() => {
  const vscode = acquireVsCodeApi();
  const panel = document.getElementById("panel");
  let revision;
  let state;
  const tabs = { overview: "Overview", rules: "Rules" };
  const previousTab = vscode.getState()?.tab;
  let tab = Object.hasOwn(tabs, previousTab) ? previousTab : "overview";
  const expanded = new Set();

  function element(tag, className, ...children) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.append(...children);
    return node;
  }

  function button(label, action, primary = false, value) {
    const node = element("button", primary ? "primary" : "secondary", label);
    node.type = "button";
    node.dataset.action = action;
    if (value !== undefined) node.dataset.value = value;
    return node;
  }

  function statusCard(tone, status, title, description, ...actions) {
    const titleRow = element("div", "status-label", element("span", "status-dot"), status);
    return element("section", `status-card ${tone}`,
      titleRow,
      element("h1", "", title),
      element("p", "muted", description),
      element("div", "actions", ...actions),
    );
  }

  function overview(state) {
    if (state.restricted) {
      return statusCard("warning", "Restricted Mode", "Trust this workspace",
        "Covenant needs workspace trust to run Python, tests, or hooks.", button("Manage Workspace Trust", "trust", true));
    }
    if (!state.folder) {
      return statusCard("neutral", "No folder", "Open a repository",
        "Open a local Git repository to set up Covenant rules.", button("Open Folder", "openFolder", true));
    }
    if (state.missingPython) {
      return statusCard("warning", "Python setup", "Connect Covenant to Python",
        "Select an environment with covenant-agents installed, or copy the install command to use in a terminal.",
        button("Select Python", "python", true), button("Copy Install Command", "copyInstall"),
        button("Check Again", "refresh"));
    }
    if (state.error) {
      const actions = [button("Check Again", "refresh", true)];
      if (state.policy) actions.push(button("Open YAML", "openPolicy"));
      return statusCard("warning", "Check unavailable", "Covenant needs attention", state.error,
        ...actions, button("Show Output", "output"));
    }
    if (!state.policy) {
      return statusCard("neutral", "Setup needed", "Give your agent clear rules",
        "Draft rules from an instruction, review them, then start a task.", button("Draft Rules", "compile", true));
    }
    if (!state.status) return element("p", "muted", "Checking workspace…");
    if (!state.status.has_session) {
      return statusCard("neutral", "Ready to start", "Your rules are ready",
        "Start a task to pin these rules and record the current Git baseline.", button("Start Task", "approve", true),
        button("Set Up Hooks", "init"), button("Open YAML", "openPolicy"));
    }
    if (state.status.repo_covenant_changed) {
      return statusCard("warning", "Review required", "Rules changed on disk",
        "The approved rules still apply. Review covenant.yaml before starting a new task.",
        button("Open YAML", "openPolicy", true), button("Start New Task", "approve"));
    }
    const issues = state.reports.filter((report) => !report.reminder);
    const reminders = state.reports.filter((report) => report.reminder);
    const actions = [button("Check Now", "refresh", true)];
    if (state.status.testCount) actions.push(button("Run Tests", "test"));
    const card = issues.length
      ? statusCard("bad", "Review current changes", `${issues.length} ${issues.length === 1 ? "issue" : "issues"} to review`,
        "Open a finding to jump to the relevant file or rule.", ...actions)
      : reminders.length
        ? statusCard("warning", "Before your next commit", "Follow-up needed",
          "Review the remaining requirements below.", ...actions)
        : statusCard("good", "Task active", "All current checks pass",
          "Your working tree was checked against the approved rules.", ...actions);
    return card;
  }

  function findings(title, reports) {
    const heading = element("h2", "", title);
    const rows = reports.map((report) => {
      const row = button(report.text, "report", false, report.index);
      row.classList.add("row-button");
      return element("li", "", row);
    });
    return element("section", "section", heading, element("ul", "rows", ...rows));
  }

  function overviewContents(state) {
    const issues = state.reports.filter((report) => !report.reminder);
    const reminders = state.reports.filter((report) => report.reminder);
    const contents = [overview(state)];
    if (issues.length) contents.push(findings("Issues", issues));
    if (reminders.length) contents.push(findings("Reminders", reminders));
    return element("div", "stack", ...contents);
  }

  function ruleRow(rule, state) {
    const issues = state.reports.filter((report) => report.id === rule.id);
    const hint = rule.status === "pending_clarification" ? "Needs input"
      : rule.status !== "enforced" ? "Manual"
        : !state.status.has_session ? "Awaiting approval"
          : issues.some((issue) => !issue.reminder) ? "Review"
            : issues.length ? "Reminder" : "Passing";
    const meta = [rule.checker, rule.response, rule.origin].filter(Boolean).join(" · ");
    const content = [
      meta ? element("p", "muted", meta) : element("p", "muted", "Manual review"),
      ...(rule.scope ?? []).map((scope) => element("p", "", `Scope: ${scope}`)),
      ...(rule.assumptions ?? []).map((assumption) => element("p", "", `Assumes ${assumption}`)),
      ...(rule.blocks ? [element("p", "", `Blocks this task: ${rule.blocks}`)] : []),
    ];
    if (!rule.origin) content.push(button("Open Rule", "rule", false, rule.id));
    const details = element("details", "rule", element("summary", "",
      element("span", "rule-title", element("span", "rule-id", rule.id), element("span", "rule-state", hint)),
      element("span", "rule-text", rule.text || rule.id)), element("div", "rule-body", ...content));
    details.dataset.rule = rule.id;
    details.querySelector("summary").dataset.rule = rule.id;
    details.open = expanded.has(rule.id);
    return details;
  }

  function rulesContents(state) {
    if (!state.status) {
      return element("section", "section", element("h2", "", "Rules"),
        element("p", "muted", state.policy ? "Get a successful check to view your rules." : "Create a rules file to see your rules here."),
        state.folder && !state.policy ? button("Draft Rules", "compile", true) : "");
    }
    const rules = state.status.rules;
    const enforced = rules.filter((rule) => rule.status === "enforced").length;
    return element("section", "section",
      element("h2", "", "Workspace rules"),
      element("p", "muted", `${enforced} automatic · ${rules.length - enforced} need human input or review`),
      element("div", "section-actions actions", button("Open YAML", "openPolicy"), button("Add a Rule", "amend")),
      element("div", "rule-list", ...rules.map((rule) => ruleRow(rule, state))),
    );
  }

  function render() {
    const folder = state.folder ? element("p", "workspace", state.folder) : document.createTextNode("");
    const focus = document.activeElement?.dataset;
    for (const details of panel.querySelectorAll("details[data-rule]")) {
      if (details.open) expanded.add(details.dataset.rule);
      else expanded.delete(details.dataset.rule);
    }
    const nav = element("nav", "tabs", ...Object.entries(tabs).map(([id, label]) => {
      const control = button(label, "tab", false, id);
      control.setAttribute("aria-pressed", String(tab === id));
      return control;
    }));
    nav.setAttribute("aria-label", "Covenant sections");
    const contents = tab === "rules" ? rulesContents(state) : overviewContents(state);
    if (state.busy) {
      for (const control of contents.querySelectorAll("button[data-action]")) {
        if (["compile", "approve", "init", "python", "test", "amend"].includes(control.dataset.action)) control.disabled = true;
      }
    }
    const progress = element("p", state.busy ? "progress" : "visually-hidden", state.busy ? `${state.busy} in progress…` : "");
    progress.setAttribute("role", "status");
    panel.replaceChildren(folder, nav, progress, contents);
    if (focus?.action) [...panel.querySelectorAll("button[data-action]")]
      .find((control) => control.dataset.action === focus.action && control.dataset.value === focus.value)?.focus();
    if (focus?.rule) [...panel.querySelectorAll("details[data-rule]")]
      .find((details) => details.dataset.rule === focus.rule)?.querySelector("summary").focus();
  }

  panel.addEventListener("click", (event) => {
    const target = event.target.closest("button[data-action]");
    if (!target) return;
    const { action, value } = target.dataset;
    if (action === "tab") {
      tab = value;
      vscode.setState({ tab });
      render();
    } else vscode.postMessage({ action, value, revision });
  });
  window.addEventListener("message", ({ data }) => {
    if (data?.type !== "state") return;
    state = data.state;
    revision = data.revision;
    render();
  });
  vscode.postMessage({ action: "ready" });
})();
