const els = {
  view: document.getElementById("view"),
  list: document.getElementById("session-list"),
  status: document.getElementById("status"),
  sidebar: document.getElementById("sidebar"),
  scrim: document.getElementById("scrim"),
  main: document.getElementById("main"),
  toggle: document.getElementById("toggle-sidebar"),
  newSession: document.getElementById("new-session"),
};

const state = {
  overview: null,
  sessions: [],
  session: null,
  job: null,
  busy: false,
  error: null,
  allowSearch: false,
  loaded: false,
};

const DRAFT_KEY = "future-agent.web.draft";

const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char],
  );

const fmt = (iso) => {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return esc(iso);
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
};

const clampPercent = (value) => {
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  return Math.min(100, Math.max(0, number));
};

const safeUrl = (value) => {
  try {
    const url = new URL(String(value));
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function api(path, { method = "GET", body } = {}) {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const error = new Error((data && data.error) || `请求失败（${response.status}）`);
    error.fields = (data && data.fields) || [];
    throw error;
  }
  return data;
}

/* ---------- chrome ---------- */

function setSidebar(open) {
  els.sidebar.classList.toggle("open", open);
  els.scrim.hidden = !open;
  els.toggle.setAttribute("aria-expanded", String(open));
}

function renderStatus() {
  const o = state.overview;
  if (!o) {
    els.status.innerHTML = "";
    return;
  }
  els.status.innerHTML = `
    <div class="row" title="${esc(o.model)}">
      <span class="status-dot ${o.model_ready ? "ok" : "warn"}"></span>
      <span>${o.model_ready ? `模型 ${esc(o.model)}` : "模型未配置"}</span>
    </div>
    <div class="row">
      <span class="status-dot ${o.search_ready ? "ok" : "warn"}"></span>
      <span>${o.search_ready ? "检索已配置" : "检索未配置"}</span>
    </div>
    <div class="row" title="${esc(o.data_dir)}">
      <span class="status-dot"></span>
      <span class="mono">${esc(o.data_dir)}</span>
    </div>`;
}

function renderList() {
  if (!state.sessions.length) {
    els.list.innerHTML = '<p class="empty-hint">还没有会话。新建一次预测后会出现在这里。</p>';
    return;
  }
  const current = state.session && state.session.id;
  const queue = state.verificationQueue || [];
  els.list.innerHTML = (queue.length ? `<p class="section-label">到期待核验 ${queue.length}</p>${queue.map(item => `<button class="session-item" data-session="${esc(item.session_id)}">${esc(item.question)}<small>${esc(fmt(item.window_end))} · ${item.status === "disputed" ? "存在争议" : "待核验"}</small></button>`).join("")}<p class="section-label">全部会话</p>` : "") + state.sessions
    .map((session) => {
      const title = session.question || session.preview || "未命名会话";
      const active = session.id === current;
      return `<button class="session-item" type="button" data-session="${esc(session.id)}" ${
        active ? 'aria-current="true"' : ""
      }>
        <span class="title">${esc(title)}</span>
        <span class="meta"><span>${esc(session.stage_label)}</span><span>·</span><span>${esc(
          fmt(session.created_at),
        )}</span></span>
      </button>`;
    })
    .join("");
}

function closeSidebarOnMobile() {
  if (window.matchMedia("(max-width: 860px)").matches) setSidebar(false);
}

/* ---------- views ---------- */

function errorBanner() {
  if (!state.error) return "";
  return `<div class="callout danger" role="alert">
    <div class="callout-title">${esc(state.error)}</div>
    <div class="small">没有自动重试。检查模型或检索配置后可以再次执行。</div>
  </div>`;
}

function renderNew() {
  const o = state.overview || {};
  const canAnalyze = Boolean(o.model_ready);
  const draft = localStorage.getItem(DRAFT_KEY) || "";
  els.view.innerHTML = `
    <section class="hero">
      <h2>把一个可核验的未来问题，写成一段事实。</h2>
      <p>系统会先整理目标与结果定义，再检索公开资料、读取正文、形成判断。数值概率是未经独立校准的主观判断；依据不足时会交付方向、条件分析或说明无法判断。</p>
    </section>
    ${errorBanner()}
    <form class="composer" id="composer" autocomplete="off">
      <div class="field">
        <label for="narrative">原始叙述</label>
        <textarea id="narrative" name="narrative" maxlength="20000" required
          placeholder="例如：到 2026 年 12 月 31 日，某开源项目是否会在 GitHub 上发布 1.0 版本？请写出对象、时间窗口和可核验的结果。">${esc(
            draft,
          )}</textarea>
        <p class="hint">只写事实和待判断的问题。模型服务会接收分析所需叙述；检索仅发送你授权的公开查询，请先检查查询是否含私人信息。⌘/Ctrl + Enter 提交。</p>
      </div>
      <div class="form-row">
        <p class="hint">${
          canAnalyze
            ? "提交后会先整理目标，再由你确认是否开始消耗模型预算。"
            : "模型未配置：可以保存原文；配置后重启服务，再在会话中整理目标。"
        }</p>
        <button class="button primary" type="submit">${canAnalyze ? "开始整理" : "保存原文"}</button>
      </div>
    </form>`;
  const textarea = document.getElementById("narrative");
  textarea.addEventListener("input", () => localStorage.setItem(DRAFT_KEY, textarea.value));
  document.getElementById("composer").addEventListener("submit", onCreate);
}

function stageChip(session) {
  const stage = session.assistance?.stage;
  const label = session.assistance?.stage_label || "尚未开始";
  if (session.assistance?.forecast_id) return `<span class="chip ok">${esc(label)}</span>`;
  if (stage === "unanswerable") return `<span class="chip danger">${esc(label)}</span>`;
  if (stage === "clarification" || stage === "search_authorization")
    return `<span class="chip warn">${esc(label)}</span>`;
  return `<span class="chip accent">${esc(label)}</span>`;
}

function renderWorking(session) {
  const job = state.job;
  const messages = (job?.progress || [])
    .map(
      (line) => `<li><time>${esc(
        new Date(line.at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }),
      )}</time><span>${esc(line.message)}</span></li>`,
    )
    .join("");
  els.view.innerHTML = `
    ${sessionHeader(session)}
    <section class="progress-panel" id="job-panel" aria-live="polite">
      <div class="progress-head"><span class="pulse" aria-hidden="true"></span><span>${esc(
        jobHeadline(job),
      )}</span></div>
      <button class="button" type="button" data-action="cancel-forecast" id="cancel-forecast" hidden>停止分析</button>
      <ul class="progress-log" id="job-log">${messages || "<li><span>正在排队…</span></li>"}</ul>
    </section>
    <p class="hint">任务在单个本地工作线程中执行；不要关闭这个页面，刷新或重新打开会话会自动重连进度，保存的步骤保留在数据库中。</p>`;
}

function jobHeadline(job) {
  const kind = job?.kind;
  const map = {
    prepare: "正在整理目标与结果定义",
    execute: "正在检索、读取并形成判断",
    answer: "正在结合补充信息继续",
    authorize: "正在执行你授权的补充检索",
    freeze: "正在封存判断",
    resolve: "正在登记核验",
    export: "正在导出报告",
  };
  return map[kind] || "正在执行";
}

function sessionHeader(session) {
  const question = session.question?.question || session.assistance?.plan?.question;
  const title = question || "等待整理目标";
  const q = session.question;
  const plan = session.assistance?.plan;
  const windowText =
    (q && q.window_start && q.window_end
      ? `${fmt(q.window_start)} → ${fmt(q.window_end)}`
      : null) ||
    (plan && plan.window_start && plan.window_end
      ? `${fmt(plan.window_start)} → ${fmt(plan.window_end)}`
      : null);
  return `<header class="session-head">
    <div class="chips">
      ${stageChip(session)}
      <span class="chip">${esc(session.status_label)}</span>
      ${session.forecast?.frozen ? '<span class="chip ok">已冻结</span>' : ""}
      ${session.forecast ? `<span class="chip">${esc(session.forecast.delivery_label)}</span>` : ""}
    </div>
    <h2 class="session-title">${esc(title)}</h2>
    <p class="session-sub">${esc(fmt(session.created_at))}${
      windowText ? ` · 窗口 ${esc(windowText)}` : ""
    } · <span class="mono">${esc(session.id.slice(0, 8))}</span></p>
    <button class="button" type="button" data-action="delete-session">删除会话与分析报告</button>
  </header>`;
}

function render() {
  renderStatus();
  renderList();
  if (!state.session) {
    renderNew();
    return;
  }
  if (state.busy) {
    renderWorking(state.session);
    return;
  }
  renderSession();
}

function renderSession() {
  const session = state.session;
  if (state.busy) {
    renderWorking(session);
    return;
  }
  const a = session.assistance || {};
  const forecast = session.forecast;
  const status = session.status;
  const missingModel = state.overview && !state.overview.model_ready;

  els.view.innerHTML = `
    ${sessionHeader(session)}
    ${errorBanner()}
    ${missingModel ? '<div class="callout warn"><div class="callout-title">模型服务未配置</div>可以保存原文和查看历史，但无法生成新的分析与判断。配置 LLM_BASE_URL / LLM_API_KEY 后重启服务。</div>' : ""}
    ${status === "FAILED" && session.error_code ? `<div class="callout danger"><div class="callout-title">上次执行失败</div><span class="mono">${esc(session.error_code)}</span></div>` : ""}
    ${(status === "FAILED" || status === "READING" || status === "FORECASTING" || a.stop_reason?.startsWith("用户已停止") || state.error?.startsWith("已停止")) ? renderRetry(session) : ""}
    ${forecast ? renderReport(session, forecast) : ""}
    ${renderSource(session)}
    ${renderStage(session, a, forecast)}
    ${forecast && !forecast.frozen ? renderFreeze(forecast) : ""}
    ${forecast && forecast.frozen ? renderLifecycle(session, forecast) : ""}
    ${renderFooter(session, forecast)}`;
}

function renderRetry(session) {
  const label = session.status === "READING" ? "上次读取被中断" : "上次调用失败";
  return `<div class="callout warn">
    <div class="callout-title">${label}，需要你确认后重试</div>
    重试会终止旧步骤，保留已消耗的预算与未知费用，再重新整理目标；不会自动重发已失败的调用。
    <div class="actions" style="margin-top:10px"><button class="button primary" type="button" data-action="retry" ${
      state.overview && !state.overview.model_ready ? "disabled" : ""
    }>重试</button></div>
  </div>`;
}

function renderSource(session) {
  const answers = (session.answers || [])
    .map(
      (answer) => `<div class="timeline-item"><span class="when">${esc(
        fmt(answer.created_at),
      )}</span><div class="prose">${esc(answer.text)}</div></div>`,
    )
    .join("");
  return `<section class="panel">
    <div class="panel-head"><h3>原始叙述</h3><span class="hint">${esc(
      fmt(session.created_at),
    )}</span></div>
    <div class="prose">${esc(session.source_text || "（无内容）")}</div>
    ${
      answers
        ? `<details><summary>补充信息 ${session.answers.length} 条</summary><div class="details-body timeline">${answers}</div></details>`
        : ""
    }
  </section>`;
}

function renderStage(session, a, forecast) {
  const stage = a.stage;
  if (!stage) {
    return `<section class="panel">
      <div class="panel-head"><h3>下一步</h3></div>
      <p class="hint">原文已保存。整理目标会调用模型，并可能提出最多三个问题。</p>
      <div class="actions" style="margin-top:14px">
        <button class="button primary" type="button" data-action="prepare" ${
          state.overview && !state.overview.model_ready ? "disabled" : ""
        }>整理目标</button>
      </div>
    </section>`;
  }

  const parts = [];
  parts.push(renderPlan(a));

  if (stage === "clarification" && a.questions?.length) {
    parts.push(`<section class="panel">
      <div class="panel-head"><h3>需要你补充</h3><span class="hint">最多一轮，三个问题以内</span></div>
      <ul class="list">${a.questions
        .map(
          (question) =>
            `<li><strong>${esc(question.kind_label)}</strong>：${esc(question.question)}${
              question.changes_judgment === false ? ' <span class="muted small">（不影响判断）</span>' : ""
            }${question.cannot_research_reason ? `<div class="hint">${esc(question.cannot_research_reason)}</div>` : ""}</li>`,
        )
        .join("")}</ul>
      <form class="inline-form" id="answer-form" style="margin-top:14px">
        <div class="field">
          <label for="answer-text">你的补充</label>
          <textarea id="answer-text" maxlength="8000" required placeholder="只写你确知的事实。不知道时请直接说明「不知道」。"></textarea>
        </div>
        ${searchToggle()}
        <div class="actions">
          <button class="button primary" type="submit" data-role="answer-submit">提交并继续</button>
          <button class="button quiet" type="button" data-action="answer-unknown">这些我不知道</button>
        </div>
      </form>
    </section>`);
  }

  if (stage === "search_authorization") {
    parts.push(`<section class="panel">
      <div class="panel-head"><h3>补充检索需要授权</h3><span class="hint">会产生外部请求与费用</span></div>
      <p class="hint">计划发送以下查询。只包含公开目标，不包含原始叙述中的私人信息。</p>
      <ul class="list" style="margin-top:8px">${(a.pending_queries || [])
        .map(
          (query) =>
            `<li><span class="mono">${esc(query.query)}</span><div class="hint">${esc(
              query.purpose_label,
            )}${query.decision_gap ? ` · ${esc(query.decision_gap)}` : ""}</div></li>`,
        )
        .join("")}</ul>
      <div class="actions" style="margin-top:14px">
        <button class="button primary" type="button" data-action="authorize-yes">允许并继续</button>
        <button class="button" type="button" data-action="authorize-no">不允许，按现有证据判断</button>
      </div>
    </section>`);
  }

  if (stage === "unanswerable") {
    parts.push(`<section class="panel">
      <div class="panel-head"><h3>无法给出判断</h3></div>
      <div class="callout warn"><div class="callout-title">${esc(a.stop_reason || "证据不足以支持判断。")}</div>
      系统不会用猜测填补缺失的概率，也不会把失败写成结论。</div>
      ${
        a.plan?.unavailable_reason
          ? `<p class="hint" style="margin-top:10px">${esc(a.plan.unavailable_reason)}</p>`
          : ""
      }
    </section>`);
  }

  if (!forecast && stage !== "clarification" && stage !== "search_authorization" && stage !== "unanswerable") {
    const usable = a.plan ? a.plan.usable : false;
    parts.push(`<section class="panel">
      <div class="panel-head"><h3>${usable ? "开始分析与判断" : "目标尚不可用"}</h3><span class="hint">${esc(
        a.stage_label,
      )}</span></div>
      <p class="hint">${usable ? "会按计划检索公开资料、读取正文，再由模型在证据范围内形成判断。失败不会自动重试。" : "先补充目标信息，或查看计划中的不可用原因。"}</p>
      ${searchToggle()}
      <div class="actions" style="margin-top:14px">
        <button class="button primary" type="button" data-action="execute" ${usable ? "" : "disabled"} ${
          state.overview && !state.overview.model_ready ? "disabled" : ""
        }>开始分析</button>
        <button class="button quiet" type="button" data-action="prepare">重新整理目标</button>
      </div>
    </section>`);
  }

  if (a.reading || a.budgets) {
    parts.push(renderResearch(a));
  }
  return parts.join("");
}

function searchToggle() {
  return `<label class="check"><input type="checkbox" id="allow-search" ${
    state.allowSearch ? "checked" : ""
  } ${state.overview && !state.overview.search_ready ? "disabled" : ""}> 允许检索公开网页（消耗搜索与读取配额）</label>`;
}

function renderPlan(a) {
  const plan = a.plan;
  if (!plan) return "";
  return `<section class="panel">
    <div class="panel-head"><h3>分析计划</h3><span class="hint">${esc(plan.analysis_kind)} · ${
      plan.duration_days ? `${esc(plan.duration_days)} 天` : "无固定期限"
    }</span></div>
    <div class="grid-2">
      <div class="kv"><span class="k">目标</span><span class="v">${esc(plan.question)}</span></div>
      <div class="kv"><span class="k">结果类型</span><span class="v">${esc(plan.outcome_type)}</span></div>
      <div class="kv"><span class="k">观察窗口</span><span class="v">${esc(
        plan.window_start ? fmt(plan.window_start) : "—",
      )} → ${esc(plan.window_end ? fmt(plan.window_end) : "—")}</span></div>
      <div class="kv"><span class="k">判定规则</span><span class="v">${esc(plan.resolution_rule)}</span></div>
    </div>
    ${
      plan.definition_reason
        ? `<p class="hint" style="margin-top:12px">${esc(plan.definition_reason)}</p>`
        : ""
    }
    <div class="divider" style="margin:14px 0"></div>
    <p class="section-title">结果定义</p>
    <div class="outcome-list" style="margin-top:8px">${plan.outcomes
      .map(
        (outcome) => `<div class="outcome">
          <div class="outcome-key">${esc(outcome.key)}</div>
          <div class="outcome-def">${esc(outcome.definition)}</div>
        </div>`,
      )
      .join("")}</div>
    ${
      plan.queries?.length
        ? `<div class="divider" style="margin:14px 0"></div>
          <p class="section-title">计划查询 ${plan.queries.length} 条</p>
          <div class="stack tight" style="margin-top:8px">${plan.queries
            .map(
              (query) => `<div class="kv"><span class="v mono">${esc(query.query)}</span>
              <span class="k">${esc(query.purpose_label)}${
                query.decision_gap ? ` · ${esc(query.decision_gap)}` : ""
              }</span></div>`,
            )
            .join("")}</div>`
        : ""
    }
    ${
      plan.assumptions?.length
        ? `<details><summary>假设与前提 ${plan.assumptions.length} 条</summary><ul class="list details-body">${plan.assumptions
            .map((item) => `<li>${esc(item)}</li>`)
            .join("")}</ul></details>`
        : ""
    }
    ${plan.unavailable_reason ? `<div class="callout warn" style="margin-top:12px">${esc(plan.unavailable_reason)}</div>` : ""}
  </section>`;
}

function renderResearch(a) {
  const reading = a.reading;
  const budgets = a.budgets;
  return `<section class="panel">
    <div class="panel-head"><h3>研究记录</h3><span class="hint">${esc(
      a.research_rounds || 0,
    )} 轮检索 · ${esc(reading?.citations ?? 0)} 条引用</span></div>
    ${
      reading
        ? `<div class="grid-2">
            <div class="kv"><span class="k">数值依据</span><span class="v">${esc(reading.numeric_status)}</span></div>
            <div class="kv"><span class="k">参照来源</span><span class="v">${esc(reading.source_kind || "—")}</span></div>
            <div class="kv span-2"><span class="k">参照</span><span class="v">${esc(reading.reference || "—")}</span></div>
            ${
              reading.stop_reason
                ? `<div class="kv span-2"><span class="k">停止原因</span><span class="v">${esc(reading.stop_reason)}</span></div>`
                : ""
            }
          </div>`
        : `<p class="hint">还没有读取记录。</p>`
    }
    ${
      a.research_gaps?.length
        ? `<details><summary>仍缺的证据 ${a.research_gaps.length} 项</summary><ul class="list details-body">${a.research_gaps
            .map((gap) => `<li>${esc(gap)}</li>`)
            .join("")}</ul></details>`
        : ""
    }
    ${
      budgets
        ? `<details><summary>预算用量</summary><div class="grid-2 details-body">
            <div class="kv"><span class="k">模型调用</span><span class="v mono">${esc(budgets.calls_used)} / ${esc(budgets.max_calls)}</span></div>
            <div class="kv"><span class="k">Token</span><span class="v mono">${esc(budgets.consumed_tokens)} / ${esc(budgets.max_total_tokens)}</span></div>
            <div class="kv"><span class="k">耗时</span><span class="v mono">${esc(budgets.consumed_seconds)}s / ${esc(budgets.max_run_seconds)}s</span></div>
          </div></details>`
        : ""
    }
  </section>`;
}

function renderReport(session, forecast) {
  const outcomes = (forecast.outcomes || [])
    .map((outcome) => {
      const percent = outcome.percent === null ? null : clampPercent(outcome.percent);
      return `<div class="outcome">
        <div class="outcome-head">
          <div><div class="outcome-key">${esc(outcome.key)}</div><div class="outcome-def">${esc(
            outcome.definition,
          )}</div></div>
          <div class="outcome-pct">${percent === null ? "—" : `${esc(percent)}%`}</div>
        </div>
        ${
          percent === null
            ? ""
            : `<div class="bar" role="img" aria-label="概率 ${esc(percent)}%"><span style="width:${percent}%"></span></div>`
        }
        ${outcome.reasoning ? `<div class="outcome-reason">${esc(outcome.reasoning)}</div>` : ""}
        ${
          outcome.watch_signals?.length
            ? `<div class="outcome-reason">观察信号：${esc(outcome.watch_signals.join("；"))}</div>`
            : ""
        }
      </div>`;
    })
    .join("");

  const list = (items, className = "") =>
    items && items.length
      ? `<ul class="list ${className}">${items.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>`
      : "";

  const evidence = (forecast.evidence || [])
    .map((item) => {
      const url = safeUrl(item.url);
      return `<div class="evidence">
        <div class="evidence-top">
          <div class="evidence-title">${esc(item.title)}</div>
          <span class="chip">${esc(item.purpose_label)}</span>
        </div>
        <blockquote>${esc(item.quote)}</blockquote>
        ${item.relevance_reason ? `<div class="small muted">${esc(item.relevance_reason)}</div>` : ""}
        ${
          url
            ? `<a class="url" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(
                url,
              )}</a>`
            : item.url
              ? `<span class="url">${esc(item.url)}</span>`
              : ""
        }
      </div>`;
    })
    .join("");

  return `<section class="panel">
    <div class="panel-head">
      <h3>判断</h3>
      <span class="hint">${esc(forecast.delivery_label)} · ${esc(forecast.calibration_status || "未校准")}</span>
    </div>
    ${forecast.historical_reference ? `<div class="callout info"><strong>历史参照</strong><p>${esc(forecast.historical_reference.reason)}</p>${forecast.historical_reference.status === "computed" ? `<p>过去12个完整30天窗口：${esc(forecast.historical_reference.numerator)}/${esc(forecast.historical_reference.denominator)}，观察频率 ${esc((forecast.historical_reference.frequency * 100).toFixed(1))}%（不是未来概率）</p>` : ""}${forecast.historical_reference.method_version === "case-reference-1.0" ? `<p>定性案例，不提供统计概率；历史事实重建，未经人工独立批准。</p>${(forecast.historical_reference.cases || []).map(c => `<p><strong>${esc(c.key)}</strong> ${esc(c.question)}<br>历史结果：${esc(c.outcome)}<br>${c.source_urls.map(u => esc(u)).join("；")}</p>`).join("")}` : ""}</div>` : ""}
    ${forecast.historical_reference?.bayesian_estimate ? `<div class="callout info"><p>贝叶斯统计参照：${esc((forecast.historical_reference.bayesian_estimate.predictive_probability * 100).toFixed(1))}%（模型假设下的估计，未经独立校准）</p><p>固定先验 Beta(1,1)；计算 (${esc(forecast.historical_reference.bayesian_estimate.successes)}+1)/(${esc(forecast.historical_reference.bayesian_estimate.trials)}+2)。假设窗口条件独立且发生机制稳定，不自动替代最终判断。</p></div>` : ""}
    <p class="conclusion">${esc(forecast.conclusion || "（没有给出结论）")}</p>
    <div class="callout info"><p>关键依据：${esc(forecast.key_drivers?.[0] || forecast.reference_basis || "未提供")}</p>
    <p>主要未知：${esc(forecast.unknown_effects?.[0] || "未记录关键未知，不代表没有不确定性")}</p>
    <p>改判条件：${esc(forecast.change_conditions?.[0] || "未提供")}</p></div>
    ${forecast.no_numeric_reason ? `<div class="callout info" style="margin-top:12px"><div class="callout-title">没有数值概率</div>${esc(forecast.no_numeric_reason)}</div>` : ""}
    ${
      forecast.refusal_reason
        ? `<div class="callout warn" style="margin-top:12px"><div class="callout-title">拒绝给出判断</div>${esc(forecast.refusal_reason)}</div>`
        : ""
    }
    <div class="divider" style="margin:16px 0"></div>
    <p class="section-title">结果与概率</p>
    <div class="outcome-list" style="margin-top:10px">${outcomes || '<p class="hint">没有结果定义。</p>'}</div>
    ${
      forecast.reference_basis
        ? `<details><summary>参照与调整</summary><div class="details-body stack tight">
            <div class="kv"><span class="k">参照类型</span><span class="v">${esc(forecast.reference_kind || "—")}</span></div>
            <div class="prose">${esc(forecast.reference_basis)}</div>
            ${forecast.differences_and_adjustment ? `<div class="prose muted">${esc(forecast.differences_and_adjustment)}</div>` : ""}
          </div></details>`
        : ""
    }
    ${forecast.key_drivers?.length ? `<details><summary>关键驱动 ${forecast.key_drivers.length} 项</summary><div class="details-body">${list(forecast.key_drivers)}</div></details>` : ""}
    ${forecast.unknown_effects?.length ? `<details><summary>未知影响 ${forecast.unknown_effects.length} 项</summary><div class="details-body">${list(forecast.unknown_effects)}</div></details>` : ""}
    ${forecast.decision_review ? `<div class="callout"><p>判断边界：${esc(forecast.decision_review.synthesis_reason)}</p><p>最强反面解释：${esc(forecast.decision_review.strongest_counterargument)}</p>${forecast.decision_review.conditional_factors?.length ? list(forecast.decision_review.conditional_factors) : ""}</div>` : ""}
    ${forecast.change_conditions?.length ? `<details><summary>结论改变条件</summary><div class="details-body">${list(forecast.change_conditions)}</div></details>` : ""}
    ${forecast.limitations?.length ? `<details><summary>局限与缺口 ${forecast.limitations.length} 项</summary><div class="details-body">${list(forecast.limitations)}</div></details>` : ""}
    ${forecast.assumptions?.length ? `<details><summary>假设</summary><div class="details-body">${list(forecast.assumptions)}</div></details>` : ""}
    ${forecast.action_conditions?.length ? `<details><summary>行动条件</summary><div class="details-body">${list(forecast.action_conditions)}</div></details>` : ""}
    ${
      evidence
        ? `<details><summary>引用证据 ${forecast.evidence.length} 条</summary><div class="details-body evidence-list">${evidence}</div></details>`
        : ""
    }
    <details><summary>方法与版本</summary><div class="grid-2 details-body">
      <div class="kv"><span class="k">方法</span><span class="v mono">${esc(forecast.method)} ${esc(forecast.method_version)}</span></div>
      <div class="kv"><span class="k">模型</span><span class="v mono">${esc(forecast.model_id)}</span></div>
      <div class="kv"><span class="k">提示词</span><span class="v mono">${esc(forecast.prompt_version)}</span></div>
      <div class="kv"><span class="k">证据截止</span><span class="v">${esc(fmt(forecast.evidence_cutoff))}</span></div>
    </div></details>
  </section>`;
}

