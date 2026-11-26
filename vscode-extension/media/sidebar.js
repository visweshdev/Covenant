(() => {
  const vscode = acquireVsCodeApi();
  const panel = document.getElementById("panel");

  function element(tag, className, ...children) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.append(...children);
    return node;
  }

  function button(label, action, primary = false) {
    const node = element("button", primary ? "primary" : "secondary", label);
    node.type = "button";
    node.dataset.action = action;
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
    return statusCard("good", "Task active", "Workspace checks",
      "Check the current working tree against your approved rules.", button("Check Now", "refresh", true));
  }

  panel.addEventListener("click", (event) => {
    const target = event.target.closest("button[data-action]");
    if (target) vscode.postMessage({ action: target.dataset.action });
  });
  window.addEventListener("message", ({ data }) => {
    if (data?.type !== "state") return;
    const { state } = data;
    const folder = state.folder ? element("p", "workspace", state.folder) : document.createTextNode("");
    const focus = document.activeElement?.dataset.action;
    const contents = overview(state);
    if (state.busy) {
      for (const control of contents.querySelectorAll("button[data-action]")) {
        if (["compile", "approve", "init", "python"].includes(control.dataset.action)) control.disabled = true;
      }
    }
    const progress = element("p", state.busy ? "progress" : "visually-hidden", state.busy ? `${state.busy} in progress…` : "");
    progress.setAttribute("role", "status");
    panel.replaceChildren(folder, progress, contents);
    if (focus) [...panel.querySelectorAll("button[data-action]")].find((control) => control.dataset.action === focus)?.focus();
  });
  vscode.postMessage({ action: "ready" });
})();
