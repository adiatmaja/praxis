    const API = window.location.origin;
    let token = localStorage.getItem("praxis_token") || "";
    let eventSource = null;
    let currentView = "dashboard";
    let projects = [];
    let plans = [];
    let currentTasks = [];
    let selectedProjectId = null;
    let globalProjectFilter = localStorage.getItem("praxis_project_filter") || "all";
    let selectedPlanId = null;
    let selectedTaskId = null;
    let showingForm = false;
    let dashboardTasks = {};       // map: planId -> tasks[]
    // Whether each plan's task fetch actually ANSWERED. A failing GET used to
    // write an empty array, which every reader downstream took as a measured
    // zero: the lane promised task cards, the needs-attention badge lost its
    // failed-task term, and the completed tally rendered "0/0". `/api/status`
    // already draws exactly this line for the agent manager
    // (`agents_reachable`), and this is the same rule applied here: a zero
    // nobody could measure is not a zero.
    let dashboardTasksReachable = {};  // map: planId -> boolean
    // Spec documents read back from the repo for the "View Spec" toggle.
    // undefined = not fetched yet, null = the read failed, string = the doc.
    let dashboardSpecCache = {};   // map: planId -> markdown | null
    let dashboardTaskLogs = {};    // map: taskId -> last log line
    let dashboardTaskRawLog = {};  // map: taskId -> accumulated raw log text
    let selectedDashboardTaskId = null;
    let sidePanelLogSource = null; // EventSource for side panel live log
    let liveLogMode = "clean";     // "clean" | "raw" for side panel
    let fullLogsMode = "clean";    // "clean" | "raw" for full-logs view
    // How many completed plans get a collapsed lane on the dashboard. The
    // remainder is REPORTED rather than dropped: see renderDashboard.
    const COMPLETED_LANE_LIMIT = 5;
    let expandedCompletedPlans = new Set();
    let expandedSpecs = new Set();
    let dashboardSseSource = null;
    let HARNESSES = [];
    let lifecycleItems = [];
    let effectiveLmStudioUrl = null;  // global effective LM Studio URL (from /api/status)
    // The last numbers /api/status actually MEASURED, kept as values rather
    // than read back out of the rendered DOM. `pollStatus` writes "?" into
    // #stat-agents when the agent manager could not be reached; parsing that
    // text back gave `Number("?")` = NaN and the health bar printed the
    // literal string "Agents: NaN", undoing the unknown-state handling one
    // function away. Before the first poll resolves, reading the DOM instead
    // returned the index.html default 0 and asserted a zero nobody measured.
    // `null` means "not measured", which is a third state, not zero.
    let measuredAgentCount = null;
    let measuredQueueCount = null;
    // What counts as "landed" for a task, mirroring the engine's
    // SATISFIED_STATUSES (src/orchestrator/core/status_vocab.py). All three
    // are terminal and satisfied: MERGED did the work itself, SUPERSEDED
    // handed it to split children, NO_CHANGES found it already done, and they
    // are what LETS a plan complete. `failed` is deliberately absent, so
    // widening the numerator never hides a task that did not land.
    const SATISFIED_TASK_STATUSES = ["merged", "no_changes", "superseded"];
    let selectedLifecycle = null;
    let lifecycleSegment = "spec";
    // Work parked at the human merge gate, across all projects, PLUS the
    // autonomous improvement proposals parked at the earlier approve/reject
    // gate. Refreshed once on load and again whenever the rate-limited
    // approvals_digest SSE event fires; the header badge must be correct
    // before the first event arrives.
    //
    // `count` is the API's merge-gate total: a number of DISTINCT PULL
    // REQUESTS, not of rows. In single-branch mode N tasks share one work
    // branch and so one PR, so it is routinely SMALLER than
    // `tasks.length + plans.length`, deliberately. It also EXCLUDES
    // proposals, because it feeds a digest line that calls its items "PRs"
    // and a proposal has neither a branch nor a PR. `proposals` therefore
    // has to be read separately everywhere, or a live decision is invisible;
    // declaring it here means no consumer ever reads `undefined`.
    let pendingApprovals = {
      count: 0, proposal_count: 0, oldest_hours: 0,
      tasks: [], plans: [], proposals: []
    };

    initTheme();

    function initTheme() {
      const stored = localStorage.getItem("praxis_theme") || "light";
      document.documentElement.dataset.theme = stored;
    }

    function toggleTheme() {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      localStorage.setItem("praxis_theme", next);
    }

    function ensureToken() {
      if (!token) resetToken();
      return token;
    }

    function resetToken() {
      openTokenModal();
    }

    function openTokenModal() {
      const overlay = document.getElementById("token-overlay");
      overlay.classList.add("open");
      const input = document.getElementById("token-input");
      input.value = token || "";
      input.type = "password";
      document.getElementById("token-eye").textContent = "Show";
      setTokenFeedback("", "");
      input.focus();
    }

    function closeTokenModal() {
      document.getElementById("token-overlay").classList.remove("open");
    }

    function onTokenOverlayClick(event) {
      if (event.target === document.getElementById("token-overlay")) closeTokenModal();
    }

    function onTokenKey(event) {
      if (event.key === "Enter") { event.preventDefault(); saveToken(); }
      else if (event.key === "Escape") closeTokenModal();
    }

    function toggleTokenVisibility() {
      const input = document.getElementById("token-input");
      const eye = document.getElementById("token-eye");
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      eye.textContent = show ? "Hide" : "Show";
    }

    function setTokenFeedback(msg, kind) {
      const el = document.getElementById("token-feedback");
      el.textContent = msg;
      el.className = "token-feedback" + (kind ? " " + kind : "");
    }

    function clearToken() {
      token = "";
      localStorage.removeItem("praxis_token");
      document.getElementById("token-input").value = "";
      setTokenFeedback("Token cleared.", "");
    }

    async function saveToken() {
      const candidate = document.getElementById("token-input").value.trim();
      if (!candidate) { setTokenFeedback("Enter a token.", "error"); return; }
      const saveBtn = document.getElementById("token-save");
      saveBtn.disabled = true;
      setTokenFeedback("Verifying…", "");
      let verified = false;
      try {
        const res = await fetch(API + "/api/status", {
          headers: { "Authorization": "Bearer " + candidate, "Content-Type": "application/json" }
        });
        if (res.status === 401 || res.status === 403) {
          setTokenFeedback("Invalid token — the server rejected it.", "error");
          saveBtn.disabled = false;
          return;
        }
        verified = res.ok;
        setTokenFeedback(res.ok ? "Token verified." : "Server error (" + res.status + "). Token saved anyway.", res.ok ? "ok" : "error");
      } catch (error) {
        setTokenFeedback("Could not reach the server. Token saved — check the connection.", "error");
      }
      token = candidate;
      localStorage.setItem("praxis_token", token);
      saveBtn.disabled = false;
      pollStatus();
      if (verified) {
        setTimeout(() => { closeTokenModal(); switchView(currentView); }, 500);
      }
    }

    function headers() {
      return {
        "Authorization": "Bearer " + ensureToken(),
        "Content-Type": "application/json"
      };
    }

    async function api(method, path, body) {
      const options = { method, headers: headers() };
      if (body !== undefined) options.body = JSON.stringify(body);
      const response = await fetch(API + path, options);
      if (!response.ok) {
        // Carry the STATUS on the error. Every caller used to get only the
        // body, so the one renderer that catches these had no way to tell a
        // server that never answered from one that answered "your token is
        // wrong", and it called both "Connection failed". See `switchView`'s
        // catch: on a fresh install the first thing the dashboard says is the
        // one thing that is not true.
        const error = new Error(await response.text());
        error.status = response.status;
        throw error;
      }
      if (response.status === 204) return null;
      return response.json();
    }

    // Strip ANSI SGR / CSI escape sequences (opencode CLI emits colored output).
    // eslint-disable-next-line no-control-regex
    const ANSI_RE = /\x1b[[()#;?]*(?:[0-9]{1,4}(?:;[0-9]{0,4})*)?[0-9A-ORZcf-nqry=><]/g;
    function stripAnsi(s) { return String(s == null ? "" : s).replace(ANSI_RE, ""); }

    function esc(value) {
      return String(value ?? "").replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[c]));
    }

    function renderMarkdown(src) {
      // Strip a leading YAML frontmatter block (--- ... ---) so spec_path/type
      // metadata isn't shown as literal text.
      const text = (src || "").replace(/\r\n/g, "\n").replace(/^\s*---\n[\s\S]*?\n---\n?/, "");
      const lines = text.split("\n");
      let html = "", inCode = false, listType = null;
      const closeList = () => { if (listType) { html += listType === "ol" ? "</ol>" : "</ul>"; listType = null; } };
      const inline = (s) => esc(s)
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/\*([^*]+)\*/g, '<em>$1</em>')
        // Links: [text](url) — only http(s) or relative paths, never javascript:
        .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+|[^):\s]+)\)/g,
          '<a href="$2" target="_blank" rel="noopener">$1</a>');
      for (const raw of lines) {
        // A fence line is ```[info]. An OPEN fence may carry an info string
        // (```bash); per CommonMark a fence only CLOSES on a bare ``` with no
        // info string — otherwise nested ```lang blocks leak their content.
        const fence = raw.trim().match(/^(`{3,})(.*)$/);
        if (fence) {
          if (!inCode) { closeList(); html += "<pre><code>"; inCode = true; }
          else if (fence[2].trim() === "") { html += "</code></pre>"; inCode = false; }
          else { html += esc(raw) + "\n"; }
          continue;
        }
        if (inCode) { html += esc(raw) + "\n"; continue; }
        if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(raw)) { closeList(); html += "<hr>"; continue; }
        const h = raw.match(/^(#{1,6})\s+(.*)$/);
        const cb = raw.match(/^\s*-\s\[( |x|X)\]\s+(.*)$/);
        const li = raw.match(/^\s*[-*]\s+(.*)$/);
        const ol = raw.match(/^\s*(\d+)\.\s+(.*)$/);
        const bq = raw.match(/^\s*>\s?(.*)$/);
        if (h) { closeList(); html += "<h" + h[1].length + ">" + inline(h[2]) + "</h" + h[1].length + ">"; continue; }
        if (cb) { if (listType !== "check") { closeList(); html += "<ul class='md-checks'>"; listType = "check"; } const done = cb[1] !== " "; html += "<li>" + (done ? "&#9745;" : "&#9744;") + " " + inline(cb[2]) + "</li>"; continue; }
        if (li) { if (listType !== "ul") { closeList(); html += "<ul>"; listType = "ul"; } html += "<li>" + inline(li[1]) + "</li>"; continue; }
        if (ol) { if (listType !== "ol") { closeList(); html += "<ol>"; listType = "ol"; } html += "<li>" + inline(ol[2]) + "</li>"; continue; }
        if (bq) { closeList(); html += "<blockquote>" + inline(bq[1]) + "</blockquote>"; continue; }
        closeList();
        if (raw.trim()) html += "<p>" + inline(raw) + "</p>";
      }
      closeList();
      if (inCode) html += "</code></pre>";
      return html;
    }

    function jsArg(value) {
      return esc(JSON.stringify(String(value ?? "")));
    }

    function safeHref(value) {
      try {
        const url = new URL(String(value ?? ""), window.location.href);
        if (url.protocol === "http:" || url.protocol === "https:") return esc(url.href);
      } catch (error) {
        return "";
      }
      return "";
    }

    function badge(status) {
      return '<span class="badge ' + esc(status) + '">' + esc(status) + '</span>';
    }

    function mono(value) {
      return '<span style="font-family:Cascadia Mono, SF Mono, Consolas, monospace;font-size:12px;">' + esc(value || "-") + '</span>';
    }

    function setTopbar(title, actionLabel) {
      document.getElementById("topbar-title").textContent = title;
      const action = document.getElementById("primary-action");
      action.textContent = actionLabel;
      // Hide the per-view action when it has no label (Memory) or would just
      // duplicate the persistent "+ Create Spec" button (Plans view).
      action.style.display = actionLabel ? "" : "none";
    }

    function primaryAction() {
      if (currentView === "dashboard") {
        switchView("projects").then(() => { showingForm = true; renderProjectsView(); });
      } else if (currentView === "projects") {
        showingForm = true;
        renderProjectsView();
      } else if (currentView === "plans") {
        openSpecChat();
      } else if (currentView === "tasks") {
        renderTasks();
      } else {
        connectEvents();
      }
    }

    async function switchView(name) {
      currentView = name;
      showingForm = false;
      // showPendingApprovals() overrides the primary action button's click
      // handler; any real navigation restores the default dispatcher.
      const primaryActionEl = document.getElementById("primary-action");
      if (primaryActionEl) primaryActionEl.onclick = primaryAction;
      document.querySelectorAll(".nav-item").forEach(el => el.classList.toggle("active", el.dataset.view === name));
      const viewContainer = document.getElementById("view-container");
      if (viewContainer) viewContainer.innerHTML = '<div class="view-loader"><div class="spinner"></div><span>Loading…</span></div>';
      if (eventSource && name !== "logs" && name !== "dashboard") {
        eventSource.close();
        eventSource = null;
      }
      if (name !== "dashboard" && sidePanelLogSource) {
        sidePanelLogSource.close();
        sidePanelLogSource = null;
      }
      if (name === "dashboard") {
        setTopbar("Dashboard", "+ New Project");
        await loadDashboard();
      } else if (name === "projects") {
        setTopbar("Projects", "+ New Project");
        await loadProjects();
      } else if (name === "plans") {
        setTopbar("Plans", "");
        await loadPlans();
        await loadLifecycle();
      } else if (name === "tasks") {
        setTopbar("Tasks", "Refresh");
        await renderTasks();
      } else if (name === "logs") {
        setTopbar("Live Logs", "Reconnect");
        renderLogs();
      } else if (name === "specs") {
        setTopbar("Specs", "Refresh");
        await renderDocs("spec");
      } else if (name === "docplans") {
        setTopbar("Plan Docs", "Refresh");
        await renderDocs("plan");
      } else if (name === "memory") {
        setTopbar("Memory", "");
        await renderMemory();
      }
    }

    async function loadProjects() {
      projects = await api("GET", "/api/projects");
      if (!selectedProjectId && projects.length) selectedProjectId = projects[0].id;
      if (!projects.some(project => project.id === selectedProjectId)) selectedProjectId = projects[0]?.id || null;
      document.getElementById("stat-projects").textContent = projects.length;
      populateGlobalProjectSelect();
      if (currentView === "projects") renderProjectsView();
    }

    function populateGlobalProjectSelect() {
      const sel = document.getElementById("global-project");
      if (!sel) return;
      if (globalProjectFilter !== "all" && !projects.some(p => p.id === globalProjectFilter)) {
        globalProjectFilter = "all";
        localStorage.setItem("praxis_project_filter", globalProjectFilter);
      }
      sel.innerHTML = ['<option value="all">All Projects</option>']
        .concat(projects.map(p => '<option value="' + esc(p.id) + '">' + esc(p.name) + '</option>'))
        .join("");
      sel.value = globalProjectFilter;
      refreshGitState(globalProjectFilter);
    }

    async function refreshGitState(projectId) {
      const el = document.getElementById("git-state-line");
      if (!el) return;
      if (!projectId || projectId === "all") {
        el.textContent = "—";
        el.title = "";
        return;
      }
      try {
        const gs = await api("GET", "/api/projects/" + projectId + "/git-state");
        if (!gs.available) {
          el.textContent = "origin/" + (gs.base || "main") + " · unavailable";
          el.title = gs.detail || "";
          return;
        }
        const when = gs.committed_at ? " · " + gs.committed_at.slice(0, 10) : "";
        el.textContent = "origin/" + gs.base + " · " + (gs.short_sha || "") + when;
        el.title = (gs.subject || "") + (gs.sha ? " (" + gs.sha + ")" : "");
      } catch (e) {
        el.textContent = "origin · error";
        el.title = String(e);
      }
    }

    function onGlobalProjectChange() {
      globalProjectFilter = document.getElementById("global-project").value;
      localStorage.setItem("praxis_project_filter", globalProjectFilter);
      if (globalProjectFilter !== "all") selectedProjectId = globalProjectFilter;
      selectedPlanId = null;
      selectedTaskId = null;
      selectedDashboardTaskId = null;
      refreshGitState(globalProjectFilter);
      switchView(currentView);
    }

    function visiblePlans() {
      return globalProjectFilter === "all"
        ? plans
        : plans.filter(plan => plan.project_id === globalProjectFilter);
    }

    async function loadPlans() {
      if (!projects.length) await loadProjects();
      plans = [];
      for (const project of projects) {
        const projectPlans = await api("GET", "/api/projects/" + project.id + "/plans");
        plans.push(...projectPlans.map(plan => ({ ...plan, projectName: project.name })));
      }
      const visible = visiblePlans();
      if (!selectedPlanId && visible.length) selectedPlanId = visible[0].id;
      if (!visible.some(plan => plan.id === selectedPlanId)) selectedPlanId = visible[0]?.id || null;
      document.getElementById("stat-plans").textContent = plans.length;
      if (currentView === "plans") renderPlansView();
    }

    function renderProjectsView() {
      const container = document.getElementById("view-container");
      const masterRows = projects.map(project =>
        '<button class="master-row' + (selectedProjectId === project.id ? ' selected' : '') +
        '" type="button" onclick="selectProject(\'' + esc(project.id) + '\')">' +
          '<div class="row-main"><div class="row-name">' + esc(project.name) + '</div>' +
          '<div class="row-meta">' + esc(project.repo_url) + '</div></div>' +
          (project.approval_gate ? badge("active") : badge("pending")) +
        '</button>'
      ).join("") || '<div class="empty-list">No projects</div>';
      const detail = showingForm ? renderProjectForm() :
        selectedProjectId ? renderProjectDetail(projects.find(project => project.id === selectedProjectId)) :
        '<div class="detail-empty">Select a project</div>';

      container.innerHTML =
        '<div class="master-panel">' +
          '<div class="master-header"><div class="master-title">' + projects.length + ' Projects</div>' +
          '<button class="btn btn-compact" type="button" onclick="loadProjects()">Refresh</button></div>' +
          '<div class="master-list">' + masterRows + '</div>' +
        '</div><div class="detail-panel">' + detail + '</div>';
    }

    function selectProject(id) {
      selectedProjectId = id;
      showingForm = false;
      renderProjectsView();
    }

    function renderProjectDetail(project) {
      if (!project) return '<div class="detail-empty">Project not found</div>';
      return '<div class="detail-header">' +
        '<div class="detail-title">' + esc(project.name) + '</div>' +
        '<div class="detail-subtitle">' + esc(project.repo_url) + '</div>' +
      '</div><div class="detail-content">' +
        '<div class="detail-section"><div class="detail-section-title">Configuration</div><div class="detail-card">' +
          '<div class="detail-field"><span class="field-label">Model</span><span class="field-value">' + mono(project.model_name) + '</span></div>' +
          '<div class="detail-field"><span class="field-label">Default Branch</span><span class="field-value">' + esc(project.default_branch) + '</span></div>' +
          '<div class="detail-field"><span class="field-label">Approval Gate</span><span class="field-value">' + (project.approval_gate ? "On" : "Off") + '</span></div>' +
          '<div class="detail-field"><span class="field-label">Max Retries</span><span class="field-value">' + esc(project.max_retries) + '</span></div>' +
          '<div class="detail-field"><span class="field-label">Confidence Threshold</span><span class="field-value">' + Math.round((project.confidence_threshold || 0) * 100) + '%</span></div>' +
          '<div class="detail-field"><span class="field-label">LM Studio</span><span class="field-value">' + mono(effectiveLmStudioUrl || project.lm_studio_url) +
            (effectiveLmStudioUrl && effectiveLmStudioUrl !== project.lm_studio_url ? '<span style="color:var(--text-faint);font-size:11px;margin-left:6px;">(effective — global override)</span>' : '') + '</span></div>' +
        '</div></div>' +
      '</div><div class="detail-actions">' +
        '<button class="btn btn-primary" type="button" onclick="switchView(\'plans\')">View Plans</button>' +
        '<button class="btn btn-danger" type="button" onclick="deleteProject(\'' + esc(project.id) + '\')">Delete</button>' +
      '</div>';
    }

    function renderProjectForm() {
      const html = '<div class="detail-header"><div class="detail-title">New Project</div>' +
        '<div class="detail-subtitle">Add a repository for orchestration</div></div>' +
        '<div class="detail-content"><form onsubmit="submitProject(event)">' +
          '<div class="formrow"><label>Name</label><input id="pf-name" required autocomplete="off"></div>' +
          '<div class="formrow"><label>Repository URL</label><input id="pf-repo" required autocomplete="off" placeholder="https://github.com/user/repo"></div>' +
          '<div class="formrow"><label>Model (implementer)</label>' +
            '<select id="pf-model-select" onchange="onModelPreset(this)"><option value="">Loading models…</option></select>' +
            '<input id="pf-model" required autocomplete="off" placeholder="model id" style="margin-top:6px;display:none;">' +
            '<div id="pf-model-note" class="settings-note" style="display:none;"></div></div>' +
          '<div class="formrow"><label>Agent model (reasoning)</label>' +
            '<select id="pf-agent-model-preset" onchange="onAgentModelPreset(this.value)">' +
              '<option value="">Global default</option>' +
              '<option value="claude-opus-4-8|">Opus (claude-opus-4-8)</option>' +
              '<option value="claude-opus-4-8|low">Opus — low effort</option>' +
              '<option value="claude-sonnet-4-6|">Sonnet (claude-sonnet-4-6)</option>' +
              '<option value="claude-haiku-4-5|">Haiku (claude-haiku-4-5)</option>' +
              '<option value="custom">Custom…</option>' +
            '</select>' +
            '<input id="pf-agent-model" placeholder="custom model id" style="display:none;margin-top:6px;">' +
          '</div>' +
          '<div class="formrow"><label for="project-harness">Harness</label>' +
            '<div style="display:flex;gap:8px;align-items:center;">' +
              '<select id="project-harness" name="harness" style="flex:1;"></select>' +
              '<button type="button" class="btn btn-compact" onclick="renderHarnessAbout()">About harnesses</button>' +
            '</div>' +
          '</div>' +
          '<div class="form-actions"><button type="submit" class="btn btn-primary">Create</button>' +
          '<button type="button" class="btn" onclick="showingForm=false;renderProjectsView()">Cancel</button></div>' +
        '</form></div>';
      // Populate harness + model dropdowns after DOM update (async, next microtask)
      setTimeout(loadHarnesses, 0);
      setTimeout(loadLmModels, 0);
      return html;
    }

    function onModelPreset(select) {
      const custom = document.getElementById("pf-model");
      const value = select.value;
      if (value === "__custom__") {
        custom.style.display = "block";
        custom.value = "";
        custom.focus();
      } else {
        custom.style.display = "none";
        custom.value = value;
      }
      // Selecting a preset option sets the harness field too: presets are
      // convenience wiring over harness+model+endpoint together.
      syncHarnessToSelectedModel();
    }

    // Point #project-harness at whatever the currently selected model option
    // declares. Called on change AND at the end of both loadHarnesses() and
    // loadLmModels(), because the browser auto-selects the first option once
    // the model dropdown is populated and no change event fires for that.
    //
    // Order-independence: the two loaders are both scheduled with
    // setTimeout(..., 0) and both await the network, so either can finish
    // first, and assigning .value to a select with no options silently does
    // nothing. Each call is a no-op unless BOTH selects are populated, so
    // whichever loader finishes last is the one that lands the sync.
    function syncHarnessToSelectedModel() {
      const modelSel = document.getElementById("pf-model-select");
      const harnessSel = document.getElementById("project-harness");
      if (!modelSel || !harnessSel || !harnessSel.options.length) return;
      const opt = modelSel.selectedOptions && modelSel.selectedOptions[0];
      const harness = opt && opt.dataset.harness;
      if (harness) harnessSel.value = harness;
    }

    // Show/clear the reachability note under the model dropdown. Presets are
    // legitimate configuration and are never removed just because the
    // endpoint is currently down: this note is what makes that state
    // OBVIOUS instead of silently offering models that are not served.
    function setModelAvailabilityNote(text) {
      const note = document.getElementById("pf-model-note");
      if (!note) return;
      if (text) {
        note.textContent = text;
        note.style.display = "block";
        note.style.color = "#ef4444";
      } else {
        note.textContent = "";
        note.style.display = "none";
      }
    }

    async function loadLmModels() {
      const select = document.getElementById("pf-model-select");
      const custom = document.getElementById("pf-model");
      if (!select) return;
      let presets = [];
      try {
        const data = await api("GET", "/api/settings/presets");
        presets = data.presets || [];
      } catch (e) { /* presets unavailable, fall through to the LM Studio list */ }
      let models = [];
      let connected = false;
      let lmStudioUrl = "";
      let embeddingIds = [];
      try {
        const data = await api("GET", "/api/lm-models");
        models = data.models || [];
        connected = !!data.connected;
        lmStudioUrl = data.lm_studio_url || "";
        embeddingIds = data.embedding_model_ids || [];
      } catch (e) { /* fall through to manual entry */ }
      // Never offer an embedding model as an implementer, it generates
      // vectors, not code. embeddingIds is populated only when LM Studio's
      // richer /api/v0/models endpoint confirmed the "type"; when that
      // enrichment wasn't available it comes back empty and nothing here is
      // filtered out (silence, not a false "this one is safe" claim).
      const embeddingSet = new Set(embeddingIds);
      const implementerModels = models.filter(m => !embeddingSet.has(m));
      const liveModelSet = new Set(implementerModels);
      if (presets.length || implementerModels.length) {
        // A preset with no external requirement (no api_key, no interactive
        // login) is understood to run through this same LM Studio endpoint,
        // so whether it is CURRENTLY being served is knowable and worth
        // saying. A preset that requires something else talks to a
        // different endpoint or process this check never touched, so it is
        // left unannotated rather than guessed at.
        const presetGroups = presets.map(p => {
          const locallyChecked = !(p.requires && p.requires.length);
          let suffix = "";
          if (locallyChecked) {
            if (!connected) {
              suffix = " (endpoint unreachable)";
            } else if (liveModelSet.has(p.model)) {
              suffix = " (live)";
            } else {
              suffix = " (not currently loaded)";
            }
          }
          return '<optgroup label="' + esc(p.label) + '">' +
            '<option value="' + esc(p.model) + '" data-harness="' + esc(p.harness) + '">' +
              esc(p.model + suffix) +
            '</option>' +
          '</optgroup>';
        }).join("");
        // De-duplicate: a preset whose model is also a live LM Studio model
        // is dropped from the "Other" group so it appears once: as the
        // preset entry, which carries the harness metadata the bare live
        // entry does not.
        const presetModelNames = new Set(
          presets
            .filter(p => !(p.requires && p.requires.length))
            .map(p => p.model)
        );
        const otherModels = implementerModels.filter(m => !presetModelNames.has(m));
        const lmGroup = otherModels.length
          ? '<optgroup label="Other (from LM Studio)">' +
              otherModels.map(m => '<option value="' + esc(m) + '">' + esc(m) + '</option>').join("") +
            '</optgroup>'
          : "";
        select.innerHTML = presetGroups + lmGroup + '<option value="__custom__">Custom…</option>';
        select.style.display = "";
        if (custom) { custom.style.display = "none"; custom.value = select.value; }
        // The browser just auto-selected the first option without firing
        // change; sync the harness to it or Create submits a harness that
        // matches the selected model only by coincidence.
        syncHarnessToSelectedModel();
        setModelAvailabilityNote(connected ? "" :
          "No model server reachable" + (lmStudioUrl ? " at " + lmStudioUrl : "") +
          ". Presets below are configured but not confirmed live, pick one only if you know it will be running, or enter a model id manually.");
      } else {
        // Neither presets nor LM Studio are reachable, fall back to a free-text field
        select.style.display = "none";
        if (custom) { custom.style.display = "block"; custom.placeholder = "model id (LM Studio unreachable)"; }
        setModelAvailabilityNote(connected ? "" :
          "No model server reachable" + (lmStudioUrl ? " at " + lmStudioUrl : "") + ".");
      }
    }

    function onAgentModelPreset(value) {
      const custom = document.getElementById("pf-agent-model");
      if (value === "custom") { custom.style.display = "block"; return; }
      custom.style.display = "none";
      const [model, effort] = value.split("|");
      custom.dataset.model = model || "";
      custom.dataset.effort = effort || "";
    }

    async function submitProject(event) {
      event.preventDefault();
      const project = await api("POST", "/api/projects", {
        name: document.getElementById("pf-name").value,
        repo_url: document.getElementById("pf-repo").value,
        model_name: document.getElementById("pf-model").value,
        agent_model: (document.getElementById("pf-agent-model").style.display === "block"
          ? document.getElementById("pf-agent-model").value
          : document.getElementById("pf-agent-model").dataset.model) || null,
        agent_model_effort: document.getElementById("pf-agent-model").dataset.effort || null,
        harness: document.getElementById("project-harness")?.value || null
      });
      selectedProjectId = project.id;
      showingForm = false;
      await loadProjects();
    }

    async function deleteProject(id) {
      if (!confirm("Delete this project?")) return;
      await api("DELETE", "/api/projects/" + id);
      selectedProjectId = null;
      selectedPlanId = null;
      await loadProjects();
    }

    function renderPlansView() {
      const container = document.getElementById("view-container");
      const visible = visiblePlans();
      const masterRows = visible.map(plan =>
        '<button class="master-row' + (selectedPlanId === plan.id ? ' selected' : '') +
        '" type="button" onclick="selectPlan(\'' + esc(plan.id) + '\')">' +
          '<div class="row-main"><div class="row-name">' + esc(planLabel(plan)).slice(0, 100) + '</div>' +
          '<div class="row-meta">' + esc(plan.projectName) + '</div></div>' + badge(plan.status) +
        '</button>'
      ).join("") || '<div class="empty-list">No plans</div>';
      const detail = showingForm ? renderPlanForm() :
        selectedPlanId ? renderPlanDetail(plans.find(plan => plan.id === selectedPlanId)) :
        '<div class="detail-empty">Select a plan</div>';

      container.innerHTML =
        '<div class="master-panel">' +
          '<div class="master-header"><div class="master-title">' + visible.length + ' Plans</div>' +
          '<button class="btn btn-compact" type="button" onclick="loadPlans()">Refresh</button></div>' +
          '<div class="master-list">' + masterRows + '</div>' +
        '</div><div class="detail-panel">' + detail + '</div>';
    }

    function selectPlan(id) {
      selectedPlanId = id;
      showingForm = false;
      renderPlansView();
    }

    function renderPlanDetail(plan) {
      if (!plan) return '<div class="detail-empty">Plan not found</div>';
      // A plan wedged behind a terminally FAILED dependency renders as a
      // perfectly healthy `active` badge with no error anywhere, because it IS
      // active and its `error` column IS null -- the engine leaves it that way
      // on purpose so a human can still retry. Both fields are derived by the
      // server (`plan_reachability`), never recomputed here: a second copy of
      // the rule in JS is how the dashboard ends up disagreeing with `praxis
      // plans` about the same plan. `|| []` because a server older than the
      // fields sends neither, and absent must read as "nothing to say".
      const stalledIds = plan.stalled_task_ids || [];
      const stalledBlockers = plan.stalled_blocked_by_task_ids || [];
      // The BLOCKERS are named, not the blocked leaves: only a failed task can
      // be retried, so those are the ids a reader can act on.
      const stalledRow = stalledIds.length ?
        '<div class="detail-field"><span class="field-label">Stalled</span><span class="field-value">' +
          esc(stalledIds.length + (stalledIds.length === 1 ? " task" : " tasks") +
              " can never be dispatched: a dependency failed terminally and the loop never revisits it. " +
              (stalledBlockers.length ? "Retry a blocker to release them:" : "")) +
          (stalledBlockers.length ? ' ' + stalledBlockers.map(mono).join(", ") : "") +
        '</span></div>' : "";
      // Where this plan's work actually IS. `PlanResponse` has carried
      // `integration_pr_url` / `integration_merged_at` since migration 9 and
      // this card rendered neither, so the dashboard could not tell "landed on
      // the base branch" from "sitting on the plan branch behind an unapproved
      // PR" -- the exact distinction those columns were added for, and the one
      // `praxis plans` already draws. Same three-way reading as the CLI's
      // `_status_cell`, deliberately, so the two surfaces cannot disagree.
      const integrationHref = plan.integration_pr_url ? safeHref(plan.integration_pr_url) : "";
      const prLink = integrationHref ?
        '<a href="' + integrationHref + '" target="_blank" rel="noopener">' + esc(plan.integration_pr_url) + '</a>' : "";
      let integrationText;
      if (plan.integration_merged_at) {
        integrationText = esc("merged at " + plan.integration_merged_at + ", so this work is on the base branch: ");
      } else if (plan.integration_pr_url) {
        integrationText = esc("open, so this work is still on the plan branch: ");
      } else if (plan.status === "completed") {
        // `plans.error` is a ONE-WAY signal: a recorded reason means the PR
        // could not be opened and the work is stranded, an absent one proves
        // nothing, so both readings are named rather than either asserted.
        integrationText = plan.error ?
          esc("no integration PR. The server recorded: " + plan.error) :
          esc("no integration PR, and no reason was recorded. Either the work already reached the base branch (its task PRs were merged, or every task was a no-op), or it is on the plan branch and did not.");
      } else {
        integrationText = esc("no integration PR: it is opened when a plan completes, and this plan is " + (plan.status || "in an unreported state") + ".");
      }
      const integrationRow =
        '<div class="detail-field"><span class="field-label">Integration</span><span class="field-value">' +
        integrationText + prLink + '</span></div>';
      const approveReject = (plan.status === "pending" && plan.source === "autonomous") ?
        '<button class="btn btn-primary" type="button" onclick="approvePlan(\'' + esc(plan.id) + '\')">Approve</button>' +
        '<button class="btn btn-danger" type="button" onclick="rejectPlan(\'' + esc(plan.id) + '\')">Reject</button>' : "";
      return '<div class="detail-header"><div class="detail-title">' + esc(plan.projectName) + '</div>' +
        '<div class="detail-subtitle">Source: ' + esc(plan.source) +
        (plan.confidence != null ? ' &middot; Confidence: ' + Math.round(plan.confidence * 100) + '%' : ' &middot; Confidence: n/a (pending review)') +
        '</div></div>' +
        '<div class="detail-content">' +
          // `plans.spec` was dropped in Spec 2: the markdown documents in the
          // repository are the source of truth and the DB is a thin execution
          // ledger holding only their paths. A "Specification" heading over
          // `plan.spec` therefore promised text this row cannot have and
          // rendered an empty card under the promise. Name what the row DOES
          // know, and say where the text lives.
          '<div class="detail-section"><div class="detail-section-title">Documents</div>' +
          '<div class="detail-card">' +
          '<div class="detail-field"><span class="field-label">Spec</span><span class="field-value">' +
          (plan.spec_path ? mono(plan.spec_path) : esc("not recorded")) + '</span></div>' +
          '<div class="detail-field"><span class="field-label">Plan</span><span class="field-value">' +
          (plan.plan_path ? mono(plan.plan_path) : esc("not recorded")) + '</span></div>' +
          '<div class="detail-field"><span class="field-label">Where</span><span class="field-value">' +
          esc("in the project repository; the Spec and Plan tabs render them") + '</span></div>' +
          '</div></div>' +
          (plan.opus_plan ? renderOpusPlan(plan.opus_plan, plan.id) : "") +
          '<div class="detail-section"><div class="detail-section-title">Status</div><div class="detail-card">' +
          '<div class="detail-field"><span class="field-label">Status</span><span class="field-value">' + badge(plan.status) + '</span></div>' +
          '<div class="detail-field"><span class="field-label">Branch</span><span class="field-value">' + mono(plan.plan_branch_name) + '</span></div>' +
          '<div class="detail-field"><span class="field-label">Created</span><span class="field-value">' + esc(plan.created_at) + '</span></div>' +
          integrationRow +
          stalledRow +
          '</div></div></div>' +
          (approveReject ? '<div class="detail-actions">' + approveReject + '</div>' : "");
    }

    function renderPlanForm() {
      const opts = projects.map(project => '<option value="' + esc(project.id) + '">' + esc(project.name) + '</option>').join("");
      return '<div class="detail-header"><div class="detail-title">Submit Spec</div>' +
        '<div class="detail-subtitle">Create a new plan from a specification</div></div>' +
        '<div class="detail-content"><form onsubmit="submitPlan(event)">' +
          '<div class="formrow"><label>Project</label><select id="plf-project" required>' + opts + '</select></div>' +
          '<div class="formrow"><label>Specification</label><textarea id="plf-spec" required></textarea></div>' +
          '<div class="form-actions"><button type="submit" class="btn btn-primary">Submit</button>' +
          '<button type="button" class="btn" onclick="showingForm=false;renderPlansView()">Cancel</button></div>' +
        '</form></div>';
    }

    async function submitPlan(event) {
      event.preventDefault();
      const projectId = document.getElementById("plf-project").value;
      const plan = await api("POST", "/api/projects/" + projectId + "/plans", {
        spec: document.getElementById("plf-spec").value
      });
      selectedPlanId = plan.id;
      showingForm = false;
      await loadPlans();
    }

    let opusPlanViewMode = {};

    function renderOpusPlan(raw, planId) {
      const mode = opusPlanViewMode[planId] || "pretty";
      const toggleHtml = '<div style="display:flex;align-items:center;justify-content:space-between;">' +
        '<div class="detail-section-title" style="margin-bottom:0;">Opus Plan</div>' +
        '<div class="json-toggle">' +
          '<button class="json-toggle-btn' + (mode === "pretty" ? " active" : "") + '" type="button" onclick="toggleOpusPlanView(\'' + esc(planId) + '\',\'pretty\')">Pretty</button>' +
          '<button class="json-toggle-btn' + (mode === "raw" ? " active" : "") + '" type="button" onclick="toggleOpusPlanView(\'' + esc(planId) + '\',\'raw\')">Raw</button>' +
        '</div></div>';
      let content;
      if (mode === "raw") {
        let formatted;
        try { formatted = JSON.stringify(JSON.parse(raw), null, 2); } catch (e) { formatted = raw; }
        content = '<div class="detail-card" style="white-space:pre-wrap;font-size:12px;font-family:Cascadia Mono, SF Mono, Consolas, monospace;line-height:1.5;overflow-x:auto;">' + esc(formatted) + '</div>';
      } else {
        content = renderOpusPlanPretty(raw);
      }
      return '<div class="detail-section">' + toggleHtml + '<div style="margin-top:10px;">' + content + '</div></div>';
    }

    function renderOpusPlanPretty(raw) {
      let data;
      try { data = JSON.parse(raw); } catch (e) { return '<div class="detail-card" style="white-space:pre-wrap;font-size:13px;">' + esc(raw) + '</div>'; }
      let html = '<div class="detail-card opus-plan-pretty">';
      if (data.plan_summary) html += '<div class="op-summary">' + esc(data.plan_summary) + '</div>';
      if (data.tasks && data.tasks.length) {
        html += '<div class="detail-section-title" style="margin-bottom:8px;">Tasks (' + data.tasks.length + ')</div>';
        data.tasks.forEach(function(task, i) {
          html += '<div class="op-task">';
          html += '<div class="op-task-title">' + (i + 1) + '. ' + esc(task.title) + '</div>';
          if (task.slug) html += '<div class="op-task-slug">' + esc(task.slug) + '</div>';
          if (task.description) html += '<div class="op-task-desc">' + esc(task.description) + '</div>';
          if (task.depends_on && task.depends_on.length) html += '<div class="op-task-deps">Depends on: ' + task.depends_on.map(esc).join(", ") + '</div>';
          html += '</div>';
        });
      }
      html += '</div>';
      return html;
    }

    function toggleOpusPlanView(planId, mode) {
      opusPlanViewMode[planId] = mode;
      renderPlansView();
    }

    async function approvePlan(id) { await api("POST", "/api/plans/" + id + "/approve"); await loadPlans(); }
    async function rejectPlan(id) { await api("POST", "/api/plans/" + id + "/reject"); await loadPlans(); }

    async function renderTasks() {
      if (!plans.length) await loadPlans();
      const container = document.getElementById("view-container");
      const visible = visiblePlans();
      if (!visible.some(plan => plan.id === selectedPlanId)) selectedPlanId = visible[0]?.id || null;
      const planOpts = visible.map(plan =>
        '<option value="' + esc(plan.id) + '"' + (selectedPlanId === plan.id ? " selected" : "") + '>' +
        esc(plan.projectName) + ": " + esc(planLabel(plan)).slice(0, 56) + '</option>'
      ).join("");

      container.innerHTML =
        '<div class="master-panel">' +
          '<div class="master-header"><div class="master-title">Tasks</div>' +
          '<button class="btn btn-compact" type="button" onclick="renderTasks()">Refresh</button></div>' +
          '<div style="padding:12px 20px;border-bottom:1px solid var(--border-subtle);"><label>Plan</label>' +
          '<select id="task-plan-select" onchange="onTaskPlanChange()">' + planOpts + '</select></div>' +
          '<div class="master-list" id="tasks-master-list"></div>' +
        '</div><div class="detail-panel" id="task-detail-panel"><div class="detail-empty">Select a task</div></div>';

      const planId = selectedPlanId || visible[0]?.id;
      if (planId) {
        selectedPlanId = planId;
        await loadTasksForPlan(planId);
      } else {
        document.getElementById("tasks-master-list").innerHTML = '<div class="empty-list">No plans</div>';
      }
    }

    function onTaskPlanChange() {
      selectedPlanId = document.getElementById("task-plan-select").value;
      selectedTaskId = null;
      loadTasksForPlan(selectedPlanId);
    }

    async function loadTasksForPlan(planId) {
      currentTasks = await api("GET", "/api/plans/" + planId + "/tasks");
      const list = document.getElementById("tasks-master-list");
      if (!list) return;
      list.innerHTML = currentTasks.map(task =>
        '<button class="master-row' + (selectedTaskId === task.id ? ' selected' : '') +
        '" type="button" onclick="selectTask(\'' + esc(task.id) + '\')">' +
          '<div class="row-main"><div class="row-name">' + esc(task.title) + '</div>' +
          '<div class="row-meta">' + esc(task.branch_name) + '</div></div>' + badge(task.status) +
        '</button>'
      ).join("") || '<div class="empty-list">No tasks for this plan</div>';
    }

    function selectTask(id) {
      selectedTaskId = id;
      const task = currentTasks.find(item => item.id === id);
      document.querySelectorAll("#tasks-master-list .master-row").forEach(row => row.classList.remove("selected"));
      const rows = Array.from(document.querySelectorAll("#tasks-master-list .master-row"));
      const index = currentTasks.findIndex(item => item.id === id);
      if (rows[index]) rows[index].classList.add("selected");
      renderTaskDetail(task);
    }

    function renderTaskDetail(task) {
      const panel = document.getElementById("task-detail-panel");
      if (!panel) return;
      if (!task) {
        panel.innerHTML = '<div class="detail-empty">Task not found</div>';
        return;
      }
      const stopBtn = task.status === "in_progress" ?
        '<button class="btn btn-danger" type="button" onclick="stopTask(\'' + esc(task.id) + '\')">Stop</button>' : "";
      panel.innerHTML =
        '<div class="detail-header"><div class="detail-title">' + esc(task.title) + '</div>' +
        '<div class="detail-subtitle">' + esc(task.branch_name) + '</div></div>' +
        '<div class="detail-content">' +
          '<div class="detail-section"><div class="detail-section-title">Configuration</div><div class="detail-card">' +
          '<div class="detail-field"><span class="field-label">Status</span><span class="field-value">' + badge(task.status) + '</span></div>' +
          '<div class="detail-field"><span class="field-label">Attempt</span><span class="field-value">' + esc(task.attempt) + '</span></div>' +
          '<div class="detail-field"><span class="field-label">PR</span><span class="field-value">' +
          (task.pr_url ? '<a href="' + esc(task.pr_url) + '" target="_blank" rel="noreferrer">View PR</a>' : "-") + '</span></div>' +
          '<div class="detail-field"><span class="field-label">Updated</span><span class="field-value">' + esc(task.updated_at) + '</span></div>' +
          '</div></div>' +
          (task.description ? '<div class="detail-section"><div class="detail-section-title">Description</div>' +
          '<div class="detail-card" style="white-space:pre-wrap;font-size:13px;line-height:1.6;">' + esc(task.description) + '</div></div>' : "") +
          (task.review_feedback ? '<div class="detail-section"><div class="detail-section-title">Review Feedback</div>' +
          '<div class="detail-card" style="white-space:pre-wrap;font-size:12px;font-family:Cascadia Mono, SF Mono, Consolas, monospace;line-height:1.5;">' + esc(task.review_feedback) + '</div></div>' : "") +
        '</div><div class="detail-actions">' + stopBtn +
        '<button class="btn" type="button" onclick="viewTaskLogs(\'' + esc(task.id) + '\')">View Logs</button></div>';
    }

    async function stopTask(id) {
      await api("POST", "/api/tasks/" + id + "/stop");
      await loadTasksForPlan(selectedPlanId);
      document.getElementById("task-detail-panel").innerHTML = '<div class="detail-empty">Select a task</div>';
    }

    function viewTaskLogs(taskId) {
      switchView("logs");
      setTimeout(() => connectTaskLogs(taskId), 100);
    }

    async function renderDocs(category) {
      const docs = await api("GET", "/api/docs?category=" + category);
      const rows = docs.map(d => {
        const pct = d.total_count ? Math.round(100 * d.done_count / d.total_count) : 0;
        const bar = category === "plan"
          ? '<div class="doc-bar"><div class="doc-bar-fill" style="width:' + pct + '%"></div></div>'
            + '<span class="doc-bar-label">' + d.done_count + '/' + d.total_count + '</span>'
          : "";
        const llm = d.classified_by === "haiku" ? ' <span class="doc-tag">haiku</span>' : "";
        return '<div class="row" onclick="openDoc(\'' + esc(d.path) + '\')">' +
          '<div class="row-main"><div class="row-name">' + esc(d.title || d.path) + '</div>' +
          '<div class="row-sub">' + esc(d.path) + llm + '</div></div>' + bar + '</div>';
      }).join("");
      const container = document.getElementById("view-container");
      container.style.display = "";
      container.innerHTML =
        '<div class="docs-list-col">' +
        '<div class="list-header"><h2>' + (category === "spec" ? "Specs" : "Plan Docs") + '</h2>' +
        '<button class="btn" type="button" onclick="refreshDocs()">Refresh</button></div>' +
        '<div class="list">' + (rows || '<div class="empty">No ' + category + ' docs found</div>') + '</div>' +
        '</div>';
    }

    async function refreshDocs() {
      await api("POST", "/api/docs/refresh");
      const v = currentView === "specs" ? "spec" : "plan";
      renderDocs(v);
    }

    async function openDoc(path) {
      const doc = await api("GET", "/api/docs/raw?path=" + encodeURIComponent(path));
      const isSpec = path.includes("/specs/");
      const container = document.getElementById("view-container");
      // Replace list area with a two-panel layout: list on left, editor on right
      const existingList = container.querySelector(".list");
      const existingHeader = container.querySelector(".list-header");
      // Remove any prior detail pane
      const prior = container.querySelector(".spec-detail-pane");
      if (prior) prior.remove();
      const pane = document.createElement("div");
      pane.className = "spec-detail-pane";
      pane.style.cssText = "flex:1;display:flex;flex-direction:column;overflow:hidden;background:var(--bg);border-left:1px solid var(--border-subtle);";
      const escapedPath = esc(path);
      pane.innerHTML =
        '<div class="detail-header" style="display:flex;align-items:center;justify-content:space-between;gap:12px;">' +
          '<div><div class="detail-title">' + escapedPath + '</div>' +
          '<div class="detail-subtitle">' + (isSpec ? "Spec document" : "Plan document") + '</div></div>' +
          '<button class="btn btn-compact" type="button" onclick="this.closest(\'.spec-detail-pane\').remove()">Close</button>' +
        '</div>' +
        '<div style="flex:1;overflow:auto;padding:12px 24px;">' +
          '<textarea id="spec-editor-content" style="width:100%;height:220px;resize:vertical;font-family:var(--font-mono,monospace);font-size:13px;background:var(--panel);color:var(--text);border:1px solid var(--border-subtle);border-radius:6px;padding:10px;box-sizing:border-box;">' +
            esc(doc.content) +
          '</textarea>' +
          (isSpec
            ? '<div style="display:flex;gap:8px;margin-top:8px;">' +
                '<button class="btn" type="button" onclick="saveSpec(\'' + escapedPath + '\')">Save</button>' +
              '</div>' +
              '<div style="margin-top:16px;border-top:1px solid var(--border-subtle);padding-top:12px;">' +
                '<div class="detail-subtitle" style="margin-bottom:6px;">Generate Plan</div>' +
                '<textarea id="spec-plan-notes" placeholder="Extra notes for the plan (optional)…" style="width:100%;height:72px;resize:vertical;font-family:var(--font-mono,monospace);font-size:13px;background:var(--panel);color:var(--text);border:1px solid var(--border-subtle);border-radius:6px;padding:8px;box-sizing:border-box;"></textarea>' +
                '<div style="margin-top:8px;">' +
                  '<button class="btn" type="button" onclick="createPlanFromSpec(\'' + escapedPath + '\')">Create Plan</button>' +
                '</div>' +
              '</div>'
            : '') +
        '</div>';
      // Make container flex so pane sits beside the list
      container.style.display = "flex";
      container.style.overflow = "hidden";
      container.style.flex = "1";
      if (existingList) existingList.style.overflowY = "auto";
      container.appendChild(pane);
    }

    async function saveSpec(specPath) {
      const content = document.getElementById("spec-editor-content").value;
      await api("POST", "/api/specs/modify", {
        project_id: selectedProjectId,
        spec_path: specPath,
        content: content,
      });
    }

    async function createPlanFromSpec(specPath) {
      const notes = (document.getElementById("spec-plan-notes") || {value: ""}).value;
      const btn = event.target;
      btn.disabled = true;
      btn.textContent = "Generating…";
      try {
        await api("POST", "/api/specs/plan", {
          project_id: selectedProjectId,
          spec_path: specPath,
          notes: notes,
        });
        btn.textContent = "Plan queued";
      } catch (e) {
        btn.disabled = false;
        btn.textContent = "Create Plan";
      }
    }

    function renderLogs() {
      const container = document.getElementById("view-container");
      const cleanActive = fullLogsMode === "clean" ? " active" : "";
      const rawActive = fullLogsMode === "raw" ? " active" : "";
      container.innerHTML =
        '<div class="master-panel">' +
          '<div class="master-header"><div class="master-title">Log Sources</div>' +
          '<button class="btn btn-compact" type="button" onclick="renderLogs()">Refresh</button></div>' +
          '<div class="master-list">' +
            '<button class="master-row selected" type="button" onclick="connectEvents()">' +
              '<div class="row-main"><div class="row-name">System Events</div><div class="row-meta">SSE</div></div>' +
            '</button>' + buildTaskLogSources() +
          '</div>' +
        '</div><div class="detail-panel">' +
          '<div class="detail-header" style="display:flex;align-items:center;justify-content:space-between;gap:12px;">' +
            '<div><div class="detail-title" id="log-title">System Events</div><div class="detail-subtitle">Server-sent event stream</div></div>' +
            '<div style="display:flex;gap:8px;align-items:center;">' +
              '<div class="log-mode-toggle" id="full-log-mode-toggle" style="display:none;">' +
                '<button class="log-mode-btn' + cleanActive + '" id="full-log-clean-btn" data-mode="clean" type="button" onclick="setFullLogsMode(\'clean\')">Clean</button>' +
                '<button class="log-mode-btn' + rawActive + '" id="full-log-raw-btn" data-mode="raw" type="button" onclick="setFullLogsMode(\'raw\')">Raw</button>' +
              '</div>' +
              '<button class="btn btn-compact" type="button" onclick="connectEvents()">Reconnect</button>' +
            '</div>' +
          '</div>' +
          '<div style="flex:1;overflow:hidden;padding:12px 24px 24px;"><div id="log-output" class="log"></div></div>' +
        '</div>';
      connectEvents();
    }

    function buildTaskLogSources() {
      return currentTasks
        .filter(task => task.status === "in_progress" || task.status === "reviewing")
        .map(task =>
          '<button class="master-row" type="button" onclick="connectTaskLogs(\'' + esc(task.id) + '\')">' +
            '<div class="row-main"><div class="row-name">' + esc(task.title) + '</div><div class="row-meta">' + esc(task.branch_name) + '</div></div>' +
            badge(task.status) +
          '</button>'
        ).join("");
    }

    let fullLogsRawBuffer = "";   // raw log text for the full-logs task view
    let fullLogsIsTask = false;   // true when the full-logs view is showing a task (vs system events)

    function appendLog(line) {
      const log = document.getElementById("log-output");
      if (!log) return;
      log.textContent += line + "\n";
      log.scrollTop = log.scrollHeight;
    }

    function renderFullLogsOutput() {
      const log = document.getElementById("log-output");
      if (!log) return;
      log.innerHTML = renderCuratedLog(fullLogsRawBuffer, fullLogsMode);
      log.scrollTop = log.scrollHeight;
    }

    function setFullLogsMode(mode) {
      fullLogsMode = mode;
      const cleanBtn = document.getElementById("full-log-clean-btn");
      const rawBtn = document.getElementById("full-log-raw-btn");
      if (cleanBtn) cleanBtn.classList.toggle("active", mode === "clean");
      if (rawBtn) rawBtn.classList.toggle("active", mode === "raw");
      renderFullLogsOutput();
    }

    function connectEvents() {
      const title = document.getElementById("log-title");
      if (title) title.textContent = "System Events";
      // Hide toggle for system events view (not curated)
      const toggle = document.getElementById("full-log-mode-toggle");
      if (toggle) toggle.style.display = "none";
      fullLogsIsTask = false;
      openSse("/api/events?token=" + encodeURIComponent(ensureToken()));
    }

    function connectTaskLogs(taskId) {
      const task = currentTasks.find(item => item.id === taskId);
      const title = document.getElementById("log-title");
      if (title) title.textContent = task ? task.title : "Task Logs";
      // Show mode toggle for task logs
      const toggle = document.getElementById("full-log-mode-toggle");
      if (toggle) toggle.style.display = "";
      fullLogsIsTask = true;
      fullLogsRawBuffer = "";
      openTaskSse("/api/tasks/" + taskId + "/logs?token=" + encodeURIComponent(ensureToken()));
    }

    function openSse(path) {
      if (eventSource) eventSource.close();
      const logEl = document.getElementById("log-output");
      if (logEl) logEl.textContent = "";
      eventSource = new EventSource(API + path);
      eventSource.onopen = () => appendLog("connected");
      eventSource.onerror = () => appendLog("connection error");
      eventSource.onmessage = event => appendLog(event.data);
      handleBrainstormSse(eventSource);
      ["plan_activated", "agent_dispatched", "review_completed", "improvement_proposed",
       "task_completed", "task_failed", "task_retry", "opus_queued", "log", "agent_log"].forEach(type => {
        eventSource.addEventListener(type, event => appendLog("[" + type + "] " + event.data));
      });
      eventSource.addEventListener("task_split", event => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch (error) {
          return;
        }
        if (!data) return;
        const leafCount = Array.isArray(data.child_slugs) ? data.child_slugs.length : "?";
        appendLog("Split task " + data.task_id + " into " + leafCount + " leaves: " + (data.reason || ""));
      });
      eventSource.addEventListener("task_escalated", event => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch (error) {
          return;
        }
        if (!data) return;
        appendLog("Escalated task " + data.task_id + " to " + data.to_harness + "/" + data.to_model + ": " + (data.reason || ""));
      });
    }

    // Task-specific SSE for the full-logs view — uses curated rendering
    function openTaskSse(path) {
      if (eventSource) eventSource.close();
      const logEl = document.getElementById("log-output");
      if (logEl) logEl.innerHTML = renderCuratedLog("", fullLogsMode);
      eventSource = new EventSource(API + path);
      eventSource.onerror = () => appendLog("connection error");
      handleBrainstormSse(eventSource);
      const handleLogChunk = (event) => {
        let d;
        try { d = JSON.parse(event.data); } catch (e) { return; }
        if (!d) return;
        const chunk = String(d.logs || d.line || "");
        if (chunk) {
          fullLogsRawBuffer += chunk;
          renderFullLogsOutput();
        }
      };
      eventSource.addEventListener("agent_log", handleLogChunk);
      eventSource.addEventListener("log", handleLogChunk);
      ["task_completed", "task_failed"].forEach(type => {
        eventSource.addEventListener(type, event => appendLog("[" + type + "] " + event.data));
      });
      // The server ends a finished task's log stream with a terminal "complete"
      // event. Close the EventSource so it does NOT auto-reconnect and re-replay
      // the already-rendered stored logs.
      eventSource.addEventListener("complete", () => {
        if (eventSource) { eventSource.close(); eventSource = null; }
      });
    }

    async function loadDashboard(options = {}) {
      await loadProjects();
      await loadPlans();
      const scoped = visiblePlans();
      const activePlans = scoped.filter(plan => plan.status === "active");
      const pendingPlans = scoped.filter(plan => plan.status === "pending");
      // PlanStatus has five members. Leaving failed and rejected out of the
      // fetch is what made a dead plan invisible on the one surface whose job
      // is showing what needs attention: with no tasks fetched, the failed
      // task count in renderDashboard was necessarily zero.
      const stoppedPlans = scoped.filter(plan => plan.status === "failed" || plan.status === "rejected");
      const completedPlans = scoped.filter(plan => plan.status === "completed").slice(0, COMPLETED_LANE_LIMIT);
      const plansToLoad = [...activePlans, ...pendingPlans, ...stoppedPlans, ...completedPlans];
      const currentPlanIds = new Set(plansToLoad.map(plan => String(plan.id)));
      for (const planId of Object.keys(dashboardTasks)) {
        if (!currentPlanIds.has(String(planId))) {
          delete dashboardTasks[planId];
          delete dashboardTasksReachable[planId];
        }
      }

      for (const plan of plansToLoad) {
        try {
          dashboardTasks[plan.id] = await api("GET", "/api/plans/" + plan.id + "/tasks");
          // Cleared on every success, or one transient failure would leave
          // the plan flagged unknown for the rest of the session.
          dashboardTasksReachable[plan.id] = true;
        } catch (error) {
          // The empty array is kept so every reader below can still iterate,
          // but it is NOT a measurement, and this flag is what says so.
          dashboardTasks[plan.id] = [];
          dashboardTasksReachable[plan.id] = false;
        }
      }

      renderDashboard();
      if (!options.skipSseReconnect) connectDashboardSse();
    }

    function renderDashboard() {
      if (!projects.length) {
        document.getElementById("view-container").innerHTML =
          '<div class="detail-empty" style="max-width:480px;margin:64px auto;text-align:center;padding:32px;">' +
          '<div style="font-weight:700;font-size:16px;margin-bottom:8px;">Welcome to Praxis</div>' +
          '<div style="color:var(--text-muted);font-size:13px;line-height:1.6;margin-bottom:20px;">' +
          'Praxis turns ideas into shipped code: write a <strong>Spec</strong>, Claude breaks it into a ' +
          '<strong>Plan</strong>, then agents implement it in a <strong>Run</strong> — reviewed and merged automatically.' +
          '<br><br>Start by connecting a repository as a project.</div>' +
          '<button class="btn btn-primary" type="button" onclick="switchView(\'projects\')">+ Create Project</button>' +
          '</div>';
        return;
      }
      const scoped = visiblePlans();
      const activePendingPlans = scoped.filter(plan => plan.status === "active" || plan.status === "pending");
      // A failed or rejected plan gets a lane of its own, immediately after
      // the live ones. It is finished, so it does not belong beside "active",
      // but it is not completed either and folding it into the collapsed
      // completed list would hide the one outcome someone has to act on.
      const stoppedPlans = scoped.filter(plan => plan.status === "failed" || plan.status === "rejected");
      const allCompleted = scoped.filter(plan => plan.status === "completed");
      const completedPlans = allCompleted.slice(0, COMPLETED_LANE_LIMIT);
      const attentionCount = scoped.filter(plan => plan.status === "pending").length +
        scoped.filter(plan => plan.status === "failed").length +
        Object.values(dashboardTasks).flat().filter(task => task.status === "failed").length;
      // The failed-task term is a SUM OVER FETCHES, so one fetch that never
      // answered silently subtracts from it. The count then reads as a
      // complete tally when it is a lower bound, and in the worst case the
      // badge disappears entirely from a failure rather than from calm.
      const attentionUnknown = Object.keys(dashboardTasks).some(planId => !tasksReachable(planId));
      const liveLanes = [...activePendingPlans, ...stoppedPlans];
      const lanesHtml = liveLanes.map(renderSwimLane).join("");
      const completedHtml = completedPlans.length ? completedPlans.map(renderCompletedLane).join("") : "";
      // The cap is kept, but never silently: a truncated list that says
      // nothing reads as the whole list.
      const hiddenCompleted = allCompleted.length - completedPlans.length;
      const truncationHtml = hiddenCompleted > 0 ?
        '<div class="lane-truncation">Showing the ' + completedPlans.length + ' most recent of ' +
        allCompleted.length + ' completed plans. ' + hiddenCompleted + ' more are in the Plans view.</div>' : "";
      const bodyHtml = liveLanes.length ?
        lanesHtml + completedHtml + truncationHtml :
        '<div class="dashboard-idle"><div>No active plans right now</div><div style="font-size:12px;margin-top:8px;">Submit a spec to start orchestration.</div></div>' +
        completedHtml + truncationHtml;
      const container = document.getElementById("view-container");
      container.innerHTML =
        '<div class="dashboard-shell">' +
          renderHealthBar(attentionCount, attentionUnknown) +
          '<div class="dashboard-body">' +
            '<div class="dashboard-lanes">' + bodyHtml + '</div>' +
            '<div class="side-panel" id="dashboard-side-panel"></div>' +
          '</div>' +
        '</div>';
      if (selectedDashboardTaskId) {
        const selectedTask = Object.values(dashboardTasks).flat().find(task => task.id === selectedDashboardTaskId);
        if (selectedTask) {
          openDashboardTask(selectedTask.id, selectedTask.plan_id);
        } else {
          selectedDashboardTaskId = null;
        }
      }
    }

    function renderHealthBar(attentionCount, attentionUnknown) {
      // These numbers come from the values pollStatus MEASURED, never from
      // the rendered text in the sidebar. Reading #stat-agents back out gave
      // Number("?") = NaN whenever the agent manager was unreachable, and the
      // index.html default 0 before the first poll had even run.
      const opusStatus = window.__opusStatus || "unknown";
      const agentText = measuredAgentCount == null ? "?" : String(measuredAgentCount);
      const queueText = measuredQueueCount == null ? "?" : String(measuredQueueCount);
      const agentTitle = measuredAgentCount == null ?
        ' title="not measured yet, or the agent manager could not be reached"' : "";
      // An unknown term makes the count a LOWER BOUND, so the badge says so
      // and is shown even at zero: "no failures found" and "we could not
      // finish looking" must not render identically.
      const attentionLabel = attentionUnknown ?
        attentionCount + "+? needs attention" : attentionCount + " needs attention";
      const attentionTitle = attentionUnknown ?
        ' title="at least this many; one or more plans\' tasks could not be loaded"' : "";
      const attentionBadge = (attentionCount > 0 || attentionUnknown) ?
        '<span class="health-attention"' + attentionTitle + '>' + esc(attentionLabel) + '</span>' : "";
      return '<div class="health-bar">' +
        '<div class="health-item"><span class="health-dot ' + esc(opusStatus) + '"></span><span>Opus ' + esc(opusStatus.replace("_", " ")) + '</span></div>' +
        '<div class="health-item"><span' + agentTitle + '>Agents: ' + esc(agentText) + '</span></div>' +
        '<div class="health-item"><span>Queue: ' + esc(queueText) + '</span></div>' +
        '<span class="health-spacer"></span>' + attentionBadge +
      '</div>';
    }

    // The one honest label for a plan row. `plans.spec` was DROPPED in Spec 2
    // and PlanResponse never carries it, so leading this chain with it was
    // dead code that read as the intended source; the branch and the document
    // paths are what the row actually knows.
    function planLabel(plan) {
      const raw = plan.plan_branch_name || plan.plan_path || plan.spec_path || "";
      const name = String(raw).replace(/^plan\//, "").replace(/\.md$/, "");
      return name || ("Plan " + String(plan.id || "").slice(0, 8));
    }

    // Did this plan's task fetch answer at all? A plan nobody has tried to
    // fetch reads as reachable, because the only thing that sets `false` is a
    // failed request; the comparison is `!== false` for the same reason
    // `setConnection` compares `connected_measured !== false`.
    function tasksReachable(planId) {
      return dashboardTasksReachable[planId] !== false;
    }

    // What an empty lane means depends on WHY it is empty, and the default
    // ("task cards will appear shortly") is a promise the loop only keeps for
    // a plan it is actually working on.
    function emptyLaneMessage(plan) {
      // "No tasks yet" and "we could not ask" are different facts, and every
      // branch below would read the empty array a failed fetch left behind as
      // a real, measured zero.
      if (!tasksReachable(plan.id)) {
        return "Could not load this plan's tasks. This lane is empty because the request failed, not because there is no work: the count is unknown.";
      }
      // An autonomous proposal has no tasks because nobody has approved it,
      // and none will ever appear until somebody does. Telling the operator
      // to wait points them away from the only thing that unblocks it.
      if (plan.status === "pending" && plan.source === "autonomous") {
        return "Awaiting your approval - no tasks are created until this proposal is approved.";
      }
      if (plan.status === "failed") return "This plan failed before any task was recorded.";
      if (plan.status === "rejected") return "This plan was rejected, so no tasks were created.";
      return "Planning in progress - task cards will appear shortly.";
    }

    function renderSwimLane(plan) {
      const tasks = (dashboardTasks[plan.id] || []).slice();
      // no_changes sits beside merged: the leaf is done and its work is in
      // the tree, it just did not need a commit to get there. An unlisted
      // status sorts to 99 and lands below `pending`, which would bury a
      // finished leaf under unstarted ones.
      //
      // needs_clarification sits beside passed, the other thing parked on a
      // person. It was the status this map OMITTED, so the one leaf nothing
      // but a human answering can advance sorted to 99 and sank to the bottom
      // of the lane, under work that had not started. Every TaskStatus value
      // has a rank here and tests/test_status_vocab.py parses this literal to
      // prove it, in both directions.
      const statusOrder = { merged: 0, no_changes: 1, passed: 2, needs_clarification: 3, reviewing: 4, in_progress: 5, failed: 6, superseded: 7, pending: 8 };
      tasks.sort((a, b) => (statusOrder[a.status] ?? 99) - (statusOrder[b.status] ?? 99));
      const isSpecExpanded = expandedSpecs.has(plan.id);
      const specPreview = esc(planLabel(plan)).slice(0, 80);
      const approvalActions = (plan.status === "pending" && plan.source === "autonomous") ?
        '<button class="btn btn-compact btn-primary" type="button" onclick="dashboardApprove(\'' + esc(plan.id) + '\')">Approve</button>' +
        '<button class="btn btn-compact btn-danger" type="button" onclick="dashboardReject(\'' + esc(plan.id) + '\')">Reject</button>' : "";
      const taskCards = tasks.length ? tasks.map(renderTaskCard).join("") :
        '<div class="card-meta" style="padding:2px 0;">' + emptyLaneMessage(plan) + '</div>';
      return '<section class="swim-lane">' +
        '<div class="lane-header">' +
          badge(plan.status) +
          '<div class="lane-spec">' + specPreview + '</div>' +
          '<div class="lane-meta">' + esc(plan.projectName || "unknown") + " - " + esc(plan.plan_branch_name || "-") + '</div>' +
          '<button class="btn btn-compact" type="button" onclick="toggleSpec(\'' + esc(plan.id) + '\')">' + (isSpecExpanded ? "Hide Spec" : "View Spec") + '</button>' +
          approvalActions +
        '</div>' +
        (isSpecExpanded ? '<div class="lane-spec-full"><div class="detail-card">' + renderPlanSpecBody(plan) + '</div></div>' : "") +
        '<div class="lane-cards">' + taskCards + '</div>' +
      '</section>';
    }

    // What belongs in the expanded "View Spec" box. `plans.spec` was dropped
    // in Spec 2, so there is no spec text on the row and the button used to
    // open a box containing `esc(undefined)`, which is "". The spec is a
    // DOCUMENT IN THE REPO; there are four honest states and a blank box is
    // none of them.
    function renderPlanSpecBody(plan) {
      if (!plan.spec_path) {
        return '<div class="detail-empty">No spec document is recorded for this plan.</div>';
      }
      const doc = dashboardSpecCache[plan.id];
      if (doc === undefined) {
        return '<div class="detail-empty">Reading ' + esc(plan.spec_path) + '&hellip;</div>';
      }
      if (doc === null) {
        return '<div class="detail-empty">Could not read ' + esc(plan.spec_path) +
          ' from the repository.</div>';
      }
      if (!doc.trim()) {
        return '<div class="detail-empty">' + esc(plan.spec_path) + ' is empty in the repository.</div>';
      }
      return renderMarkdown(doc);
    }

    // Fetch one plan's spec document through the SAME endpoint the lifecycle
    // view already uses to read a repo document, so this adds no new server
    // surface. A failure caches `null` rather than retrying on every render.
    async function loadPlanSpecDoc(planId) {
      if (dashboardSpecCache[planId] !== undefined) return;
      const plan = plans.find(item => item.id === planId);
      // No path means there is nothing to fetch, and renderPlanSpecBody says
      // so from `spec_path` alone; caching a null here would blame the read.
      if (!plan || !plan.spec_path) return;
      try {
        const doc = await api("GET", "/api/projects/" + plan.project_id +
          "/doc-raw?path=" + encodeURIComponent(plan.spec_path));
        dashboardSpecCache[planId] = String(doc.content ?? "");
      } catch (error) {
        dashboardSpecCache[planId] = null;
      }
      // The read can land after the reader has navigated away, and
      // renderDashboard writes the shared #view-container unconditionally.
      // Every other late-arriving handler in this file takes the same guard.
      if (currentView === "dashboard") renderDashboard();
    }

    function renderTaskCard(task) {
      const isSelected = selectedDashboardTaskId === task.id;
      const isPulsing = task.status === "in_progress" || task.status === "reviewing";
      const pulseClass = task.status === "reviewing" ? "amber" : "green";
      const retryAction = task.status === "failed" ?
        '<button class="btn btn-compact btn-danger" type="button" onclick="event.stopPropagation();dashboardRetry(' + jsArg(task.id) + ')">Retry</button>' : "";
      const prHref = task.pr_url ? safeHref(task.pr_url) : "";
      const prLink = prHref ?
        '<a href="' + prHref + '" target="_blank" rel="noreferrer" onclick="event.stopPropagation()">PR</a>' : "";
      const mergeActions = task.status === "passed"
        ? '<div class="card-actions" onclick="event.stopPropagation()">' +
          '<button class="btn-approve" onclick="approveMerge(' + jsArg(task.id) + ')">Approve merge</button>' +
          '<button class="btn-reject" onclick="rejectTask(' + jsArg(task.id) + ')">Reject</button>' +
          '</div>'
        : "";
      const logLine = dashboardTaskLogs[task.id] ? '<div class="card-log-line">' + esc(stripAnsi(dashboardTaskLogs[task.id])) + '</div>' : "";
      return '<article class="task-card status-' + esc(task.status) + (isSelected ? " selected" : "") +
        '" data-task-id="' + esc(task.id) + '" onclick="openDashboardTask(' + jsArg(task.id) + ',' + jsArg(task.plan_id) + ')">' +
        '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">' +
          badge(task.status) +
          (isPulsing ? '<span class="pulse-dot ' + pulseClass + '"></span>' : "") +
        '</div>' +
        '<div class="card-title">' + esc(task.title) + '</div>' +
        '<div class="card-meta">' + esc(task.branch_name || "-") + '</div>' +
        '<div class="card-meta" style="display:flex;align-items:center;gap:8px;margin-top:6px;">' + retryAction + prLink + '</div>' +
        mergeActions +
        logLine +
      '</article>';
    }

    function renderCompletedLane(plan) {
      const isExpanded = expandedCompletedPlans.has(plan.id);
      const tasks = dashboardTasks[plan.id] || [];
      // Counting only `merged` against every task renders "1/3 merged" under
      // a COMPLETED badge for a plan whose other two leaves were `no_changes`
      // and `superseded`, which reads as two tasks that did not land. Both are
      // in the engine's SATISFIED_STATUSES and are what LET the plan complete;
      // `no_changes` is not rare here, because task 1 routinely writes task 2's
      // file. The word changes with the numerator: a no_changes leaf never
      // produced a commit, so "merged" would be wrong for it either way.
      const landedCount = tasks.filter(task => SATISFIED_TASK_STATUSES.includes(task.status)).length;
      // Widening the numerator must not swallow a real failure, so failures
      // are named separately rather than left as the arithmetic remainder.
      const failedCount = tasks.filter(task => task.status === "failed").length;
      const tallyText = tasksReachable(plan.id) ?
        landedCount + "/" + tasks.length + " landed" + (failedCount ? " &middot; " + failedCount + " failed" : "") :
        "tasks unavailable";
      const tallyTitle = tasksReachable(plan.id) ?
        ' title="landed = merged, no_changes or superseded: the work is in the tree"' :
        ' title="this plan\'s tasks could not be loaded, so the tally is unknown, not zero"';
      const specPreview = esc(planLabel(plan)).slice(0, 80);
      return '<section class="swim-lane completed-lane' + (isExpanded ? " expanded" : "") + '">' +
        '<div class="completed-header" onclick="toggleCompletedPlan(\'' + esc(plan.id) + '\')">' +
          '<span class="disclosure' + (isExpanded ? " open" : "") + '">></span>' +
          '<span class="badge completed">completed</span>' +
          '<span class="lane-spec">' + specPreview + '</span>' +
          '<span class="completed-meta"' + tallyTitle + '>' + tallyText + '</span>' +
          '<span class="completed-meta">' + esc(timeAgo(plan.created_at)) + '</span>' +
        '</div>' +
        (isExpanded ? '<div class="lane-cards">' + (tasks.length ? tasks.map(renderTaskCard).join("") :
          '<div class="card-meta">' + (tasksReachable(plan.id) ? "No tasks" :
            "Could not load this plan's tasks.") + '</div>') + '</div>' : "") +
      '</section>';
    }

    function timeAgo(isoString) {
      if (!isoString) return "-";
      const then = new Date(isoString).getTime();
      if (Number.isNaN(then)) return "-";
      const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
      if (seconds < 60) return seconds + "s ago";
      const minutes = Math.floor(seconds / 60);
      if (minutes < 60) return minutes + "m ago";
      const hours = Math.floor(minutes / 60);
      if (hours < 24) return hours + "h ago";
      const days = Math.floor(hours / 24);
      return days + "d ago";
    }

    // ── Live Log: classifier ───────────────────────────────────────────────
    function classifyLogLine(line) {
      const t = line.trim();
      if (!t) return null;
      // Phase headers: lines surrounded by === or ---
      if (/^={3,}.*={3,}$/.test(t) || /^-{3,}.*-{3,}$/.test(t)) return "phase";
      if (/^Cloning into\b/.test(t)) return "phase";
      if (/^Switched to\b/.test(t)) return "phase";
      if (/^branch '/.test(t)) return "phase";
      // File edit lines
      if (/^Applied edit to /.test(t)) return "file";
      if (/^Added .+(to the chat|to \.gitignore)/.test(t)) return "file";
      // Commit lines
      if (/^Commit [0-9a-f]/.test(t)) return "commit";
      // Test / error result lines (conservative: real test-runner summaries
      // and tracebacks, NOT arbitrary code that mentions "failed"/"error").
      if (/^\d+ (passed|failed|error)/i.test(t)) return "test";           // pytest summary
      if (/\b\d+ (passed|failed)\b/i.test(t) && /\b(test|spec)/i.test(t)) return "test";
      if (/^(PASS|FAIL|FAILED|ERROR)\b/.test(t)) return "test";           // jest/pytest line
      if (/^ok \d/.test(t)) return "test";                                // TAP "ok 1 - ..."
      if (/^(Tests?:|Test Suites:)/.test(t)) return "test";               // jest footer
      if (/^Traceback \(most recent call last\)/.test(t)) return "test";
      if (/^E\s+\w+Error\b/.test(t)) return "test";                       // pytest error line
      // Info lines
      if (/^(Aider v|Model:|Git repo:|Repo-map:|Tokens:|Warning:)/.test(t)) return "info";
      return null;
    }

    // Heuristic: does this raw line look like source code (vs natural-language
    // reasoning)? Used so reasoning is shown while code collapses. Receives the
    // UN-trimmed line so leading indentation is available as a signal.
    function looksLikeCode(rawLine) {
      const t = rawLine.trim();
      if (!t) return false;
      // Indented lines are almost always code in this stream.
      if (/^\s{2,}/.test(rawLine) || /^\t/.test(rawLine)) return true;
      // Common code-line openers / closers.
      if (/^(import |from |export |const |let |var |function |class |def |async |await |return |public |private |protected |interface |type |enum |package |module|namespace|#include|#define|#!|@|\/\/|\/\*|\*|\}|\)|\]|<\/|<[A-Za-z])/.test(t)) return true;
      // Code-ish symbols / operators.
      if (/[{};]|=>|===|!==|::|\)\s*\{|\(\)|\bself\.|\bconsole\.|\brequire\(/.test(t)) return true;
      // Assignment / key-value that is NOT a normal sentence.
      if (/^[\w.$\[\]"']+\s*[:=]\s*\S/.test(t) && !/^\S+\s+\S+\s+\S+\s+\S+/.test(t)) return true;
      // Ends with code punctuation.
      if (/[;{(,]$/.test(t)) return true;
      // Bare file path / filename on its own line (Aider echoes these in
      // lists) — not reasoning, fold into the collapsed code block.
      if (/^\S+$/.test(t) && (t.includes("/") || /\.\w{1,5}$/.test(t))) return true;
      return false;
    }

    // ── Live Log: shared curated-render helper ─────────────────────────────
    // Returns an HTML string. mode: "clean" | "raw".
    function renderCuratedLog(rawText, mode) {
      if (!rawText) return '<span style="color:var(--text-faint);font-style:italic;">Waiting for output&hellip;</span>';
      rawText = stripAnsi(rawText);
      if (mode === "raw") {
        return esc(rawText);
      }
      // Clean mode: classify lines, collapse null runs
      const lines = rawText.split(/\r?\n/);
      let html = "";
      let nullRun = 0; // count of consecutive non-blank null lines

      const flushNullRun = () => {
        if (nullRun > 0) {
          html += '<span class="log-line-collapse">&middot;&middot;&middot; writing code (' + nullRun + ' line' + (nullRun !== 1 ? "s" : "") + ') &middot;&middot;&middot;</span>\n';
          nullRun = 0;
        }
      };

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) {
          // blank: skip WITHOUT flushing so interspersed blanks (Aider
          // double-spaces its output) don't fragment one code block into
          // many tiny "writing code (1 line)" markers.
          continue;
        }
        const cat = classifyLogLine(trimmed);
        if (cat === null) {
          if (looksLikeCode(line)) {
            // accumulate into one merged "writing code" marker
            nullRun++;
            continue;
          }
          // natural-language reasoning: flush any pending code block, then show it
          flushNullRun();
          html += '<span class="log-line-prose">' + esc(trimmed) + '</span>\n';
          continue;
        }
        flushNullRun();
        let glyph, cls;
        switch (cat) {
          case "phase":
            glyph = "&#10003; "; cls = "log-line-phase"; break;
          case "file":
            glyph = "&#8594; "; cls = "log-line-file"; break;
          case "commit":
            glyph = "&#9679; "; cls = "log-line-commit"; break;
          case "test":
            if (/passed|PASS/i.test(trimmed) && !/failed|FAIL/i.test(trimmed)) {
              glyph = "&#10003; "; cls = "log-line-test-pass";
            } else if (/failed|FAIL|Traceback|Error:/i.test(trimmed)) {
              glyph = "&#10007; "; cls = "log-line-test-fail";
            } else {
              glyph = "&#8505; "; cls = "log-line-info";
            }
            break;
          case "info":
            glyph = "&#8505; "; cls = "log-line-info"; break;
          default:
            glyph = ""; cls = "";
        }
        html += '<span class="' + cls + '">' + glyph + esc(trimmed) + '</span>\n';
      }
      flushNullRun();
      return html || '<span style="color:var(--text-faint);font-style:italic;">Waiting for output&hellip;</span>';
    }

    // ── Live Log: render into side-panel log container ─────────────────────
    function renderLiveLog(taskId) {
      const container = document.getElementById("dashboard-log-tail");
      if (!container) return;
      const raw = dashboardTaskRawLog[taskId] || "";
      container.innerHTML = renderCuratedLog(raw, liveLogMode);
      container.scrollTop = container.scrollHeight;
    }

    // Toggle mode and re-render
    function setLiveLogMode(mode) {
      liveLogMode = mode;
      document.querySelectorAll(".log-mode-btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.mode === mode);
      });
      if (selectedDashboardTaskId) renderLiveLog(selectedDashboardTaskId);
    }

    function openDashboardTask(taskId, planId) {
      const task = (dashboardTasks[planId] || []).find(item => item.id === taskId);
      if (!task) {
        selectedDashboardTaskId = null;
        document.querySelectorAll(".task-card").forEach(el => el.classList.remove("selected"));
        return;
      }
      selectedDashboardTaskId = taskId;
      document.querySelectorAll(".task-card").forEach(el => {
        el.classList.toggle("selected", el.dataset.taskId === taskId);
      });
      renderSidePanel(task);
      // Open dedicated SSE to replay stored history + stream live log
      if (sidePanelLogSource) { sidePanelLogSource.close(); sidePanelLogSource = null; }
      if (!dashboardTaskRawLog[taskId]) dashboardTaskRawLog[taskId] = "";
      const src = new EventSource(API + "/api/tasks/" + taskId + "/logs?token=" + encodeURIComponent(ensureToken()));
      sidePanelLogSource = src;
      // Each (re)connection replays the full stored log from the start, so
      // reset the buffer on open to prevent duplication if the stream reconnects.
      src.onopen = () => { dashboardTaskRawLog[taskId] = ""; };
      src.addEventListener("agent_log", event => {
        let d;
        try { d = JSON.parse(event.data); } catch (e) { return; }
        if (!d) return;
        const chunk = String(d.logs || d.line || "");
        if (chunk) {
          dashboardTaskRawLog[taskId] = (dashboardTaskRawLog[taskId] || "") + chunk;
          if (String(selectedDashboardTaskId) === String(taskId)) renderLiveLog(taskId);
        }
      });
      src.addEventListener("log", event => {
        let d;
        try { d = JSON.parse(event.data); } catch (e) { return; }
        if (!d) return;
        const chunk = String(d.logs || d.line || "");
        if (chunk) {
          dashboardTaskRawLog[taskId] = (dashboardTaskRawLog[taskId] || "") + chunk;
          if (String(selectedDashboardTaskId) === String(taskId)) renderLiveLog(taskId);
        }
      });
      src.onerror = () => {};
    }

    function renderSidePanel(task) {
      const panel = document.getElementById("dashboard-side-panel");
      if (!panel) return;
      const stopBtn = task.status === "in_progress" ?
        '<button class="btn btn-danger" type="button" onclick="dashboardStop(' + jsArg(task.id) + ')">Stop</button>' : "";
      const retryBtn = task.status === "failed" ?
        '<button class="btn btn-danger" type="button" onclick="dashboardRetry(' + jsArg(task.id) + ')">Retry</button>' : "";
      const logsBtn = '<button class="btn" type="button" onclick="viewTaskLogs(' + jsArg(task.id) + ')">View Full Logs</button>';
      const panelPrHref = task.pr_url ? safeHref(task.pr_url) : "";
      const prLink = panelPrHref ? '<a href="' + panelPrHref + '" target="_blank" rel="noreferrer">View PR</a>' : "—";
      const metaStrip =
        'Attempt ' + esc(task.attempt) +
        ' &middot; PR ' + prLink +
        (task.updated_at ? ' &middot; updated ' + esc(task.updated_at) : '');
      const descAccordion = task.description
        ? '<details class="side-panel-accordion side-panel-section">' +
            '<summary>Description</summary>' +
            '<div class="side-panel-block">' + esc(task.description) + '</div>' +
          '</details>'
        : "";
      const reviewAccordion = task.review_feedback
        ? '<details class="side-panel-accordion side-panel-section">' +
            '<summary>Review Feedback</summary>' +
            '<div class="side-panel-block">' + esc(task.review_feedback) + '</div>' +
          '</details>'
        : "";
      const activeMode = liveLogMode;
      const cleanActive = activeMode === "clean" ? " active" : "";
      const rawActive = activeMode === "raw" ? " active" : "";
      panel.innerHTML =
        '<div class="side-panel-header">' +
          '<div class="side-panel-title-wrap">' +
            '<div class="side-panel-title">' + esc(task.title) + '</div>' +
            '<div class="side-panel-branch">' + esc(task.branch_name || "-") + '</div>' +
            '<div class="side-panel-header-badge">' + badge(task.status) + '</div>' +
          '</div>' +
          '<button class="side-panel-close" type="button" onclick="closeDashboardPanel()">&#x2715;</button>' +
        '</div>' +
        '<div class="side-panel-content">' +
          '<div class="side-panel-meta-strip">' + metaStrip + '</div>' +
          descAccordion +
          reviewAccordion +
          '<div class="side-panel-log-section">' +
            '<div class="side-panel-log-header">' +
              '<span class="side-panel-section-title" style="margin:0;">Live Activity</span>' +
              '<div class="log-mode-toggle">' +
                '<button class="log-mode-btn' + cleanActive + '" data-mode="clean" type="button" onclick="setLiveLogMode(\'clean\')">Clean</button>' +
                '<button class="log-mode-btn' + rawActive + '" data-mode="raw" type="button" onclick="setLiveLogMode(\'raw\')">Raw</button>' +
              '</div>' +
            '</div>' +
            '<div class="side-panel-log" id="dashboard-log-tail"></div>' +
          '</div>' +
        '</div>' +
        '<div class="side-panel-actions">' + stopBtn + retryBtn + logsBtn + '</div>';
      panel.classList.add("open");
      // Render any already-accumulated log (e.g. when re-opening a task)
      renderLiveLog(task.id);
    }

    function closeDashboardPanel() {
      selectedDashboardTaskId = null;
      if (sidePanelLogSource) { sidePanelLogSource.close(); sidePanelLogSource = null; }
      const panel = document.getElementById("dashboard-side-panel");
      if (panel) {
        panel.classList.remove("open");
        panel.innerHTML = "";
      }
      document.querySelectorAll(".task-card").forEach(el => el.classList.remove("selected"));
    }

    async function dashboardStop(taskId) {
      await api("POST", "/api/tasks/" + taskId + "/stop");
      const owningPlan = Object.entries(dashboardTasks).find(([, tasks]) => tasks.some(task => task.id === taskId));
      if (owningPlan) {
        const planId = owningPlan[0];
        dashboardTasks[planId] = await api("GET", "/api/plans/" + planId + "/tasks");
      }
      closeDashboardPanel();
      renderDashboard();
    }

    function connectDashboardSse() {
      if (dashboardSseSource && dashboardSseSource !== eventSource) {
        dashboardSseSource.close();
      }
      if (eventSource) eventSource.close();

      const source = new EventSource(API + "/api/events?token=" + encodeURIComponent(ensureToken()));
      eventSource = source;
      dashboardSseSource = source;

      handleBrainstormSse(source);

      source.onerror = () => {
        const healthBar = document.querySelector(".health-bar");
        if (!healthBar || healthBar.querySelector(".reconnecting")) return;
        const reconnecting = document.createElement("span");
        reconnecting.className = "reconnecting";
        reconnecting.style.cssText = "color:var(--badge-reviewing-text);font-size:11px;";
        reconnecting.textContent = "reconnecting...";
        healthBar.appendChild(reconnecting);
      };

      source.onopen = () => {
        const reconnecting = document.querySelector(".health-bar .reconnecting");
        if (reconnecting) reconnecting.remove();
      };

      source.addEventListener("agent_log", event => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch (error) {
          return;
        }

        const taskId = data?.task_id;
        if (!taskId) return;

        const logText = String(data.logs || data.line || "");
        const lines = logText.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
        if (lines.length) dashboardTaskLogs[taskId] = stripAnsi(lines[lines.length - 1]);

        document.querySelectorAll(".task-card").forEach(card => {
          if (card.dataset.taskId !== String(taskId)) return;
          let logLineEl = card.querySelector(".card-log-line");
          if (!logLineEl) {
            logLineEl = document.createElement("div");
            logLineEl.className = "card-log-line";
            card.appendChild(logLineEl);
          }
          logLineEl.textContent = dashboardTaskLogs[taskId] || "";
        });
        // Side-panel log is now driven by its own dedicated SSE (sidePanelLogSource).
      });

      ["task_completed", "task_failed"].forEach(type => {
        source.addEventListener(type, event => {
          try {
            const data = JSON.parse(event.data);
            if (data && data.task_id) delete dashboardTaskLogs[data.task_id];
          } catch (error) { /* ignore */ }
          if (currentView === "dashboard") void loadDashboard({ skipSseReconnect: true });
        });
      });
      ["plan_activated", "agent_dispatched", "review_completed", "task_retry", "improvement_proposed"].forEach(type => {
        source.addEventListener(type, () => {
          if (currentView === "dashboard") void loadDashboard({ skipSseReconnect: true });
        });
      });

      source.addEventListener("opus_queued", event => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch (error) {
          return;
        }
        if (data?.queued_count != null) {
          document.getElementById("stat-queue").textContent = String(data.queued_count);
          // Keep the value in step with the DOM, or the health bar shows the
          // older polled number until the next 5s tick.
          measuredQueueCount = data.queued_count;
        }
      });

      // A brain call hit a dead provider session — authoritative login signal
      // (the status poll can falsely report some CLIs as authenticated).
      source.addEventListener("provider_auth_required", event => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch (error) {
          return;
        }
        if (data?.provider) flagRuntimeAuthFailure(data.provider, data.login_hint);
      });

      source.addEventListener("task_needs_clarification", event => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch (error) {
          return;
        }
        if (data?.task_id) renderClarification(data.task_id, data.question, data.brain_note);
      });

      source.addEventListener("clarification_resolved", event => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch (error) {
          return;
        }
        if (data?.task_id) clearClarification(data.task_id);
      });

      // Rate-limited (every few hours) summary of work parked at the merge
      // gate. The badge itself is kept correct between events by the initial
      // pollPendingApprovals() call on page load.
      source.addEventListener("approvals_digest", event => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch (error) {
          return;
        }
        if (!data) return;
        // MERGE, never overwrite. The digest is assembled from a narrower
        // query than GET /api/approvals/pending, so a list it omits means
        // "not asked for", not "none left": assigning the event wholesale
        // deleted the autonomous proposals the poll had already fetched, and
        // the header went quiet over a live decision.
        pendingApprovals = mergeApprovals(pendingApprovals, data);
        renderApprovalsBadge();
        // The event is a hint; the endpoint is the truth. Re-fetch so the
        // merged view cannot keep a proposal that has since been decided.
        void pollPendingApprovals();
      });
    }

    function renderClarification(taskId, question, brainNote) {
      const el = document.querySelector('.task-card[data-task-id="' + taskId + '"]');
      if (!el) return;
      clearClarification(taskId);
      const box = document.createElement("div");
      box.className = "clarification-banner";
      box.innerHTML =
        '<p class="clarification-q"><strong>Worker needs clarification:</strong> ' + esc(question) + "</p>" +
        (brainNote ? '<p class="clarification-note">Brain could not resolve: ' + esc(brainNote) + "</p>" : "") +
        '<textarea id="clarify-input-' + esc(taskId) + '" placeholder="Answer the worker\'s question..."></textarea>' +
        '<button onclick="submitClarification(\'' + esc(taskId) + '\')">Submit answer</button>';
      el.appendChild(box);
    }

    window.submitClarification = async function submitClarification(taskId) {
      const input = document.getElementById("clarify-input-" + taskId);
      if (!input) return;
      const answer = input.value.trim();
      if (!answer) return;
      await api("POST", "/api/tasks/" + taskId + "/clarify", { answer });
      clearClarification(taskId);
    };

    function clearClarification(taskId) {
      document.querySelectorAll('.task-card[data-task-id="' + taskId + '"] .clarification-banner')
        .forEach(n => n.remove());
    }

    function toggleSpec(planId) {
      if (expandedSpecs.has(planId)) {
        expandedSpecs.delete(planId);
        renderDashboard();
        return;
      }
      expandedSpecs.add(planId);
      // Paint the box first so the reader sees "Reading <path>..." rather
      // than nothing while the repo read is in flight, then fetch.
      renderDashboard();
      void loadPlanSpecDoc(planId);
    }

    function toggleCompletedPlan(planId) {
      if (expandedCompletedPlans.has(planId)) expandedCompletedPlans.delete(planId); else expandedCompletedPlans.add(planId);
      renderDashboard();
    }

    async function dashboardApprove(planId) {
      await api("POST", "/api/plans/" + planId + "/approve");
      await loadPlans();
      await loadDashboard();
    }

    async function dashboardReject(planId) {
      await api("POST", "/api/plans/" + planId + "/reject");
      await loadPlans();
      await loadDashboard();
    }

    async function dashboardRetry(taskId) {
      await api("POST", "/api/tasks/" + taskId + "/retry");
      const owningPlan = Object.entries(dashboardTasks).find(([, tasks]) => tasks.some(task => task.id === taskId));
      if (owningPlan) {
        const planId = owningPlan[0];
        dashboardTasks[planId] = await api("GET", "/api/plans/" + planId + "/tasks");
      }
      renderDashboard();
    }

    async function approveMerge(taskId) {
      // The most irreversible action in the product, on a small button inside
      // a card whose own onclick opens the side panel, and both of its LESS
      // consequential neighbours already guard (rejectTask prompts,
      // deleteProject confirms). The text names the PR and the branch the
      // work lands on so the dialog is a fact check rather than a speed bump:
      // a task PR is opened against its plan branch, falling back to the
      // project's base branch when there is none.
      const task = Object.values(dashboardTasks).flat().find(item => item.id === taskId);
      const plan = task ? plans.find(item => item.id === task.plan_id) : null;
      const what = (task && task.pr_url) || (task && task.branch_name) || taskId;
      const into = (plan && plan.plan_branch_name) || "its base branch";
      if (!confirm(
        "Merge " + what + "\ninto " + into + "?\n\n" +
        "Praxis merges the pull request immediately. This cannot be undone from the dashboard."
      )) return;
      try {
        const res = await fetch("/api/tasks/" + taskId + "/approve-merge", {
          method: "POST",
          headers: { Authorization: "Bearer " + token },
        });
        if (!res.ok) throw new Error("approve failed: " + res.status);
        void loadDashboard({ skipSseReconnect: true });
      } catch (err) {
        alert("Approve failed: " + err.message);
      }
    }

    async function rejectTask(taskId) {
      const feedback = prompt("Reject reason (optional):") || "Rejected from dashboard";
      try {
        const res = await fetch("/api/tasks/" + taskId + "/reject-merge", {
          method: "POST",
          headers: {
            Authorization: "Bearer " + token,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ feedback: feedback }),
        });
        if (!res.ok) throw new Error("reject failed: " + res.status);
        void loadDashboard({ skipSseReconnect: true });
      } catch (err) {
        alert("Reject failed: " + err.message);
      }
    }

    // Normalize any approvals payload (REST body or SSE event) into the one
    // shape every consumer reads. A list that is absent becomes an empty
    // array here and ONLY here, so no renderer has to guess.
    function normalizeApprovals(raw) {
      const src = raw || {};
      const tasks = Array.isArray(src.tasks) ? src.tasks : [];
      const plans = Array.isArray(src.plans) ? src.plans : [];
      const proposals = Array.isArray(src.proposals) ? src.proposals : [];
      // A THIRD gate, on the same terms as proposals: a task at
      // NEEDS_CLARIFICATION whose question nobody has answered. It has no PR
      // and no branch, so the API keeps it out of `count` too, and reading
      // `count` alone leaves the header idle while the product waits for a
      // person it never told.
      const clarifications = Array.isArray(src.clarifications) ? src.clarifications : [];
      return {
        count: Number(src.count) || tasks.length + plans.length,
        proposal_count: Number(src.proposal_count) || proposals.length,
        clarification_count: Number(src.clarification_count) || clarifications.length,
        oldest_hours: Number(src.oldest_hours) || 0,
        tasks: tasks,
        plans: plans,
        proposals: proposals,
        clarifications: clarifications
      };
    }

    // Fold a digest event into the object the poll fetched.
    //
    // The merge-gate fields are taken from the event, which is what makes a
    // digest useful. `proposals` is different: the digest is built from a
    // plans query filtered on integration_pr_url, which no proposal can ever
    // satisfy, so an empty proposals list in an event carries no information.
    // Keeping the previous list is therefore the correct reading whether or
    // not the event ever starts carrying proposals: a stale entry is undone
    // by the very next poll, a deleted one is invisible until someone
    // reloads the page.
    function mergeApprovals(previous, incoming) {
      const before = normalizeApprovals(previous);
      const merged = normalizeApprovals(incoming);
      if (!merged.proposals.length) {
        merged.proposals = before.proposals;
        merged.proposal_count = before.proposal_count;
      }
      return merged;
    }

    // Everything a human still has to decide, across BOTH gates: work parked
    // at the merge gate (`count`, which is pull requests rather than rows)
    // and autonomous proposals parked before any work starts. The API keeps
    // them apart on purpose; the badge and the panel must show the same total
    // as each other, and this is it.
    function totalPendingApprovals() {
      const src = normalizeApprovals(pendingApprovals);
      return src.count + src.proposals.length + src.clarifications.length;
    }

    // Fetch the merge-gate approvals summary and refresh the header badge.
    // Called once on load (so the badge is correct before any SSE event) and
    // again on each approvals_digest event (rate-limited to every few hours).
    async function pollPendingApprovals() {
      if (!token) return;
      try {
        pendingApprovals = normalizeApprovals(await api("GET", "/api/approvals/pending"));
      } catch (error) {
        // The badge is an add-on; a failed lookup just leaves it stale.
        return;
      }
      renderApprovalsBadge();
    }

    function renderApprovalsBadge() {
      const actions = document.querySelector(".topbar-actions");
      if (!actions) return;
      let el = document.getElementById("approvals-badge");
      const src = normalizeApprovals(pendingApprovals);
      const mergeGate = src.count;
      const proposalCount = src.proposals.length;
      // Same helper the panel's header uses, so the badge and the panel can
      // never state two different totals. Both count DECISIONS, not rows: on
      // a shared pull request the panel lists one row per task while the
      // total counts the one merge that lands them all.
      const total = totalPendingApprovals();
      // Both gates, or the header reads idle while an autonomous proposal
      // waits. Testing `count` alone hid every proposal-only state, which is
      // exactly the state the improvement loop leaves behind.
      if (!total) {
        if (el) el.remove();
        return;
      }
      if (!el) {
        el = document.createElement("button");
        el.id = "approvals-badge";
        el.className = "approvals-badge";
        el.type = "button";
        el.onclick = showPendingApprovals;
        actions.insertBefore(el, actions.firstChild);
      }
      // No noun on the number: the two gates hold different things, and
      // calling a proposal a "PR" is how the digest line got it wrong. The
      // breakdown lives in the tooltip, where it can name both.
      const proposalAges = src.proposals.map(p => Number(p.age_hours) || 0);
      const oldest = Math.floor(Math.max(src.oldest_hours, ...proposalAges, 0));
      const parts = [];
      if (mergeGate) parts.push(mergeGate + (mergeGate === 1 ? " PR" : " PRs") + " at the merge gate");
      if (proposalCount) parts.push(proposalCount + (proposalCount === 1 ? " proposal" : " proposals") + " awaiting approval");
      // The THIRD gate. The badge already counts blocked questions (they are in
      // `total`) and the panel below lists them, so leaving them out here made
      // the tooltip name fewer things than the number it explains, and on a
      // clarification-only queue it opened with a bare comma naming nothing.
      const blocked = src.clarifications.length;
      if (blocked) parts.push(blocked + (blocked === 1 ? " task" : " tasks") + " blocked on a question");
      el.textContent = total + " awaiting approval";
      el.title = parts.join(", ") + ", oldest " + oldest + "h ago — click to view";
    }

    // Show the parked tasks flat, across every plan (the tasks view is
    // normally scoped to one plan via its dropdown, which cannot express a
    // cross-plan "everything awaiting approval" filter).
    async function showPendingApprovals() {
      currentView = "tasks";
      document.querySelectorAll(".nav-item").forEach(el => el.classList.toggle("active", el.dataset.view === "tasks"));
      setTopbar("Pending Approvals", "Refresh");
      const action = document.getElementById("primary-action");
      if (action) action.onclick = () => pollPendingApprovals().then(showPendingApprovals);
      currentTasks = pendingApprovals.tasks.map(t => ({
        id: t.task_id, title: t.title, branch_name: t.branch, status: "passed", pr_url: t.pr_url
      }));
      // Completed plans whose integration PR is still open belong here too:
      // the work is on the plan branch and NOT on the base branch, so it is
      // every bit as unapproved as a parked task. Listing only tasks is what
      // let the dashboard agree with a CLI that was also wrong.
      const awaitingPlans = pendingApprovals.plans || [];
      // Autonomous improvement proposals are parked on a DIFFERENT gate:
      // approve/reject before any work starts, rather than merge after work
      // finished. They carry no branch and no PR, which is why the API keeps
      // them out of `count` — but they are still a decision only a human can
      // make, and reading only `.tasks` and `.plans` printed "Nothing
      // awaiting approval" straight over a live one.
      const proposals = pendingApprovals.proposals || [];
      const container = document.getElementById("view-container");
      container.innerHTML =
        '<div class="master-panel">' +
          '<div class="master-header"><div class="master-title">' + totalPendingApprovals() + ' awaiting approval</div>' +
          '<button class="btn btn-compact" type="button" onclick="pollPendingApprovals().then(showPendingApprovals)">Refresh</button></div>' +
          '<div class="master-list" id="tasks-master-list"></div>' +
        '</div><div class="detail-panel" id="task-detail-panel"><div class="detail-empty">Select a task</div></div>';
      const list = document.getElementById("tasks-master-list");
      const taskRows = currentTasks.map(task =>
        '<button class="master-row' + (selectedTaskId === task.id ? ' selected' : '') +
        '" type="button" onclick="selectTask(\'' + esc(task.id) + '\')">' +
          '<div class="row-main"><div class="row-name">' + esc(task.title || task.id) + '</div>' +
          '<div class="row-meta">' + esc(task.branch_name) + '</div></div>' + badge(task.status) +
        '</button>'
      ).join("");
      const planRows = awaitingPlans.map(plan =>
        '<div class="master-row">' +
          '<div class="row-main"><div class="row-name">Integrate ' + esc(plan.branch || plan.plan_id) + '</div>' +
          '<div class="row-meta">' +
            (plan.pr_url ? '<a href="' + esc(plan.pr_url) + '" target="_blank" rel="noopener">' + esc(plan.pr_url) + '</a>' : esc(plan.plan_id)) +
          '</div></div>' + badge("passed") +
        '</div>'
      ).join("");
      const mergeGateHtml = (taskRows + planRows) ?
        '<div class="master-group-title">Merge gate</div>' + taskRows + planRows : "";
      // The id goes out in FULL, on its own line, beside the verb that takes
      // it. Truncating it to eight characters made the row look like it named
      // the plan while `praxis approve <8 chars>` 404s: the buttons here carry
      // the whole id and work, so only the human copying it was misled. The
      // clarification rows below already print the full form, and this matches
      // them rather than inventing a second convention.
      const proposalRows = proposals.map(proposal =>
        '<div class="master-row">' +
          '<div class="row-main"><div class="row-name">Improvement proposal</div>' +
          '<div class="row-meta">no branch yet &middot; proposed ' + Math.floor(Number(proposal.age_hours) || 0) + 'h ago</div>' +
          '<div class="row-meta">praxis approve ' + esc(proposal.plan_id) + '</div></div>' +
          badge("pending") +
          '<div class="row-actions">' +
            '<button class="btn btn-compact btn-primary" type="button" onclick="approveProposal(' + jsArg(proposal.plan_id) + ')">Approve</button>' +
            '<button class="btn btn-compact btn-danger" type="button" onclick="rejectProposal(' + jsArg(proposal.plan_id) + ')">Reject</button>' +
          '</div>' +
        '</div>'
      ).join("");
      const proposalsHtml = proposalRows ?
        '<div class="master-group-title">Autonomous proposals</div>' + proposalRows : "";
      // The third gate. A worker asked a question, the brain declined to
      // answer it or answered below the project's confidence threshold, and
      // the task has been parked ever since. There is no button here on
      // purpose: the answer is free text and the CLI verb takes it
      // (`praxis clarify <task-id> "..."`), so this surface names the
      // question and the id rather than pretending a click can resolve it.
      const clarifications = pendingApprovals.clarifications || [];
      const clarificationRows = clarifications.map(item =>
        '<div class="master-row">' +
          '<div class="row-main"><div class="row-name">' + esc(item.title || item.task_id) + '</div>' +
          '<div class="row-meta">' + esc(item.question || "(no question recorded)") + '</div>' +
          '<div class="row-meta">praxis clarify ' + esc(item.task_id) + ' "your answer"</div></div>' +
          badge("needs_clarification") +
        '</div>'
      ).join("");
      const clarificationsHtml = clarificationRows ?
        '<div class="master-group-title">Blocked on a question</div>' + clarificationRows : "";
      list.innerHTML = (mergeGateHtml + proposalsHtml + clarificationsHtml) || '<div class="empty-list">Nothing awaiting approval</div>';
    }

    // Approve/Reject a proposal from the approvals panel. The POST lives in
    // approvePlan/rejectPlan and stays there: these wrappers only add the
    // refresh this view needs, so there is exactly one call site per verb.
    async function approveProposal(planId) {
      await approvePlan(planId);
      await pollPendingApprovals();
      await showPendingApprovals();
    }

    async function rejectProposal(planId) {
      await rejectPlan(planId);
      await pollPendingApprovals();
      await showPendingApprovals();
    }

    async function pollStatus() {
      if (!token) return;
      try {
        const status = await api("GET", "/api/status");
        if (status.lm_studio_url) effectiveLmStudioUrl = status.lm_studio_url;
        const agentsStat = document.getElementById("stat-agents");
        // `agents_reachable` is false when the agent manager is absent (no
        // Docker on this host) or the container listing raised (daemon down).
        // Both used to leave active_agents at 0, indistinguishable from a
        // genuinely idle system with zero agents running: a reader could not
        // tell "0 because idle" from "0 because we could not ask".
        if (status.agents_reachable) {
          agentsStat.textContent = status.active_agents;
          agentsStat.title = "";
          measuredAgentCount = status.active_agents;
        } else {
          agentsStat.textContent = "?";
          agentsStat.title = "could not reach the agent manager; Docker may be unavailable";
          // null, not the last good number: a stale count reprinted as fresh
          // is the same lie in slower motion.
          measuredAgentCount = null;
        }
        document.getElementById("stat-queue").textContent = status.opus_state.queued_count;
        measuredQueueCount = status.opus_state.queued_count;
        window.__opusStatus = status.opus_state ? status.opus_state.status : "unknown";
        // Re-render the opus pill in-place so it reflects the fresh status without
        // waiting for the next full dashboard reload.
        const _opusDot = document.querySelector(".health-bar .health-dot");
        if (_opusDot) {
          const _s = window.__opusStatus;
          _opusDot.className = "health-dot " + _s;
          const _opusTxt = _opusDot.nextElementSibling;
          if (_opusTxt) _opusTxt.textContent = "Opus " + _s.replace("_", " ");
        }
        setConnection("agent", status.agent_model, status.opus_state.status);
        setConnection("subagent", status.subagent_model, status.subagent_model.connected ? "connected" : "disconnected", status.lm_studio_url);
        updateProviderLoginBanner(status.providers);
      } catch (error) {
        // "offline" is a claim about the PROVIDER, and this branch establishes
        // nothing about any provider: the status call itself did not come back.
        // On a fresh dashboard with no token that is a 401, and both pills read
        // "offline" while both providers were fine, which is the same
        // fact-versus-verdict split the doctor rows were just fixed for.
        const unreachable =
          error.status === 401 || error.status === 403 ? "not authorized" : "unknown";
        setConnection("agent", { name: unreachable, connected: false }, "disconnected");
        setConnection(
          "subagent", { name: unreachable, connected: false }, "disconnected"
        );
      }
    }

    // Track whether the user has manually dismissed the banner this session.
    // Reset whenever the set of actionable providers changes so a newly broken
    // provider always surfaces again.
    let _bannerDismissedFor = "";
    // Last providers array from /api/status, so the SSE handler can re-render.
    let _lastProviders = [];
    // Providers flagged by a RUNTIME brain-call auth failure (provider_auth_required
    // SSE event). This is the authoritative signal: some CLIs (codex) report
    // "logged in" to the status poll even when the session is actually revoked,
    // so the poll alone can't be trusted. name -> login_hint.
    const _runtimeAuthFailures = new Map();

    function flagRuntimeAuthFailure(provider, loginHint) {
      if (!provider) return;
      _runtimeAuthFailures.set(provider, loginHint || (provider + " login"));
      updateProviderLoginBanner(_lastProviders);
    }

    function updateProviderLoginBanner(providers) {
      const banner = document.getElementById("provider-login-banner");
      if (!banner) return;
      // Defensive: older backends may omit the key
      const pollList = Array.isArray(providers) ? providers : [];
      _lastProviders = pollList;
      // NOTE: a runtime auth failure is NOT cleared by the poll reporting the
      // provider authenticated — some CLIs (codex) lie to the status check on a
      // revoked-but-cached token. Runtime evidence (an actual 401) outranks the
      // poll; the flag clears only when the user dismisses the banner (after
      // running the login command).
      // Union: installed-but-unauthenticated from the poll, plus runtime failures
      // (which override the poll's optimistic "authenticated" for that provider).
      const actionableMap = new Map();
      pollList.filter(p => p.cli_available && !p.authenticated)
        .forEach(p => actionableMap.set(p.name, p.login_hint || (p.name + " login")));
      _runtimeAuthFailures.forEach((hint, name) => actionableMap.set(name, hint));
      const actionable = Array.from(actionableMap, ([name, login_hint]) => ({ name, login_hint }));
      // Build a stable key so we can detect when the set changes (e.g. user logs in)
      const key = actionable.map(p => p.name).sort().join(",");
      if (key !== _bannerDismissedFor) {
        // Set changed — lift any previous dismiss so the new state shows
        _bannerDismissedFor = "";
      }
      if (actionable.length === 0 || _bannerDismissedFor === key) {
        banner.classList.remove("visible");
        return;
      }
      // Rebuild rows (preserve dismiss button which is always the first child)
      const dismiss = banner.querySelector(".provider-login-banner-dismiss");
      // Remove old rows
      Array.from(banner.querySelectorAll(".provider-login-banner-row")).forEach(el => el.remove());
      actionable.forEach(p => {
        const row = document.createElement("div");
        row.className = "provider-login-banner-row";
        const hint = p.login_hint || (p.name + " login");
        row.innerHTML =
          "⚠ <strong>" + escapeHtml(p.name) + "</strong> needs login — run " +
          "<span class=\"provider-login-banner-hint\">" + escapeHtml(hint) + "</span>" +
          " in your terminal, then retry.";
        // Insert before dismiss button
        banner.insertBefore(row, dismiss);
      });
      banner.classList.add("visible");
    }

    function dismissProviderBanner() {
      const banner = document.getElementById("provider-login-banner");
      if (!banner) return;
      // Record the current set of provider names shown so we only re-show if the set changes
      const names = Array.from(banner.querySelectorAll(".provider-login-banner-row strong"))
        .map(el => el.textContent).sort().join(",");
      _bannerDismissedFor = names;
      // Clear runtime-flagged failures on dismiss: the user has acknowledged
      // (and presumably run the login command). A subsequent real auth failure
      // re-flags the provider via the SSE event.
      _runtimeAuthFailures.clear();
      banner.classList.remove("visible");
    }

    function escapeHtml(str) {
      return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    // endpointUrl is optional: when the connection is DOWN and a URL is
    // known (e.g. the LM Studio endpoint behind the Implementer slot), show
    // it instead of a bare "unknown": the dashboard previously named no
    // target at all, so there was nothing to go check.
    //
    // `info.connected_measured === false` means /api/status did not actually
    // probe connectivity for this one (a `local` planner has no CLI to probe,
    // an unrecognized provider, or a planner that failed to resolve at all).
    // Painting a confident green or red dot for a fact nobody measured reads
    // as a real answer; a caller that only reads a binary dot cannot tell
    // "confirmed up" from "confirmed down" from "never checked". Other
    // `info` shapes (subagent_model) never carry this key, so `!== false`
    // treats a missing field as "measured", which keeps this backward
    // compatible.
    function setConnection(prefix, info, status, endpointUrl) {
      const dot = document.getElementById(prefix + "-dot");
      const model = document.getElementById(prefix + "-model");
      const connected = info && info.connected;
      const measured = !info || info.connected_measured !== false;
      let dotClass;
      if (!measured) {
        // No CSS rule targets "unmeasured" on purpose: the dot falls back to
        // the base .conn-dot neutral gray instead of a colour that would
        // assert a verdict.
        dotClass = "unmeasured";
      } else if (status === "rate_limited") {
        dotClass = "rate_limited";
      } else {
        dotClass = connected ? "connected" : "disconnected";
      }
      dot.className = "conn-dot " + dotClass;
      if (!measured) {
        model.textContent = info && info.name ? info.name : "unknown";
        model.title = (info && info.detail) || "not measured here";
      } else if (!connected && endpointUrl) {
        model.textContent = "unreachable (" + endpointUrl + ")";
        model.title = endpointUrl;
      } else {
        model.textContent = info && info.name ? info.name : "unknown";
        model.removeAttribute("title");
      }
    }

    // ── Create-Spec chat panel ───────────────────────────────────────────────

    let specChatSessionId = null;
    let specChatStreamingBubble = null;

    function openSpecChat() {
      const overlay = document.getElementById("spec-chat-overlay");
      overlay.classList.add("open");
      // Brainstorm replies arrive over the /api/events SSE; ensure it is live even
      // when opened from a view (Projects/Plans) that had closed the stream.
      if (!eventSource) connectDashboardSse();
      const label = document.getElementById("spec-chat-project-label");
      const project = projects.find(p => p.id === selectedProjectId);
      label.textContent = project ? project.name : (projects.length ? projects[0].name : "No project selected");
      document.getElementById("spec-chat-input").focus();
    }

    function closeSpecChat() {
      document.getElementById("spec-chat-overlay").classList.remove("open");
      // Reset session so the next open starts a fresh conversation
      specChatSessionId = null;
      specChatStreamingBubble = null;
      const messages = document.getElementById("spec-chat-messages");
      if (messages) messages.innerHTML = '<div class="spec-chat-empty">Describe what you want to build.<br>Claude will help you shape it into a spec.</div>';
      setChatInputEnabled(true);
    }

    function onSpecChatOverlayClick(event) {
      if (event.target === document.getElementById("spec-chat-overlay")) closeSpecChat();
    }

    function onSpecChatKey(event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendSpecChatMessage();
      }
    }

    function appendChatMessage(role, text) {
      const messages = document.getElementById("spec-chat-messages");
      if (!messages) return;
      // Remove empty-state placeholder on first message
      const empty = messages.querySelector(".spec-chat-empty");
      if (empty) empty.remove();

      if (role === "assistant" && specChatStreamingBubble) {
        // Append to existing streaming bubble
        specChatStreamingBubble.textContent += text;
      } else {
        const bubble = document.createElement("div");
        bubble.className = "spec-chat-bubble " + (role === "user" ? "user" : "assistant streaming");
        bubble.textContent = text;
        messages.appendChild(bubble);
        if (role === "assistant") specChatStreamingBubble = bubble;
      }
      messages.scrollTop = messages.scrollHeight;
    }

    function setChatInputEnabled(enabled) {
      const input = document.getElementById("spec-chat-input");
      const send = document.getElementById("spec-chat-send");
      if (input) input.disabled = !enabled;
      if (send) send.disabled = !enabled;
      if (enabled && input) input.focus();
    }

    function showSpecThinking() {
      const messages = document.getElementById("spec-chat-messages");
      if (!messages || document.getElementById("spec-chat-thinking")) return;
      const empty = messages.querySelector(".spec-chat-empty");
      if (empty) empty.remove();
      const bubble = document.createElement("div");
      bubble.id = "spec-chat-thinking";
      bubble.className = "spec-chat-bubble assistant";
      bubble.innerHTML = 'Claude is thinking<span class="thinking-dots"><span>.</span><span>.</span><span>.</span></span>';
      messages.appendChild(bubble);
      messages.scrollTop = messages.scrollHeight;
    }

    function hideSpecThinking() {
      const bubble = document.getElementById("spec-chat-thinking");
      if (bubble) bubble.remove();
    }

    function appendSpecChatError(text) {
      hideSpecThinking();
      const messages = document.getElementById("spec-chat-messages");
      if (!messages) return;
      const bubble = document.createElement("div");
      bubble.className = "spec-chat-bubble assistant spec-chat-err";
      bubble.textContent = "⚠ " + text;
      messages.appendChild(bubble);
      messages.scrollTop = messages.scrollHeight;
    }

    async function sendSpecChatMessage() {
      const input = document.getElementById("spec-chat-input");
      if (!input) return;
      const text = input.value.trim();
      if (!text) return;

      const projectId = selectedProjectId || projects[0]?.id;
      if (!projectId) {
        alert("Select a project first (Projects view).");
        return;
      }

      input.value = "";
      setChatInputEnabled(false);
      appendChatMessage("user", text);
      specChatStreamingBubble = null;
      showSpecThinking();

      try {
        if (!specChatSessionId) {
          const result = await api("POST", "/api/specs/sessions", { project_id: projectId, message: text });
          specChatSessionId = result.session_id;
        } else {
          await api("POST", "/api/specs/sessions/" + specChatSessionId + "/message", { message: text });
        }
      } catch (error) {
        appendSpecChatError(error.message);
        setChatInputEnabled(true);
      }
    }

    // SSE handlers for brainstorm events (wired into connectDashboardSse source)
    function handleBrainstormSse(source) {
      source.addEventListener("brainstorm_message", event => {
        let data;
        try { data = JSON.parse(event.data); } catch (e) { return; }
        if (data.type === "brainstorm_message") {
          hideSpecThinking();
          appendChatMessage("assistant", data.text);
        }
      });
      source.addEventListener("brainstorm_error", event => {
        let data;
        try { data = JSON.parse(event.data); } catch (e) { return; }
        appendSpecChatError(data.error || "Brainstorm turn failed.");
        specChatStreamingBubble = null;
        setChatInputEnabled(true);
      });
      source.addEventListener("context_draft_ready", event => {
        let data;
        try { data = JSON.parse(event.data); } catch (e) { return; }
        if (data.type === "context_draft_ready" && currentView === "memory") {
          showDraft({ draft_id: data.draft_id, diff: "" });
        }
      });
      source.addEventListener("brainstorm_turn_done", () => {
        // Finalize streaming bubble — remove streaming class
        hideSpecThinking();
        if (specChatStreamingBubble) {
          specChatStreamingBubble.classList.remove("streaming");
          specChatStreamingBubble = null;
        }
        setChatInputEnabled(true);
      });
    }

    // ── End Create-Spec chat panel ───────────────────────────────────────────

    // ── Memory page ──────────────────────────────────────────────────────────

    function activeProjectId() {
      return globalProjectFilter !== "all" ? globalProjectFilter : selectedProjectId;
    }

    async function renderMemory() {
      if (!projects.length) await loadProjects();
      const container = document.getElementById("view-container");
      container.style.display = "";
      const pid = activeProjectId();
      if (!pid) {
        container.innerHTML =
          '<div class="detail-empty" style="padding:40px;text-align:center;">' +
          'Select a specific project from the top-right dropdown to view its memory.</div>';
        return;
      }
      const projectName = projects.find(p => p.id === pid)?.name || pid;
      container.innerHTML =
        '<div class="view-loader"><div class="spinner"></div>' +
        '<span>Cloning repository… this can take a few seconds.</span></div>';
      let ctx;
      try {
        ctx = await api("GET", "/api/projects/" + pid + "/context");
      } catch (e) {
        const errDetail = e.message && e.message !== "Internal Server Error"
          ? e.message
          : "The server returned an error. Check that the project exists and the API is reachable.";
        container.innerHTML =
          '<div class="detail-empty" style="padding:40px;text-align:center;">' +
          '<div style="margin-bottom:8px;font-weight:700;">Could not load memory</div>' +
          '<div style="color:var(--text-muted);font-size:12px;margin-bottom:16px;">' + esc(errDetail) + '</div>' +
          '<button class="btn btn-primary" type="button" onclick="renderMemory()">Retry</button></div>';
        return;
      }
      container.innerHTML =
        '<div class="docs-list-col">' +
        '<div class="list-header"><h2>Memory &middot; ' + esc(projectName) + '</h2>' +
        '<button class="btn" type="button" onclick="syncContext()">Sync now</button></div>' +
        '<div style="flex:1;overflow-y:auto;">' +
        '<div class="mem-pane"><h3>CLAUDE.md</h3><pre class="mem-doc">' + esc(ctx.claude_md || "(empty)") + '</pre></div>' +
        '<div class="mem-pane"><h3>MEMORY.md</h3><pre class="mem-doc">' + esc(ctx.memory_md || "(empty)") + '</pre></div>' +
        '<div id="ctx-draft"></div>' +
        '</div></div>';
    }

    async function syncContext() {
      const pid = activeProjectId();
      const draft = await api("POST", "/api/projects/" + pid + "/context-sync", { summary: "manual sync" });
      showDraft(draft);
    }

    function showDraft(draft) {
      document.getElementById("ctx-draft").innerHTML =
        '<div class="mem-pane"><h3>Proposed changes</h3><pre class="mem-diff">' + esc(draft.diff || "(no changes)") + '</pre>' +
        (draft.draft_id ? '<button class="btn btn-primary" onclick="approveDraft(\'' + esc(draft.draft_id) + '\')">Approve &amp; commit</button>' : "") +
        '</div>';
    }

    async function approveDraft(id) {
      await api("POST", "/api/context-drafts/" + id + "/approve");
      renderMemory();
    }

    // ── End Memory page ───────────────────────────────────────────────────────

    // ── Harness dropdown + About panel ───────────────────────────────────────

    async function loadHarnesses() {
      try {
        HARNESSES = await api("GET", "/api/harnesses");
      } catch (e) {
        HARNESSES = [];
        return;
      }
      const sel = document.getElementById("project-harness");
      if (!sel) return;
      sel.innerHTML = "";
      for (const h of HARNESSES) {
        const opt = document.createElement("option");
        opt.value = h.id;
        opt.textContent = h.recommended ? h.display_name + " (recommended)" : h.display_name;
        sel.appendChild(opt);
      }
      // If loadLmModels() already finished, its sync was a no-op against an
      // empty select; redo it now that the options exist.
      syncHarnessToSelectedModel();
    }

    function renderHarnessAbout() {
      const body = document.getElementById("harness-about-body");
      if (!body) return;
      if (!HARNESSES.length) {
        body.innerHTML = '<p style="color:var(--text-faint);font-size:13px;padding:8px 0;">No harness data available.</p>';
      } else {
        body.innerHTML = HARNESSES.map(h =>
          '<section class="harness-card">' +
          '<h3>' + esc(h.display_name) + (h.recommended ? ' ⭐ recommended' : '') + '<small>(' + esc(h.maturity) + ')</small></h3>' +
          '<p>' + esc(h.description) + '</p>' +
          '<p><strong>What\'s unique:</strong> ' + esc(h.uniqueness) + '</p>' +
          '<p><strong>When to pick:</strong> ' + esc(h.when_to_pick) + '</p>' +
          '<div class="harness-cols">' +
            '<div><strong>Pros</strong><ul>' + (h.pros || []).map(p => '<li>' + esc(p) + '</li>').join('') + '</ul></div>' +
            '<div><strong>Cons</strong><ul>' + (h.cons || []).map(c => '<li>' + esc(c) + '</li>').join('') + '</ul></div>' +
          '</div>' +
          (h.notes && h.notes.length ? '<p style="margin-top:8px;font-size:12px;color:var(--text-faint);">Notes: ' + h.notes.map(n => esc(n)).join(' &middot; ') + '</p>' : '') +
          '</section>'
        ).join('');
      }
      document.getElementById("harness-overlay").classList.add("open");
    }

    function closeHarnessAbout() {
      document.getElementById("harness-overlay").classList.remove("open");
    }

    function onHarnessOverlayClick(event) {
      if (event.target === document.getElementById("harness-overlay")) closeHarnessAbout();
    }

    // ── End Harness dropdown + About panel ────────────────────────────────────

    document.getElementById("harness-about-close").addEventListener("click", closeHarnessAbout);
    document.getElementById("harness-about-close-footer").addEventListener("click", closeHarnessAbout);

    // ── Settings modal ────────────────────────────────────────────────────────

    let settingsCurrentTab = "global";
    // Holds original fetched values for dirty-checking: { key: originalValue }
    let settingsGlobalOriginal = {};
    // Holds original project field values for dirty-checking
    let settingsProjectOriginal = {};
    let settingsSelectedProjectId = null;

    function openSettings() {
      document.getElementById("settings-overlay").classList.add("open");
      clearSettingsFeedback();
      switchSettingsTab(settingsCurrentTab);
    }

    function closeSettings() {
      document.getElementById("settings-overlay").classList.remove("open");
    }

    function onSettingsOverlayClick(event) {
      if (event.target === document.getElementById("settings-overlay")) closeSettings();
    }

    function switchSettingsTab(tab) {
      settingsCurrentTab = tab;
      document.querySelectorAll(".settings-tab").forEach(btn => {
        btn.classList.toggle("active", btn.id === "settings-tab-" + tab);
      });
      document.querySelectorAll(".settings-tab-panel").forEach(panel => {
        panel.classList.toggle("active", panel.id === "settings-panel-" + tab);
      });
      clearSettingsFeedback();
      if (tab === "global") {
        loadGlobalSettings();
      } else if (tab === "project") {
        loadSettingsProjectList();
      } else if (tab === "models") {
        loadModelsPanel();
      }
    }

    async function loadModelsPanel() {
      const [registry, roles, caps] = await Promise.all([
        api("GET", "/api/settings/registry"),
        api("GET", "/api/settings/roles"),
        api("GET", "/api/settings/capabilities"),
      ]);
      const capModels = (caps && caps.models) || {};

      const regRows = registry.map(m => {
        const c = capModels[m.model || ""] || {};
        const swe = typeof c.swe_bench_verified === "number" ? Math.round(c.swe_bench_verified * 100) + "%" : "-";
        return '<tr>' +
          '<td>' + esc(m.name) + '</td>' +
          '<td>' + esc(m.provider) + '</td>' +
          '<td>' + esc(m.model || "-") + '</td>' +
          '<td>' + esc(m.effort || "-") + '</td>' +
          '<td>' + swe + '</td>' +
          '<td>' + esc(String(c.speed_tps || "-")) + '</td>' +
          '<td>' + esc(String(c.price_per_mtok_blended || "-")) + '</td>' +
          '</tr>';
      }).join("");
      const regTable =
        '<h3>Registered Models</h3>' +
        '<table class="reg-table"><thead><tr>' +
        '<th>Name</th><th>Provider</th><th>Model</th><th>Effort</th><th>SWE-bench</th><th>tok/s</th><th>$/Mtok</th>' +
        '</tr></thead><tbody>' + regRows + '</tbody></table>' +
        '<div class="doc-tag" style="margin-top:4px;">capabilities as of ' + esc(String(caps.as_of || "?")) + '</div>';

      const roleRows = ["plan", "review", "implement"].map(role => {
        const chain = (roles[role] || []).join(", ");
        return '<div class="formrow">' +
          '<label style="display:flex;align-items:center;gap:6px;color:var(--text-muted);font-size:12px;font-weight:500;margin-bottom:5px;">' + esc(role) + '</label>' +
          '<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">' +
            '<input id="role-' + role + '" value="' + esc(chain) + '" placeholder="model names, priority first (comma-separated)" style="flex:1;min-width:180px;">' +
            '<button class="btn btn-compact" type="button" onclick="saveRole(\'' + role + '\')">Save</button>' +
          '</div>' +
          '</div>';
      }).join("");
      const roleBlock = '<h3 style="margin-top:16px;">Role Fallback Chains</h3>' +
        '<div class="doc-tag" style="margin-bottom:8px;">First name = priority; later names are tried on rate-limit / auth / gateway errors.</div>' +
        roleRows;

      const providers = ["claude", "agy", "codex", "local"];
      const data = await api("GET", "/api/settings/models");
      const advRows = Object.entries(data).map(([site, cfg]) => {
        const opts = providers.map(p =>
          '<option value="' + p + '"' + (cfg.provider === p ? ' selected' : '') + '>' + p + '</option>'
        ).join("");
        return '<div class="formrow">' +
          '<label style="display:flex;align-items:center;gap:6px;color:var(--text-muted);font-size:12px;font-weight:500;margin-bottom:5px;">' +
            esc(site) +
            ' <span class="doc-tag">def: ' + esc(cfg.default.provider) + '/' + esc(cfg.default.model || '-') + '</span>' +
          '</label>' +
          '<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">' +
            '<select id="m-prov-' + site + '" style="flex:0 0 auto;">' + opts + '</select>' +
            '<input id="m-model-' + site + '" value="' + esc(cfg.model || '') + '" placeholder="model" style="flex:1;min-width:80px;">' +
            '<input id="m-effort-' + site + '" value="' + esc(cfg.effort || '') + '" placeholder="effort" style="flex:0 0 80px;">' +
            '<button class="btn btn-compact" type="button" onclick="saveModel(\'' + site + '\')">Save</button>' +
            '<button class="btn btn-compact" type="button" onclick="resetModel(\'' + site + '\')">Reset</button>' +
          '</div>' +
        '</div>';
      }).join("");
      const advBlock = '<details style="margin-top:16px;"><summary style="cursor:pointer;color:var(--text-muted);font-size:12px;font-weight:500;">Advanced: per-call-site overrides</summary>' +
        '<div style="margin-top:8px;">' + advRows + '<div style="margin-top:8px;"><button class="btn" type="button" onclick="resetModel(null)">Reset all</button></div></div></details>';

      document.getElementById("settings-panel-models").innerHTML =
        regTable + roleBlock + advBlock;
    }

    async function saveRole(role) {
      const raw = document.getElementById("role-" + role).value;
      const names = raw.split(",").map(s => s.trim()).filter(Boolean);
      const roles = await api("GET", "/api/settings/roles");
      roles[role] = names;
      try {
        await api("PUT", "/api/settings/roles", { chains: roles });
        await loadModelsPanel();
      } catch (e) {
        alert("Could not save role chain: " + (e && e.message ? e.message : e));
      }
    }

    async function checkOnboarding() {
      try {
        const roles = await api("GET", "/api/settings/roles");
        const configured = roles && Object.values(roles).some(c => Array.isArray(c) && c.length);
        const dismissed = localStorage.getItem("praxis_onboarding_dismissed") === "1";
        const banner = document.getElementById("onboarding-banner");
        if (banner) banner.style.display = (!configured && !dismissed) ? "block" : "none";
      } catch (e) { /* non-fatal */ }
    }

    function dismissOnboarding() {
      localStorage.setItem("praxis_onboarding_dismissed", "1");
      const banner = document.getElementById("onboarding-banner");
      if (banner) banner.style.display = "none";
    }

    async function saveModel(site) {
      const config = {
        provider: document.getElementById("m-prov-" + site).value,
        model: document.getElementById("m-model-" + site).value || null,
        effort: document.getElementById("m-effort-" + site).value || null,
      };
      const saved = await api("PUT", "/api/settings/models", { call_site: site, config });
      await loadModelsPanel();
      // A stored override that a role chain shadows has NOT taken effect. The
      // server says so; discarding the response made this panel the one place
      // the warning could not appear, and on a stock install the shipped role
      // chains shadow most call sites.
      if (saved && saved.status === "stored_but_shadowed") {
        showSettingsFeedback(saved.detail, true);
      }
    }

    async function resetModel(site) {
      await api("POST", "/api/settings/models/reset", { call_site: site });
      await loadModelsPanel();
    }

    function clearSettingsFeedback() {
      const el = document.getElementById("settings-feedback");
      if (el) { el.textContent = ""; el.className = "settings-feedback"; }
    }

    function showSettingsFeedback(msg, isError) {
      const el = document.getElementById("settings-feedback");
      if (!el) return;
      el.textContent = msg;
      el.className = "settings-feedback " + (isError ? "error" : "success");
      if (!isError) setTimeout(() => { if (el.textContent === msg) { el.textContent = ""; } }, 3000);
    }

    // ── Global settings ───────────────────────────────────────────────────────

    const SETTINGS_EDITABLE_META = {
      lm_studio_url:         { label: "LM Studio URL",           type: "text" },
      agent_model:           { label: "Agent Model",             type: "text" },
      agent_model_effort:    { label: "Agent Model Effort",      type: "text", hint: "low | medium | high (empty = default)" },
      docs_root:             { label: "Docs Root Path",          type: "text" },
      memory_md_path:        { label: "MEMORY.md Path",          type: "text" },
      brainstorm_workspace:  { label: "Brainstorm Workspace",    type: "text" },
    };

    async function loadGlobalSettings() {
      const container = document.getElementById("settings-global-content");
      container.innerHTML = '<div style="color:var(--text-faint);font-size:13px;">Loading…</div>';
      try {
        const data = await api("GET", "/api/settings");
        settingsGlobalOriginal = {};
        for (const key of Object.keys(SETTINGS_EDITABLE_META)) {
          const entry = data.editable ? data.editable[key] : null;
          settingsGlobalOriginal[key] = entry ? entry.value : null;
        }
        container.innerHTML = renderGlobalSettings(data);
        await loadAutoDelegateSetting();
      } catch (e) {
        const msg = e.message || "Unknown error";
        const isAuth = msg.includes("401") || msg.toLowerCase().includes("unauthorized");
        container.innerHTML =
          '<div style="color:var(--btn-danger-text);font-size:13px;padding:8px 0;">' +
          esc(isAuth ? "Authentication failed. Check your API token." : "Failed to load settings: " + msg) +
          '</div>';
      }
    }

    async function loadAutoDelegateSetting() {
      const toggle = document.getElementById("auto-delegate-toggle");
      const info = document.getElementById("auto-delegate-worker-info");
      if (!toggle) return;
      try {
        const data = await api("GET", "/api/settings/auto-delegate");
        toggle.checked = !!data.enabled;
        if (info && data.worker) {
          info.textContent = "Worker: " + (data.worker.harness || "default") + " / " + (data.worker.model || "default");
        }
      } catch (e) {
        console.error("Failed to load auto-delegate setting:", e);
      }
    }

    async function onAutoDelegateToggleChange(checked) {
      clearSettingsFeedback();
      const toggle = document.getElementById("auto-delegate-toggle");
      const info = document.getElementById("auto-delegate-worker-info");
      try {
        const data = await api("PUT", "/api/settings/auto-delegate", { enabled: checked });
        if (toggle) toggle.checked = !!data.enabled;
        if (info && data.worker) {
          info.textContent = "Worker: " + (data.worker.harness || "default") + " / " + (data.worker.model || "default");
        }
        showSettingsFeedback("Auto-delegate mode " + (data.enabled ? "enabled" : "disabled") + ".", false);
      } catch (e) {
        if (toggle) toggle.checked = !checked;
        const msg = e.message || "Unknown error";
        showSettingsFeedback("Failed to update auto-delegate mode: " + msg, true);
      }
    }

    function renderGlobalSettings(data) {
      const editable = data.editable || {};
      const readonly = data.readonly || {};
      let html = '<div class="settings-section-title">Editable Settings</div>';

      for (const [key, meta] of Object.entries(SETTINGS_EDITABLE_META)) {
        const entry = editable[key] || {};
        const val = entry.value != null ? String(entry.value) : "";
        const overridden = !!entry.overridden;
        const escapedKey = esc(key);
        const escapedVal = esc(val);
        html += '<div class="settings-field-row">' +
          '<label for="sg-' + escapedKey + '">' + esc(meta.label) +
            (overridden ? ' <span class="settings-overridden-badge">overridden by env</span>' : '') +
          '</label>' +
          '<input id="sg-' + escapedKey + '" type="' + esc(meta.type || "text") + '" ' +
            'data-settings-key="' + escapedKey + '" ' +
            'value="' + escapedVal + '" ' +
            'autocomplete="off">' +
          (meta.hint ? '<div class="settings-note">' + esc(meta.hint) + '</div>' : '') +
          '<div class="settings-field-actions">' +
            '<button class="btn btn-compact" type="button" onclick="resetSettingsField(' + JSON.stringify(key) + ')">Reset to default</button>' +
          '</div>' +
        '</div>';
      }

      html += '<div class="settings-section-title" style="margin-top:18px;">Read-only (require restart)</div>';
      html += '<div class="settings-readonly-group">';
      for (const [key, val] of Object.entries(readonly)) {
        html += '<div class="settings-field-row" style="margin-bottom:10px;">' +
          '<label>' + esc(key) + '</label>' +
          '<input type="text" value="' + esc(String(val != null ? val : "")) + '" disabled>' +
        '</div>';
      }
      html += '<div class="settings-note">These values are set via environment variables and require a server restart to change.</div>';
      html += '</div>';

      return html;
    }

    async function resetSettingsField(key) {
      clearSettingsFeedback();
      try {
        const data = await api("PUT", "/api/settings", { [key]: null });
        showSettingsFeedback("'" + key + "' reset to default.", false);
        // Re-render with fresh data
        const container = document.getElementById("settings-global-content");
        settingsGlobalOriginal[key] = null;
        container.innerHTML = renderGlobalSettings({ editable: data, readonly: {} });
        // Reload to get readonly section too
        loadGlobalSettings();
      } catch (e) {
        const msg = e.message || "Unknown error";
        showSettingsFeedback("Error: " + msg, true);
      }
    }

    // ── Project settings ──────────────────────────────────────────────────────

    const PROJECT_EDITABLE_FIELDS = [
      { key: "name",                  label: "Name",                   type: "text" },
      { key: "model_name",            label: "Model (implementer)",    type: "text" },
      { key: "agent_model",           label: "Agent Model (planner)",  type: "text", hint: "Leave blank to use global default" },
      { key: "agent_model_effort",    label: "Agent Model Effort",     type: "text", hint: "low | medium | high (empty = default)" },
      { key: "lm_studio_url",         label: "LM Studio URL",          type: "text", hint: "Leave blank to use global default" },
      { key: "default_branch",        label: "Default Branch",         type: "text" },
      { key: "approval_gate",         label: "Approval Gate",          type: "checkbox" },
      { key: "confidence_threshold",  label: "Confidence Threshold",   type: "number", hint: "0.0 – 1.0" },
      { key: "max_retries",           label: "Max Retries",            type: "number" },
      { key: "max_improvement_cycles",label: "Max Improvement Cycles", type: "number" },
    ];

    async function loadSettingsProjectList() {
      if (!projects.length) {
        try { await loadProjects(); } catch (e) { /* handled below */ }
      }
      const sel = document.getElementById("settings-project-select");
      if (!sel) return;
      sel.innerHTML = '<option value="">— select a project —</option>';
      for (const p of projects) {
        const opt = document.createElement("option");
        opt.value = p.id;
        opt.textContent = p.name;
        if (p.id === settingsSelectedProjectId) opt.selected = true;
        sel.appendChild(opt);
      }
      if (settingsSelectedProjectId) loadSettingsProject(settingsSelectedProjectId);
    }

    function onSettingsProjectChange() {
      const sel = document.getElementById("settings-project-select");
      settingsSelectedProjectId = sel ? sel.value : null;
      clearSettingsFeedback();
      if (settingsSelectedProjectId) {
        loadSettingsProject(settingsSelectedProjectId);
      } else {
        document.getElementById("settings-project-content").innerHTML = "";
      }
    }

    async function loadSettingsProject(id) {
      const container = document.getElementById("settings-project-content");
      container.innerHTML = '<div style="color:var(--text-faint);font-size:13px;padding:4px 0;">Loading…</div>';
      try {
        const proj = await api("GET", "/api/projects/" + id);
        settingsProjectOriginal = {};
        for (const f of PROJECT_EDITABLE_FIELDS) {
          settingsProjectOriginal[f.key] = proj[f.key] != null ? proj[f.key] : null;
        }
        container.innerHTML = renderProjectSettingsForm(proj);
      } catch (e) {
        const msg = e.message || "Unknown error";
        const isAuth = msg.includes("401") || msg.toLowerCase().includes("unauthorized");
        container.innerHTML =
          '<div style="color:var(--btn-danger-text);font-size:13px;padding:8px 0;">' +
          esc(isAuth ? "Authentication failed. Check your API token." : "Failed to load project: " + msg) +
          '</div>';
      }
    }

    function renderProjectSettingsForm(proj) {
      let html = '';
      for (const f of PROJECT_EDITABLE_FIELDS) {
        const val = proj[f.key] != null ? proj[f.key] : "";
        const escapedKey = esc(f.key);
        html += '<div class="settings-field-row">';
        html += '<label for="sp-' + escapedKey + '">' + esc(f.label) + '</label>';
        if (f.type === "checkbox") {
          html += '<div style="display:flex;align-items:center;gap:8px;">' +
            '<input id="sp-' + escapedKey + '" type="checkbox" ' +
              'data-proj-key="' + escapedKey + '" ' +
              (val ? 'checked' : '') +
              ' style="width:auto;margin:0;">' +
            '<span style="font-size:12px;color:var(--text-muted);">Enabled</span>' +
          '</div>';
        } else {
          html += '<input id="sp-' + escapedKey + '" type="' + esc(f.type || "text") + '" ' +
            'data-proj-key="' + escapedKey + '" ' +
            'value="' + esc(String(val)) + '" ' +
            'autocomplete="off"' +
            (f.type === "number" ? ' step="any"' : '') +
            '>';
        }
        if (f.hint) html += '<div class="settings-note">' + esc(f.hint) + '</div>';
        html += '</div>';
      }
      return html;
    }

    // ── Save dispatcher ───────────────────────────────────────────────────────

    async function saveSettings() {
      clearSettingsFeedback();
      if (settingsCurrentTab === "global") {
        await saveGlobalSettings();
      } else {
        await saveProjectSettings();
      }
    }

    async function saveGlobalSettings() {
      const inputs = document.querySelectorAll("#settings-panel-global [data-settings-key]");
      const changed = {};
      inputs.forEach(input => {
        const key = input.dataset.settingsKey;
        const newVal = input.value.trim();
        const origVal = settingsGlobalOriginal[key] != null ? String(settingsGlobalOriginal[key]) : "";
        if (newVal !== origVal) {
          changed[key] = newVal === "" ? null : newVal;
        }
      });

      if (!Object.keys(changed).length) {
        showSettingsFeedback("No changes to save.", false);
        return;
      }

      const btn = document.getElementById("settings-save-btn");
      btn.disabled = true;
      try {
        const data = await api("PUT", "/api/settings", changed);
        showSettingsFeedback("Global settings saved.", false);
        // Re-render with returned editable data
        const container = document.getElementById("settings-global-content");
        // Update originals
        for (const [key, entry] of Object.entries(data)) {
          settingsGlobalOriginal[key] = entry && entry.value != null ? entry.value : null;
        }
        container.innerHTML = renderGlobalSettings({ editable: data, readonly: {} });
        // Full reload to get readonly back
        loadGlobalSettings();
      } catch (e) {
        const msg = e.message || "Unknown error";
        const isAuth = msg.includes("401") || msg.toLowerCase().includes("unauthorized");
        showSettingsFeedback(isAuth ? "Authentication failed." : "Error: " + msg, true);
      } finally {
        btn.disabled = false;
      }
    }

    async function saveProjectSettings() {
      if (!settingsSelectedProjectId) {
        showSettingsFeedback("Select a project first.", true);
        return;
      }
      const inputs = document.querySelectorAll("#settings-panel-project [data-proj-key]");
      const changed = {};
      inputs.forEach(input => {
        const key = input.dataset.projKey;
        let newVal;
        if (input.type === "checkbox") {
          newVal = input.checked;
        } else if (input.type === "number") {
          newVal = input.value.trim() === "" ? null : Number(input.value);
        } else {
          newVal = input.value.trim() === "" ? null : input.value.trim();
        }
        const origVal = settingsProjectOriginal[key];
        // Compare by string for most, direct for booleans/numbers
        const origStr = origVal != null ? String(origVal) : "";
        const newStr = newVal != null ? String(newVal) : "";
        if (origStr !== newStr) {
          changed[key] = newVal;
        }
      });

      if (!Object.keys(changed).length) {
        showSettingsFeedback("No changes to save.", false);
        return;
      }

      const btn = document.getElementById("settings-save-btn");
      btn.disabled = true;
      try {
        await api("PATCH", "/api/projects/" + settingsSelectedProjectId, changed);
        showSettingsFeedback("Project settings saved.", false);
        // Refresh local projects cache and re-render
        await loadProjects();
        // Reload project form with fresh data
        await loadSettingsProject(settingsSelectedProjectId);
      } catch (e) {
        const msg = e.message || "Unknown error";
        const isAuth = msg.includes("401") || msg.toLowerCase().includes("unauthorized");
        showSettingsFeedback(isAuth ? "Authentication failed." : "Error: " + msg, true);
      } finally {
        btn.disabled = false;
      }
    }

    // Esc key closes settings modal (and other modals if none open)
    document.addEventListener("keydown", function(event) {
      if (event.key !== "Escape") return;
      if (document.getElementById("settings-overlay").classList.contains("open")) {
        closeSettings();
      } else if (document.getElementById("harness-overlay").classList.contains("open")) {
        closeHarnessAbout();
      } else if (document.getElementById("spec-chat-overlay").classList.contains("open")) {
        closeSpecChat();
      }
    });

    // ── End Settings modal ────────────────────────────────────────────────────

    // ── Unified Lifecycle View (Spec | Plan | Run) ────────────────────────────

    // An item is identified by project AND path: two projects can hold the
    // same docs/superpowers/specs/... name, and once this view spans more
    // than one project a bare path selects the wrong row.
    function lifecycleKey(item) {
      return String(item.project_id || "") + "::" + String(item.spec_path || "");
    }

    function lifecycleScope() {
      return globalProjectFilter === "all"
        ? projects
        : projects.filter(project => project.id === globalProjectFilter);
    }

    function lifecycleScopeLabel() {
      if (globalProjectFilter === "all") return "All Projects";
      const project = projects.find(p => p.id === globalProjectFilter);
      return project ? project.name : "1 project";
    }

    async function loadLifecycle() {
      // Honour the global project filter, like visiblePlans() does for every
      // other list. This used to fetch the singly-selected project id, which
      // under "All Projects" is whatever loadProjects left behind (that is,
      // projects[0]), so the view showed ONE arbitrary project's specs under
      // a header counting "N Plans".
      const scope = lifecycleScope();
      const collected = [];
      for (const project of scope) {
        let items;
        try {
          items = await api("GET", "/api/projects/" + project.id + "/lifecycle");
        } catch (error) {
          // The endpoint reads the repo, so one unreachable clone must not
          // blank every other project's specs.
          continue;
        }
        for (const item of items) {
          collected.push({ ...item, project_id: project.id, projectName: project.name });
        }
      }
      lifecycleItems = collected;
      if (!lifecycleItems.some(item => lifecycleKey(item) === selectedLifecycle)) {
        selectedLifecycle = lifecycleItems.length ? lifecycleKey(lifecycleItems[0]) : null;
      }
      renderLifecycleView();
    }

    function currentLifecycleItem() {
      return lifecycleItems.find(item => lifecycleKey(item) === selectedLifecycle) || null;
    }

    function renderLifecycleView() {
      const c = document.getElementById("view-container");
      const rows = lifecycleItems.map(it =>
        '<button class="master-row' + (selectedLifecycle === lifecycleKey(it) ? ' selected' : '') +
        '" type="button" onclick="selectLifecycle(' + jsArg(lifecycleKey(it)) + ')">' +
        '<div class="row-main"><div class="row-name">' + esc(it.title) + '</div>' +
        '<div class="row-meta">' + esc(it.projectName || "") + ' &middot; ' + esc(it.stage) + '</div></div>' +
        badge(it.stage) + '</button>'
      ).join("") || '<div class="empty-list">No specs yet — use + Create Spec</div>';
      const detail = selectedLifecycle
        ? renderLifecycleDetail(currentLifecycleItem())
        : '<div class="detail-empty">Select an item</div>';
      c.innerHTML =
        '<div class="master-panel"><div class="master-header">' +
        '<div class="master-title">' + lifecycleItems.length + ' Plans' +
        '<span class="master-scope">' + esc(lifecycleScopeLabel()) + '</span></div>' +
        '<button class="btn btn-compact" type="button" onclick="loadLifecycle()">Refresh</button></div>' +
        '<div class="master-list">' + rows + '</div></div>' +
        '<div class="detail-panel">' + detail + '</div>';
    }

    function selectLifecycle(key) { selectedLifecycle = key; lifecycleSegment = "spec"; renderLifecycleView(); loadLifecycleDoc(); }
    function setLifecycleSegment(seg) { lifecycleSegment = seg; renderLifecycleView(); loadLifecycleDoc(); }

    let lifecycleDocCache = {};

    function renderLifecycleDetail(it) {
      if (!it) return '<div class="detail-empty">Not found</div>';
      const seg = (k, label, on) => '<button class="json-toggle-btn' + (lifecycleSegment === k ? ' active' : '') +
        '"' + (on ? '' : ' disabled') + ' type="button" onclick="setLifecycleSegment(\'' + k + '\')">' + label + '</button>';
      const segmented = '<div class="json-toggle">' + seg("spec", "Spec", true) +
        seg("plan", "Plan", !!it.plan_path) + seg("run", "Run", !!it.run_id) + '</div>';
      let body = "";
      if (lifecycleSegment === "run") {
        const run = plans.find(p => p.id === it.run_id);
        body = run ? renderPlanDetail(run) : '<div class="detail-empty">Run not loaded — open Refresh</div>';
      } else {
        const path = lifecycleSegment === "spec" ? it.spec_path : it.plan_path;
        const cached = lifecycleDocCache[String(it.project_id) + "::" + String(path)];
        let promote = "";
        if (lifecycleSegment === "plan" && it.plan_path && !it.run_id) {
          promote = '<div class="detail-actions"><button class="btn btn-primary" type="button" onclick="promoteLifecycle(\'' + esc(it.plan_path) + '\')">Promote to Run</button></div>';
        } else if (lifecycleSegment === "spec" && it.spec_path && !it.plan_path) {
          promote = '<div class="detail-actions"><button class="btn btn-primary" type="button" onclick="generatePlanForLifecycle(\'' + esc(it.spec_path) + '\')">Generate Plan</button></div>';
        }
        let cardBody;
        if (!path) {
          cardBody = '<div class="detail-empty">No ' + (lifecycleSegment === "spec" ? "specification" : "plan") +
            ' document linked. For legacy plans, the spec lives in the repo&rsquo;s docs/ directory.</div>';
        } else if (cached === undefined) {
          cardBody = "Loading…";
        } else if (!cached.trim()) {
          cardBody = '<div class="detail-empty">This ' + (lifecycleSegment === "spec" ? "specification" : "plan") +
            ' document is empty. For legacy plans, the original spec content lives in the repo&rsquo;s docs/ directory.</div>';
        } else {
          cardBody = renderMarkdown(cached);
        }
        body = '<div class="detail-content"><div class="detail-card">' +
          cardBody + '</div>' + promote + '</div>';
      }
      return '<div class="detail-header"><div class="detail-title">' + esc(it.title) + '</div>' +
        '<div class="detail-subtitle">' + esc(it.projectName || "") + '</div>' +
        '<div style="margin-top:8px;">' + segmented + '</div></div>' + body;
    }

    async function loadLifecycleDoc() {
      const it = currentLifecycleItem();
      if (!it || lifecycleSegment === "run") return;
      const path = lifecycleSegment === "spec" ? it.spec_path : it.plan_path;
      // The cache is keyed by project too: the same path in two projects is
      // two different documents.
      const cacheKey = String(it.project_id) + "::" + String(path);
      if (!path || lifecycleDocCache[cacheKey] !== undefined) { renderLifecycleView(); return; }
      try {
        const doc = await api("GET", "/api/projects/" + it.project_id + "/doc-raw?path=" + encodeURIComponent(path));
        lifecycleDocCache[cacheKey] = doc.content;
      } catch (e) { lifecycleDocCache[cacheKey] = "_Could not load " + path + "_"; }
      renderLifecycleView();
    }

    async function generatePlanForLifecycle(specPath) {
      const it = currentLifecycleItem();
      const projectId = it ? it.project_id : selectedProjectId;
      const btn = event.target; btn.disabled = true; btn.textContent = "Generating…";
      try {
        await api("POST", "/api/specs/plan", { project_id: projectId, spec_path: specPath, notes: "" });
        btn.textContent = "Plan generated — refreshing…";
        await loadLifecycle();
      } catch (e) { btn.disabled = false; btn.textContent = "Generate Plan"; alert("Generate plan failed: " + e.message); }
    }

    async function promoteLifecycle(planPath) {
      const it = currentLifecycleItem();
      const projectId = it ? it.project_id : selectedProjectId;
      const btn = event.target; btn.disabled = true; btn.textContent = "Promoting…";
      try {
        await api("POST", "/api/plans/promote", { project_id: projectId, plan_path: planPath });
        await loadPlans(); await loadLifecycle();
      } catch (e) { btn.disabled = false; btn.textContent = "Promote to Run"; alert("Promote failed: " + e.message); }
    }

    // ── End Unified Lifecycle View ────────────────────────────────────────────

    ensureToken();
    switchView("dashboard").catch(error => {
      // "Connection failed" is only one of the two things that can happen
      // here, and it is the wrong one for the case a newcomer actually hits.
      // The orchestrator answering 401 means the connection WORKED and the
      // token is wrong, and saying "connection failed" sends the reader to
      // check whether the server is up. The remedy is a button already in the
      // toolbar, so name it.
      const unauthorized = error.status === 401 || error.status === 403;
      const heading = unauthorized
        ? "Not authorized"
        : "Connection failed";
      const remedy = unauthorized
        ? 'The orchestrator answered, but rejected this token. Use the ' +
          '<strong>API Token</strong> button above; the value is AUTH_TOKEN ' +
          'in your install\'s .env.'
        : "";
      document.getElementById("view-container").innerHTML =
        '<div class="detail-empty" style="padding:40px;text-align:center;">' +
        '<div style="margin-bottom:8px;font-weight:700;">' + heading + '</div>' +
        (remedy ? '<div style="color:var(--text-muted);font-size:12px;margin-bottom:8px;">' + remedy + '</div>' : "") +
        '<div style="color:var(--text-muted);font-size:12px;margin-bottom:16px;">' + esc(error.message) + '</div>' +
        '<button class="btn btn-primary" type="button" onclick="switchView(\'dashboard\')">Retry</button></div>';
    });
    pollStatus();
    window.setInterval(pollStatus, 5000);
    // Poll once so the badge is correct before the first (rate-limited,
    // every-few-hours) approvals_digest SSE event ever arrives.
    pollPendingApprovals();