function renderFreeze(forecast) {
  const outcomes = (forecast.outcomes || [])
    .map(
      (outcome) =>
        `<label class="check block"><input type="checkbox" name="freeze-early" value="${esc(
          outcome.key,
        )}"><span>${esc(outcome.key)}<small>${esc(outcome.definition)}</small></span></label>`,
    )
    .join("");
  return `<section class="panel">
    <div class="panel-head"><h3>冻结判断</h3><span class="hint">冻结后不可覆盖</span></div>
    <p class="hint">冻结会校验并保留证据快照与输入哈希。冻结后只能追加核验记录；修订需要另存为新的预测。</p>
    <form class="inline-form" id="freeze-form" style="margin-top:14px">
      <details><summary>预先指定可提前核验的结果（可选）</summary>
        <p class="hint" style="margin:6px 0">窗口未结束时，只有这里勾选的结果可以用窗口内发生时间提前核验。</p>
        ${outcomes}
      </details>
      <div class="actions">
        <button class="button primary" type="submit" data-role="freeze-submit">冻结判断</button>
      </div>
    </form>
  </section>`;
}

function renderLifecycle(session, forecast) {
  const frozen = forecast.frozen;
  const resolutions = session.resolutions || [];
  const outcomeOptions = (forecast.outcomes || [])
    .map((outcome) => `<option value="${esc(outcome.key)}">${esc(outcome.key)} — ${esc(outcome.definition)}</option>`)
    .join("");
  const evidenceOptions = (forecast.evidence || [])
    .map(
      (item) =>
        `<label class="check block"><input type="checkbox" name="resolution-evidence" value="${esc(
          item.span_id,
        )}"><span>${esc(item.title)}<small>${esc(item.quote.slice(0, 140))}</small></span></label>`,
    )
    .join("");
  const history = resolutions
    .map(
      (item) => `<div class="timeline-item">
        <span class="when">#${esc(item.sequence)} · ${esc(fmt(item.recorded_at))}</span>
        <div><strong>${esc(item.status)}</strong>${item.resolved_outcome ? ` → ${esc(item.resolved_outcome)}` : ""} · ${esc(item.resolver)}</div>
        <div class="small muted">${esc(item.reason)}</div>
        ${item.observed_at ? `<div class="small muted">发生时间：${esc(fmt(item.observed_at))}</div>` : ""}
      </div>`,
    )
    .join("");

  return `<section class="panel">
    <div class="panel-head"><h3>封存与核验</h3><span class="hint">${esc(fmt(frozen.frozen_at))}</span></div>
    <div class="grid-2">
      <div class="kv"><span class="k">输入哈希</span><span class="v mono">${esc(frozen.input_hash.slice(0, 24))}…</span></div>
      <div class="kv"><span class="k">提前核验结果</span><span class="v">${esc(
        frozen.early_resolution_outcomes?.join("、") || "未指定",
      )}</span></div>
      ${frozen.revision_reason ? `<div class="kv span-2"><span class="k">说明</span><span class="v">${esc(frozen.revision_reason)}</span></div>` : ""}
    </div>
    ${history ? `<div class="divider" style="margin:14px 0"></div><p class="section-title">核验记录</p><div class="timeline" style="margin-top:8px">${history}</div>` : ""}
    <div class="divider" style="margin:14px 0"></div>
    <p class="section-title">追加核验</p>
    <form class="inline-form" id="resolve-form" style="margin-top:10px">
      <input type="hidden" id="resolution-expected" value="${esc(session.last_resolution_id || "")}">
      <div class="field">
        <label>核验状态</label>
        <div class="radio-row">
          <label><input type="radio" name="resolution-status" value="pending" checked> 待核验</label>
          <label><input type="radio" name="resolution-status" value="resolved"> 已核实结果</label>
          <label><input type="radio" name="resolution-status" value="disputed"> 存在争议</label>
          <label><input type="radio" name="resolution-status" value="void"> 作废</label>
        </div>
      </div>
      <div class="form-grid">
        <div class="field" id="outcome-field" hidden>
          <label for="resolution-outcome">核实结果</label>
          <select id="resolution-outcome">${outcomeOptions}</select>
        </div>
        <div class="field">
          <label for="resolution-observed">发生时间（可选）</label>
          <input type="datetime-local" id="resolution-observed">
        </div>
        <div class="field">
          <label for="resolution-resolver">核验人</label>
          <input type="text" id="resolution-resolver" maxlength="200" required placeholder="你的名字或标识">
        </div>
        <div class="field span-2">
          <label for="resolution-reason">依据说明</label>
          <textarea id="resolution-reason" maxlength="4000" required placeholder="写明在哪里、何时看到结果，以及为什么它符合冻结的结果定义。"></textarea>
        </div>
      </div>
      <div id="evidence-field" hidden>
        <p class="hint" style="margin-bottom:6px">核实结果必须附预测事件版本中的正文引用证据。</p>
        ${evidenceOptions || '<p class="hint">没有可用引用，无法登记「已核实」。</p>'}
      </div>
      <div class="actions">
        <button class="button primary" type="submit" data-role="resolve-submit">追加核验</button>
      </div>
    </form>
  </section>`;
}

