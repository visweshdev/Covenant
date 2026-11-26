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
    if (state.error) {
      return statusCard("warning", "Check unavailable", "Covenant needs attention", state.error,
        button("Check Again", "refresh", true), button("Show Output", "output"));
    }
    if (!state.policy) {
      return statusCard("neutral", "Setup needed", "Give your agent clear rules",
        "Draft rules from an instruction, review them, then start a task.", button("Draft Rules", "compile", true));
    }
    if (!state.status) return element("p", "muted", "Checking workspace…");
    if (!state.status.has_session) {
      return statusCard("neutral", "Ready to start", "Your rules are ready",
        "Start a task to pin these rules and record the current Git baseline.", button("Start Task", "approve", true),
        button("Set Up Hooks", "init"));
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
    panel.replaceChildren(folder, overview(state));
  });
  vscode.postMessage({ action: "ready" });
})();