function renderFooter(session, forecast) {
  if (!forecast) return "";
  return `<section class="panel">
    <div class="panel-head"><h3>导出与查看</h3></div>
    <div class="actions">
      <button class="button" type="button" data-action="copy-report">复制 Markdown</button>
      <a class="button" href="/api/forecasts/${esc(forecast.id)}/report.md" target="_blank" rel="noopener">打开原始报告</a>
      <button class="button" type="button" data-action="export-report">保存到数据目录</button>
      <span class="hint" id="export-status" role="status"></span>
    </div>
  </section>`;
}

/* ---------- work ---------- */

function setBusy(busy) {
  state.busy = busy;
  if (!busy) state.job = null;
}

async function startJob(url, body = {}) {
  clearError();
  try {
    const data = await api(url, { method: "POST", body });
    state.job = data.job;
    setBusy(true);
    render();
    pollJob(data.job.id);
  } catch (error) {
    showError(error);
  }
}

async function pollJob(id) {
  let failures = 0;
  while (state.job?.id === id) {
    await sleep(700);
    try {
      const data = await api(`/api/jobs/${id}`);
      if (state.job?.id !== id) return;
      failures = 0;
      state.job = data.job;
      updateJobPanel(data.job);
      if (data.job.status === "done" || data.job.status === "failed") {
        setBusy(false);
        if (data.job.error) state.error = data.job.error;
        if (data.session && (!state.session || state.session.id === data.session.id)) {
          state.session = data.session;
        } else if (state.session) {
          await refreshSession(state.session.id);
        }
        await loadSessions();
        render();
        return;
      }
    } catch (error) {
      failures += 1;
      if (failures >= 3) {
        setBusy(false);
        state.error = `无法读取任务进度：${error.message}`;
        render();
        return;
      }
    }
  }
}

function updateJobPanel(job) {
  const panel = document.getElementById("job-panel");
  if (!panel || !job) return;
  const stop = document.getElementById("cancel-forecast");
  if (stop) {
    stop.hidden = !["prepare", "retry", "execute", "answer", "authorize"].includes(job.kind);
    stop.disabled = !!job.stop_requested;
    stop.textContent = job.stop_requested ? "已请求停止 · 等待当前请求返回" : "停止分析";
  }
  let metrics = document.getElementById("job-metrics");
  if (!metrics) { metrics = document.createElement("p"); metrics.id = "job-metrics"; panel.append(metrics); }
  const m = job.metrics;
  if (m) metrics.textContent = `已取得正文 ${job.documents_read || 0} 份 · 剩余调用 ${Math.max(0, m.max_calls - m.calls_used)} 次 · 记录剩余时间 ${Math.max(0, Math.round(m.max_run_seconds - m.consumed_seconds))} 秒（进行中的调用可能已预留）${job.gaps?.length ? " · 缺口：" + job.gaps.join("；") : ""}`;
  const head = panel.querySelector(".progress-head span:last-child");
  if (head) head.textContent = jobHeadline(job);
  const log = document.getElementById("job-log");
  if (!log) return;
  log.innerHTML = (job.progress || [])
    .map(
      (line) => `<li><time>${esc(
        new Date(line.at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }),
      )}</time><span>${esc(line.message)}</span></li>`,
    )
    .join("");
  log.scrollTop = log.scrollHeight;
}

function showError(error) {
  state.error = error.message || "操作失败";
  if (error.fields && error.fields.length) {
    state.error += `（${error.fields.slice(0, 4).join("、")}）`;
  }
  setBusy(false);
  render();
}

function clearError() {
  state.error = null;
}

/* ---------- data ---------- */

async function loadSessions() {
  const data = await api("/api/sessions");
  state.verificationQueue = data.verification_queue || [];
  state.sessions = data.sessions || [];
  renderList();
}

async function refreshSession(id) {
  const data = await api(`/api/sessions/${id}`);
  if (state.session && state.session.id === id) state.session = data.session;
}

async function openSession(id, { push = true } = {}) {
  clearError();
  setBusy(false);
  try {
    const data = await api(`/api/sessions/${id}`);
    state.session = data.session;
    if (data.active_job) { state.job = data.active_job; state.busy = true; pollJob(data.active_job.id); }
    if (push) history.pushState(null, "", `#/s/${id}`);
    renderList();
    render();
    els.main.focus({ preventScroll: true });
    document.querySelector(".scroller").scrollTop = 0;
    closeSidebarOnMobile();
  } catch (error) {
    showError(error);
  }
}

/* ---------- actions ---------- */

async function deleteSession() {
  if (!window.confirm("删除此会话的原文、补充、分析及数据库报告？此操作不可撤销。已导出的文件和共享正文快照会保留；冻结或实验记录受保护。")) return;
  try {
    await api(`/api/sessions/${sessionId()}`, { method: "DELETE" });
    state.session = null;
    history.pushState(null, "", "#/");
    await loadSessions();
    render();
  } catch (error) { showError(error); }
}


async function onCreate(event) {
  event.preventDefault();
  clearError();
  const textarea = document.getElementById("narrative");
  const text = textarea.value.trim();
  if (text.length < 10) {
    state.error = "请至少写清楚对象、时间和可核验的结果";
    render();
    return;
  }
  try {
    const data = await api("/api/sessions", { method: "POST", body: { text } });
    localStorage.removeItem(DRAFT_KEY);
    state.session = data.session;
    history.pushState(null, "", `#/s/${data.session.id}`);
    await loadSessions();
    render();
    if (state.overview && state.overview.model_ready) {
      await startJob(`/api/sessions/${data.session.id}/prepare`);
    }
  } catch (error) {
    showError(error);
  }
}

function sessionId() {
  return state.session && state.session.id;
}

async function act(action) {
  const id = sessionId();
  if (!id) return;
  const url = `/api/sessions/${id}/${action}`;
  if (action === "execute") {
    return startJob(url, { allow_search: state.allowSearch });
  }
  return startJob(url);
}

async function onAnswer(event) {
  event.preventDefault();
  const textarea = document.getElementById("answer-text");
  const answer = textarea.value.trim();
  if (!answer) {
    state.error = "请填写补充信息；不知道时可以直接说明";
    render();
    return;
  }
  await startJob(`/api/sessions/${sessionId()}/answer`, {
    answer,
    allow_search: state.allowSearch,
  });
}

function onFreeze(event) {
  event.preventDefault();
  const early = [...document.querySelectorAll('input[name="freeze-early"]:checked')].map(
    (input) => input.value,
  );
  if (!window.confirm("冻结会封存当前判断与证据快照，之后不能覆盖。确认冻结？")) return;
  startJob(`/api/sessions/${sessionId()}/freeze`, { early_outcomes: early });
}

function onResolve(event) {
  event.preventDefault();
  const status = document.querySelector('input[name="resolution-status"]:checked').value;
  const resolver = document.getElementById("resolution-resolver").value.trim();
  const reason = document.getElementById("resolution-reason").value.trim();
  if (!resolver || !reason) {
    state.error = "核验人和依据说明都必须填写";
    render();
    return;
  }
  const payload = {
    status,
    resolver,
    reason,
    observed_at: null,
    resolved_outcome: null,
    evidence_ids: [],
    expected_previous: document.getElementById("resolution-expected").value || null,
  };
  const observed = document.getElementById("resolution-observed").value;
  if (observed) payload.observed_at = new Date(observed).toISOString();
  if (status === "resolved") {
    const outcome = document.getElementById("resolution-outcome").value;
    const evidence = [...document.querySelectorAll('input[name="resolution-evidence"]:checked')].map(
      (input) => input.value,
    );
    if (!outcome) {
      state.error = "请选择核实结果";
      render();
      return;
    }
    if (!evidence.length) {
      state.error = "「已核实」必须选择至少一条引用证据";
      render();
      return;
    }
    payload.resolved_outcome = outcome;
    payload.evidence_ids = evidence;
  }
  startJob(`/api/sessions/${sessionId()}/resolve`, payload);
}

async function copyReport(forecastId) {
  try {
    const text = await (await fetch(`/api/forecasts/${forecastId}/report.md`, { cache: "no-store" })).text();
    await navigator.clipboard.writeText(text);
    const target = document.getElementById("export-status");
    if (target) target.textContent = "已复制 Markdown。";
  } catch {
    state.error = "复制失败；可以改用「打开原始报告」。";
    render();
  }
}

async function exportReport(forecastId) {
  const status = document.getElementById("export-status");
  try {
    const data = await api(`/api/forecasts/${forecastId}/export`, { method: "POST" });
    if (status) status.textContent = "正在写入…";
    let job = data.job;
    for (let attempt = 0; attempt < 60; attempt += 1) {
      if (job.status === "done" || job.status === "failed") break;
      await sleep(600);
      job = (await api(`/api/jobs/${job.id}`)).job;
    }
    if (!status) return;
    if (job.status === "done" && job.result) {
      status.innerHTML = `已保存：<span class="mono">${esc(job.result.markdown)}</span>`;
    } else {
      status.textContent = job.error || "导出未完成";
    }
  } catch (error) {
    if (status) status.textContent = error.message;
  }
}

/* ---------- events ---------- */

els.view.addEventListener("submit", (event) => {
  const form = event.target;
  if (form.id === "answer-form") return onAnswer(event);
  if (form.id === "freeze-form") return onFreeze(event);
  if (form.id === "resolve-form") return onResolve(event);
});

els.view.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  if (action === "cancel-forecast") {
    button.disabled = true;
    api(`/api/sessions/${sessionId()}/stop`, { method: "POST" })
      .then((data) => { button.textContent = data.message; })
      .catch((error) => { button.disabled = false; button.textContent = error.message; });
    return;
  }
  if (action === "delete-session") return deleteSession();
  if (action === "retry") {
    if (window.confirm("重试会保留已消耗预算；中断调用费用未知，可能重复计费。继续？")) return act("retry");
    return;
  }
  if (action === "prepare") return act("prepare");
  if (action === "execute") return act("execute");
  if (action === "authorize-yes")
    return startJob(`/api/sessions/${sessionId()}/authorize`, {
      approved: true,
      allow_search: state.allowSearch,
    });
  if (action === "authorize-no")
    return startJob(`/api/sessions/${sessionId()}/authorize`, {
      approved: false,
      allow_search: false,
    });
  if (action === "answer-unknown") {
    return startJob(`/api/sessions/${sessionId()}/answer`, {
      answer: "我不知道相关事实，请按未知处理。",
      allow_search: state.allowSearch,
    });
  }
  if (action === "copy-report" && state.session?.forecast) return copyReport(state.session.forecast.id);
  if (action === "export-report" && state.session?.forecast) return exportReport(state.session.forecast.id);
});

els.view.addEventListener("change", (event) => {
  if (event.target.id === "allow-search") {
    state.allowSearch = event.target.checked;
  }
  if (event.target.name === "resolution-status") {
    const resolved = event.target.value === "resolved";
    document.getElementById("outcome-field").hidden = !resolved;
    document.getElementById("evidence-field").hidden = !resolved;
  }
});

els.list.addEventListener("click", (event) => {
  const button = event.target.closest("[data-session]");
  if (button) openSession(button.dataset.session);
});

els.newSession.addEventListener("click", () => {
  clearError();
  state.session = null;
  state.busy = false;
  state.job = null;
  state.allowSearch = false;
  history.pushState(null, "", "#/");
  renderList();
  render();
  closeSidebarOnMobile();
  const textarea = document.getElementById("narrative");
  if (textarea) textarea.focus();
});

els.toggle.addEventListener("click", () => setSidebar(!els.sidebar.classList.contains("open")));
els.scrim.addEventListener("click", () => setSidebar(false));

document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    const form = event.target.closest("form");
    if (form) form.requestSubmit();
  }
  if (event.key === "Escape" && els.sidebar.classList.contains("open")) {
    setSidebar(false);
  }
});

window.addEventListener("popstate", () => {
  const id = location.hash.match(/^#\/s\/([0-9a-f-]{36})$/i);
  if (id) {
    openSession(id[1], { push: false });
  } else {
    state.session = null;
    renderList();
    render();
  }
});

/* ---------- boot ---------- */

async function boot() {
  els.view.innerHTML = '<div class="loading"><span class="spinner" aria-hidden="true"></span><span>正在读取本地数据…</span></div>';
  try {
    const [overview, sessions] = await Promise.all([api("/api/health"), api("/api/sessions")]);
    state.overview = overview;
    state.sessions = sessions.sessions || [];
    renderStatus();
    renderList();
    const match = location.hash.match(/^#\/s\/([0-9a-f-]{36})$/i);
    if (match) {
      await openSession(match[1], { push: false });
    } else {
      render();
    }
  } catch (error) {
    els.view.innerHTML = `<div class="callout danger"><div class="callout-title">无法连接本地服务</div>${esc(
      error.message,
    )}<div class="small" style="margin-top:6px">请确认服务仍在运行，然后刷新页面。</div></div>`;
  }
}

boot();
