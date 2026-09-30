/* 面板前端。原生 JS，无构建链（视觉目标卡「必须可达的约束」）。
 *
 * 三条纪律：
 *  1. **金额一律当字符串渲染**。服务端给的是 Decimal 定点字面量，
 *     这里不做 parseFloat 再 toFixed —— 那正是对账差一分钱事故的来源。
 *  2. **按钮禁用态由服务端给的 options 算**，前端不自己判状态机。
 *  3. **SSE 为主、轮询为兜底**：断线时每 8 秒拉一次全量，
 *     连上后立刻停掉轮询（两个都跑会让屏幕上的行序跳动）。
 */

(function () {
  "use strict";

  var CHANNELS = window.RECONQ_CHANNELS || [];
  var FALLBACK_POLL_MS = 8000;

  /**
   * 状态机动作名 → 按钮的写法。
   *
   * `action` 用的是 reconq.ledger.models.LedgerAction 的取值（服务端
   * `action_options()` 逐条给回来的就是它），不是接口上的 decision 词。
   * 两者的差别只有一处：**submit 与 approve 在 HTTP 上都是
   * `decision=confirm`**，由服务端按当前状态分派到 submit() 还是
   * approve()；`review: true` 走主管那一条写入口 /review。
   *
   * 这张表只决定「按钮长什么样、点哪个端点、带什么 decision 词」，
   * **不决定按钮该不该出现** —— 该不该出现由服务端的 action_options()
   * 说了算，前端只是把它给的动作逐条渲染出来。
   */
  var ACTION_BUTTONS = {
    submit: { label: "提交处置结论", cls: "primary", endpoint: "decide", decision: "confirm" },
    approve: { label: "复核通过", cls: "primary", endpoint: "decide", decision: "confirm" },
    reject: { label: "驳回", cls: "danger", endpoint: "decide", decision: "reject" },
    withdraw: { label: "撤回", cls: "", endpoint: "decide", decision: "withdraw" }
  };

  var state = {
    period: "",
    rows: [],
    selectedKey: "",
    channels: [],
    options: [],
    version: -1,
    sse: null,
    pollTimer: null,
    groups: [],
    groupMode: true,
    collapsed: {},
    role: "",
    vocab: null
  };

  var $ = function (id) { return document.getElementById(id); };

  // —— 工具 ——

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function getJSON(url) {
    return fetch(url, { headers: { "Accept": "application/json" } }).then(function (r) {
      if (!r.ok) throw new Error(url + " → HTTP " + r.status);
      return r.json();
    });
  }

  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (data) {
        return { ok: r.ok, status: r.status, data: data };
      });
    });
  }

  /** 成因 → 视觉目标卡第二节那六色。按 ambiguity_kind 归，兜底走待人工判定蓝。 */
  function causeClass(row) {
    var kind = row.ambiguity_kind || "";
    if (!kind) return "c-unknown";
    if (kind.indexOf("duplicate") >= 0 || kind.indexOf("重复") >= 0) return "c-duplicate";
    if (kind.indexOf("his_only") >= 0 || kind.indexOf("漏") >= 0) return "c-missing";
    if (kind.indexOf("cross_period") >= 0 || kind.indexOf("跨") >= 0) return "c-cross-period";
    if (kind.indexOf("refund") >= 0 || kind.indexOf("退费") >= 0) return "c-refund";
    return "c-unknown";
  }

  function causeText(row) {
    if (row.cause_draft && row.cause_draft.cause) return row.cause_draft.cause;
    if (row.ambiguity_kind) return row.ambiguity_kind;
    return row.check_name || row.check_path;
  }

  // —— 渲染：顶栏 ——

  function renderOverview(ov) {
    var gate = $("gate");
    if (!ov || !ov.gate) {
      gate.className = "gate gate-none";
      gate.textContent = "未扫描";
    } else {
      gate.className = "gate gate-" + ov.gate;
      gate.textContent = { green: "绿 · 可结账", yellow: "黄 · 待归因", red: "红 · 不能结账" }[ov.gate] || ov.gate;
    }
    $("status-zh").textContent = (ov && ov.status_zh) || "未扫描";

    var ok = (ov && ov.channels_ok) || 0;
    var total = CHANNELS.length || (ov && ov.channels_total) || 0;
    var pct = total ? Math.round((ok / total) * 100) : 0;
    $("progress-bar").style.width = pct + "%";
    var reason = (ov && ov.reason) ? "　" + ov.reason : "";
    $("progress-text").textContent = "已扫 " + ok + "/" + total + " 渠道" + reason;
  }

  // —— 渲染：左栏 ——

  function renderChannels(cards) {
    var box = $("channel-list");
    clear(box);
    if (!cards || !cards.length) {
      box.appendChild(el("p", "empty", "本期账期未选择"));
      return;
    }
    cards.forEach(function (c) {
      var card = el("div", "channel-card badge-" + c.badge);
      var head = el("div", "channel-name");
      head.appendChild(el("b", null, c.channel_zh));
      head.appendChild(el("span", "channel-badge", c.badge_zh));
      card.appendChild(head);

      if (c.status === "not_fetched") {
        card.appendChild(el("div", "channel-meta", "上次扫描：—"));
        card.appendChild(el("div", "channel-err", (c.error_code || "NOT_SCANNED") + "　" + (c.detail || "")));
      } else {
        // 金额原样渲染：服务端给什么就显示什么，不在浏览器里做算术。
        card.appendChild(el("div", "channel-meta",
          "笔数 " + c.row_count + " / 合计 ¥" + c.total_amount + " / fail " + c.checks_failed));
        if (c.dirty_rows) {
          card.appendChild(el("div", "channel-meta", "隔离脏行 " + c.dirty_rows + " 行"));
        }
        if (c.error_code) {
          card.appendChild(el("div", "channel-err", c.error_code + "　" + (c.detail || "")));
        }
      }
      box.appendChild(card);
    });
  }

  // —— 渲染：中栏 ——

  /** 读出当前筛选条件。**空值就是「不过滤」**，不传 undefined 的键。 */
  function currentFilter() {
    var out = {};
    var v;
    if ((v = $("f-state").value)) out.state = v;
    if ((v = $("f-channel").value)) out.channel = v;
    if ((v = $("f-cause").value)) out.cause_code = v;
    if ((v = $("f-kind").value)) out.ambiguity_kind = v;
    if ((v = $("f-min").value.trim())) out.amount_min = v;
    if ((v = $("f-max").value.trim())) out.amount_max = v;
    if ($("f-weak").checked) out.only_weak_match = "1";
    if ($("f-ambig").checked) out.only_ambiguous = "1";
    return out;
  }

  function diffQuery() {
    var qs = new URLSearchParams();
    var f = currentFilter();
    Object.keys(f).forEach(function (k) { qs.set(k, f[k]); });
    var text = qs.toString();
    return text ? "?" + text : "";
  }

  /** 筛选反馈条。**脏行被排除必须说出来** —— 少掉的那几条得知道去哪捞。 */
  function renderFilterNote(payload) {
    var box = $("filter-note");
    var dropped = payload.dropped_unknown_amount || 0;
    var active = payload.filter && payload.filter.active;
    if (!dropped && !active) { box.hidden = true; clear(box); return; }
    box.hidden = false;
    clear(box);
    if (active) {
      box.appendChild(el("span", "fn-main",
        "筛出 " + (payload.matched || 0) + " / " + (payload.total || 0) + " 条"));
    }
    if (dropped) {
      // 金额不可用的行被区间条件排除了。**两种成因说同一句**：
      // 上游没给金额的与给了但不可解析的，对「按金额筛」的后果一样；
      // 它们的去处不同（一个问渠道方、一个去隔离区），但那是排障时
      // 按码分开查的事，面板上给两种颜色的告警只会让人以为是两类问题。
      box.appendChild(el("span", "fn-warn",
        dropped + " 条无可用差额（上游未给金额或金额不可解析），未计入金额筛选"));
    }
  }

  /** 一行差异。金额走等宽 + 右对齐（可读性硬要求，见 CSS 注释）。 */
  function renderRow(row) {
    var node = el("div", "diff-row " + causeClass(row));
    node.setAttribute("data-key", row.key);
    node.setAttribute("role", "button");
    node.setAttribute("tabindex", "0");
    // 读屏与键盘：行本身可聚焦，回车/空格等同点击。**不只靠颜色**
    // 传达选中态 —— 见 CSS 里 .selected 的左边框。
    node.setAttribute("aria-pressed", row.key === state.selectedKey ? "true" : "false");
    if (row.key === state.selectedKey) node.classList.add("selected");
    if (row.resolved) node.classList.add("resolved");
    node.appendChild(el("span", "chan", row.channel_zh));
    node.appendChild(el("span", "cause", causeText(row)));
    node.appendChild(el("span", "amount", row.diff_amount ? "¥" + row.diff_amount : "—"));
    node.appendChild(el("span", "rowno", row.first_row_no ? ("行 " + row.first_row_no) : ""));
    node.appendChild(el("span", "st st-" + stClass(row.ledger_status), row.ledger_status_zh));
    // 长单号可复制：交办单要粘给渠道方，截断了对方就没法回查。
    if (row.order_no) {
      var no = el("span", "ono", row.order_no);
      no.title = "点击复制单号";
      no.addEventListener("click", function (ev) {
        ev.stopPropagation();
        copyText(row.order_no, no);
      });
      node.appendChild(no);
    }
    node.addEventListener("click", function () { selectRow(row.key); });
    node.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); selectRow(row.key); }
    });
    return node;
  }

  /** 状态 → CSS 类。**文字本身也带状态名**（不是只靠颜色）。 */
  function stClass(status) {
    var map = {
      "待归因": "pending",
      "待确认": "confirming",
      "待主管复核": "supervisor",
      "已确认": "confirmed",
      "已驳回": "rejected",
      "未开单": "unopened"
    };
    return map[status] || "other";
  }

  function copyText(text, node) {
    var done = function () {
      var old = node.textContent;
      node.textContent = "已复制";
      setTimeout(function () { node.textContent = old; }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { window.prompt("复制单号", text); });
    } else {
      window.prompt("复制单号", text);
    }
  }

  /** 按成因分组渲染。组头可整组收起；组内行序与不分组时一致。 */
  function renderGroups(groups) {
    var box = $("group-list");
    clear(box);
    if (!groups.length) {
      box.appendChild(el("p", "empty", "本期没有可归组的差异"));
      return;
    }
    groups.forEach(function (g) {
      var wrap = el("section", "group");
      var head = el("header", "group-head");
      var caret = el("span", "group-caret", state.collapsed[g.group_key] ? "▸" : "▾");
      head.appendChild(caret);
      head.appendChild(el("b", null, g.cause_zh));
      head.appendChild(el("span", "group-count", g.count + " 条"));
      if (g.amount) head.appendChild(el("span", "group-amount", "¥" + g.amount));
      if (g.pending) head.appendChild(el("span", "group-pending", "未了结 " + g.pending));
      head.appendChild(el("span", "group-src", g.source_zh));
      head.appendChild(el("span", "group-key", g.group_key));
      head.addEventListener("click", function () {
        state.collapsed[g.group_key] = !state.collapsed[g.group_key];
        renderGroups(groups);
      });
      wrap.appendChild(head);
      if (!state.collapsed[g.group_key]) {
        var body = el("div", "group-body");
        var members = state.rows.filter(function (r) {
          return g.member_keys.indexOf(r.key) >= 0;
        });
        if (!members.length) {
          body.appendChild(el("p", "empty", "本组的条目已被当前筛选排除"));
        } else {
          members.forEach(function (r) { body.appendChild(renderRow(r)); });
        }
        wrap.appendChild(body);
      }
      box.appendChild(wrap);
    });
  }

  function renderDiff(payload) {
    var box = $("diff-list");
    state.rows = payload.rows || [];
    var total = $("diff-total");
    total.textContent = payload.total_known ? "¥" + payload.total_amount : "合计不完整";
    total.style.color = payload.total_known ? "" : "var(--badge-red)";
    var counts = payload.counts || {};
    var parts = [];
    Object.keys(counts).forEach(function (k) { parts.push(k + " " + counts[k]); });
    $("diff-count").textContent = "共 " + state.rows.length + " 条" +
      (parts.length ? " · " + parts.join(" · ") : "");
    renderFilterNote(payload);

    if (state.groupMode) {
      box.hidden = true;
      $("group-list").hidden = false;
      renderGroups(state.groups);
      return;
    }
    $("group-list").hidden = true;
    box.hidden = false;
    clear(box);
    if (!state.rows.length) {
      box.appendChild(el("p", "empty", "本期没有差异（口径全过，或还没扫）"));
      return;
    }
    state.rows.forEach(function (row) { box.appendChild(renderRow(row)); });
  }

  // —— 右栏：决策辅助（四路并列） ——

  /** 一路来源的卡片。**标题里必须带「谁说的」**。 */
  function sourceCard(title, tag, bodyNodes, extra) {
    var sec = el("section", "aid-card " + (extra || ""));
    var head = el("header", "aid-head");
    head.appendChild(el("b", null, title));
    if (tag) head.appendChild(el("span", "aid-tag", tag));
    sec.appendChild(head);
    bodyNodes.forEach(function (n) { sec.appendChild(n); });
    return sec;
  }

  function renderAid(aid) {
    var box = $("aid");
    clear(box);
    if (!aid) { box.hidden = true; return; }
    box.hidden = false;

    // ① 明示哪一路被采用 —— 放在最上面，因为签字前先看这个。
    var adopted = aid.adopted || {};
    var ad = el("div", "aid-adopted " + (adopted.source === "none" ? "aid-none" : ""));
    ad.appendChild(el("div", "aid-adopted-label", "本条采用"));
    ad.appendChild(el("div", "aid-adopted-src", adopted.source_zh || "—"));
    ad.appendChild(el("div", "aid-adopted-why", adopted.reason || ""));
    if (adopted.note) ad.appendChild(el("div", "aid-adopted-note", "经办结论：" + adopted.note));
    box.appendChild(ad);

    // ② 模型草稿
    var m = aid.model_draft;
    box.appendChild(sourceCard("模型草稿", m ? m.label_zh : "无", m ? [
      el("div", "aid-cause", (m.cause_zh || m.cause) + "　置信度 " + m.confidence),
      el("div", null, m.explanation_zh),
      el("div", "aid-refs", "证据行：" + (m.evidence_refs || []).join("、")),
      el("div", "aid-meta", "模型 " + (m.model_name || "未记录") +
        (m.recorded_at ? " · " + m.recorded_at : ""))
    ] : [el("p", "empty", "本期没有模型草稿（模型未配置或已降级）")]));

    // ③ 规则基线 —— 标「未经模型」是硬要求，不能省
    var r = aid.rule_baseline;
    box.appendChild(sourceCard("规则基线", r ? r.label_zh : "无", r ? [
      el("div", "aid-cause", r.zh_name + "　形态 " + r.kind),
      el("div", null, r.detail),
      el("div", "aid-meta", "规则版本 " + r.rule_version +
        (r.partial ? " · 仅形态映射一档（面板不重跑完整基线）" : ""))
    ] : [el("p", "empty", "该形态没有登记规则映射")]));

    // ④ 先例
    var p = aid.precedent || {};
    var pbody = [el("p", "empty", p.note || "")];
    (p.hits || []).forEach(function (hit) {
      pbody.push(el("div", "aid-hit",
        hit.cause_zh + " · 命中 " + hit.hit_count_at + " 次 · " +
        (hit.actor || "未记录") + " · " + (hit.recorded_at || "")));
    });
    box.appendChild(sourceCard("历史先例", p.precedent_hit ? "命中过" : "无命中",
      pbody, p.precedent_hit ? "aid-hit-yes" : ""));

    // ⑤ 反幻觉核对
    var v = aid.verification || {};
    var vbody = [el("div", v.consistent ? "aid-ok" : "aid-bad",
      v.consistent ? "通过" : "不通过：" + v.summary)];
    vbody.push(el("p", "empty", v.note || ""));
    (v.mismatches || []).forEach(function (mm) {
      vbody.push(el("div", "aid-mm",
        (mm.kind_zh || mm.code || mm.layer) + "：" + mm.detail));
    });
    box.appendChild(sourceCard("反幻觉核对", v.label_zh, vbody));
  }

  function loadAid(row) {
    var url = "/api/period/" + encodeURIComponent(state.period) +
      "/discrepancy/" + encodeURIComponent(row.key) + "/aid";
    getJSON(url).then(renderAid, function (err) {
      var box = $("aid");
      clear(box);
      box.hidden = false;
      // 决策辅助拉不到就明说，**不静默留空** —— 空右栏看着像「没什么可看」。
      box.appendChild(el("p", "empty", "决策辅助读取失败：" + err.message));
    });
  }

  // —— 右栏：待办聚合 ——

  function renderTodo(data) {
    var box = $("todo-sum");
    clear(box);
    box.appendChild(el("b", null, data.role_zh + "待办 " + data.count + " 条"));
    if (data.amount_known && data.amount) {
      box.appendChild(el("span", "todo-amount", "合计 ¥" + data.amount));
    } else if (!data.amount_known) {
      box.appendChild(el("span", "todo-amount", "合计不完整"));
    }
    if (data.count) {
      box.appendChild(el("span", "todo-oldest", "最老一条已等 " + data.oldest_text));
    } else {
      box.appendChild(el("span", "todo-oldest", "本期没有待你办的单"));
    }
  }

  function refreshTodo() {
    if (!state.period || !state.role) return Promise.resolve();
    return getJSON("/api/period/" + encodeURIComponent(state.period) +
        "/todo?role=" + encodeURIComponent(state.role))
      .then(renderTodo)
      .catch(function () { /* 账还没扫时角色词表才有值 */ });
  }

  function selectRow(key) {
    state.selectedKey = key;
    var row = null;
    for (var i = 0; i < state.rows.length; i++) {
      if (state.rows[i].key === key) { row = state.rows[i]; break; }
    }
    if (!row) return;
    state.options = row.actions || [];
    state.version = row.ledger_version;
    renderDetail(row);
    // 只改选中态，不重拉列表 —— 整页刷新会让屏幕上的行序跳动。
    // 按 key 找而不是按下标：分组视图下行节点不是 state.rows 的直接投影。
    markSelected();
    loadAid(row);
  }

  function markSelected() {
    var nodes = document.querySelectorAll(".diff-row");
    Array.prototype.forEach.call(nodes, function (n) {
      if (n.getAttribute("data-key") === state.selectedKey) n.classList.add("selected");
      else n.classList.remove("selected");
    });
  }

  function renderDetail(row) {
    var box = $("detail");
    clear(box);
    box.appendChild(el("h3", null, (row.channel_zh || row.channel) + "　" + causeText(row)));

    // ① 台账事实
    var facts = el("section");
    facts.appendChild(el("h4", null, "台账事实"));
    var kv = el("dl", "kv");
    function pair(k, v) {
      kv.appendChild(el("dt", null, k));
      kv.appendChild(el("dd", null, v));
    }
    pair("账期", row.period);
    pair("口径", row.check_path);
    pair("差额", row.diff_amount ? "¥" + row.diff_amount : "（上游未给金额）");
    pair("说明", row.message || "—");
    pair("处置", row.ledger_status_zh + (row.requires_supervisor ? "（存疑账期，须主管复核）" : ""));
    facts.appendChild(kv);
    box.appendChild(facts);

    // ② 证据行号。**刻意不落整行原文** —— 交办单要发给渠道方与信息科，
    //    整行里可能带着渠道自定义列。
    var ev = el("section");
    ev.appendChild(el("h4", null, "证据行号（" + (row.evidence_total || 0) + " 行）"));
    if (row.first_row_no || row.source_file) {
      var ul = el("ul", "evidence-list");
      ul.appendChild(el("li", "rowno", (row.source_file || "（未记源文件）") + ":" + (row.first_row_no || "?")));
      ev.appendChild(ul);
    } else {
      ev.appendChild(el("p", "empty", "这条差异没有定位到具体行"));
    }
    box.appendChild(ev);

    // ③ 成因草稿（模型/规则说的）与人写的结论分栏
    var draft = el("section");
    draft.appendChild(el("h4", null, "成因草稿（" + (row.cause_draft ? row.cause_draft.model_name || "模型" : "无") + "）"));
    if (row.cause_draft) {
      var dbox = el("div", "draft-box");
      dbox.appendChild(el("div", "draft-cause", row.cause_draft.cause));
      dbox.appendChild(el("div", "draft-meta", "置信度 " + row.cause_draft.confidence + " · " + (row.cause_draft.model_name || "")));
      dbox.appendChild(el("div", null, row.cause_draft.explanation_zh));
      draft.appendChild(dbox);
    } else {
      draft.appendChild(el("p", "empty", "还没有归因草稿（模型未配置或已降级）"));
    }
    if (row.rejections && row.rejections.length) {
      var rj = el("p", "reject-box", "草稿被拒 " + row.rejections.length + " 次：" +
        row.rejections.map(function (r) { return r.code; }).join("、"));
      draft.appendChild(rj);
    }
    box.appendChild(draft);

    // ③b 决策辅助容器。内容由 /api/…/aid 单独拉 —— 它要读草稿表、拒因表
    // 与先例留痕表三处，混进列表接口会让每翻一页都白查三张表。
    var aid = el("div", "aid");
    aid.id = "aid";
    aid.hidden = true;
    box.appendChild(aid);

    // ③c 证据展开容器。点「展开证据」才拉 —— 候选行全表与对齐快照
    // 是这一条差异的全部源行，默认全展开会把右栏撑成流水账。
    var evx = el("section", "evbox");
    evx.id = "evbox";
    var evBtn = el("button", "ev-toggle", "展开证据（候选行全表 + 快照引用）");
    evBtn.addEventListener("click", function () { toggleEvidence(row.key); });
    evx.appendChild(evBtn);
    var evBody = el("div", "ev-body");
    evBody.id = "ev-body";
    evBody.hidden = true;
    evx.appendChild(evBody);
    box.appendChild(evx);

    if (row.conclusion) {
      var sec = el("section");
      sec.appendChild(el("h4", null, "经办已写结论"));
      sec.appendChild(el("div", null, row.conclusion));
      box.appendChild(sec);
    }

    // ④ 处置动作
    var act = el("section");
    act.appendChild(el("h4", null, "处置（只能由人点，模型与规则无权）"));
    var bar = el("div", "actions");
    var op = el("input");
    op.type = "text";
    op.placeholder = "操作人工号或姓名";
    bar.appendChild(op);

    var cm = el("input");
    cm.type = "text";
    cm.placeholder = "处置结论（提交必填）";
    bar.appendChild(cm);

    if (row.ledger_status === "未开单") {
      act.appendChild(el("p", "empty", "这条差异还没开成处置单；先跑一次归因或在 CLI 上开单"));
    } else {
      var fb = el("div", "act-feedback");
      fb.id = "act-feedback";
      fb.hidden = true;
      act.appendChild(fb);
      // 按钮表按**状态机给的动作名**（submit / approve / reject / withdraw，
      // 见 reconq.ledger.models.LedgerAction）渲染，标签直接用服务端给的
      // opt.label —— 中文措辞只在 ACTION_ZH 一处写，不在前端再抄一遍。
      //
      // 动作名 ≠ 接口上的 decision 词：submit 与 approve 在 HTTP 上都是
      // decision=confirm（服务端按当前状态分派到 submit() 还是 approve()），
      // 待主管复核那一步走 /review 而不是 /decide。把这层映射写在这里，
      // 而不是按状态 if/else，前端就永远不需要知道状态机的迁移细节。
      (row.actions || []).forEach(function (opt) {
        var spec = ACTION_BUTTONS[opt.action];
        if (!spec) return;
        var b = el("button", spec.cls, opt.label || spec.label);
        b.addEventListener("click", function () {
          // 存疑账期的「复核通过」必须走主管那条写入口：/decide 会按
          // reviewer 兜底角色提交，而状态机对「待主管复核」只认主管 ——
          // 前端传 role=reviewer 想自己把这一关签掉时服务端会挡下，
          // 但与其让人白点一次，不如直接给到对的那个入口。
          var review = row.requires_supervisor && opt.action === "approve";
          decide(row, opt, review ? "review" : spec.endpoint,
                 spec.decision, op.value, cm.value);
        });
        bar.appendChild(b);
      });
      act.appendChild(bar);
      act.appendChild(el("p", "action-hint",
        "当前状态「" + row.ledger_status_zh + "」可做：" +
        (row.actions || []).map(function (a) { return a.label; }).join("、") + "；版本 " + row.version));
    }
    box.appendChild(act);
  }

  /**
   * 提交一次人工处置。
   *
   * ``endpoint`` 是 ``decide`` / ``review``（哪个写入口），``decision`` 是
   * 接口上的处置词：submit 与 approve 都落在 ``confirm`` 上，由服务端按
   * 当前状态分派到 :func:`submit` 还是 :func:`approve`；``reject`` 与
   * ``withdraw`` 原样传。前端不判断当前状态、不自己算该走哪条迁移。
   */
  function decide(row, opt, endpoint, decision, operator, comment) {
    if (!operator) { alert("请先填操作人 —— 处置必须留一个自然人"); return; }
    var feedback = $("act-feedback");
    postJSON("/api/discrepancy/" + encodeURIComponent(row.key) + "/" + endpoint, {
      decision: decision,
      role: (opt.roles && opt.roles[0]) || "",
      operator: operator,
      comment: comment || "",
      expected_version: row.ledger_version
    }).then(function (res) {
      if (!res.ok) {
        // 错误码**原样显示**：面板上要能看到「为什么点了没动」，
        // 而不是一句「操作失败」。被拒的点击同样要能解释。
        showFeedback("err",
          "[" + (res.data.code || res.status) + "] " + (res.data.detail || "处置被拒"),
          operator);
        refreshAll();
        return;
      }
      // 成功：**就地更新**这一行的状态与版本，不等整页重拉 ——
      // 经办人连点两条时，整页刷新会让第一条的反馈先消失再出现。
      var idx = state.rows.findIndex(function (r) { return r.key === row.key; });
      if (idx >= 0) {
        state.rows[idx].ledger_status = res.data.after;
        state.rows[idx].ledger_status_zh = res.data.status_zh;
        state.rows[idx].ledger_version = res.data.version;
        state.rows[idx].actions = res.data.options || [];
      }
      showFeedback("ok",
        res.data.action + " 成功：→ " + res.data.status_zh + "（版本 " + res.data.version + "）",
        operator, res.data.at);
      refreshAll();
    }).catch(function (err) {
      showFeedback("err", "请求失败：" + err.message, operator);
    });
  }

  /** 操作反馈条。**失败带错误码，成功带操作人与时刻** —— 留痕要看得见。 */
  function showFeedback(kind, text, operator, at) {
    var box = $("act-feedback");
    if (!box) return;
    box.className = "act-feedback " + kind;
    clear(box);
    box.appendChild(el("span", "af-text", text));
    var who = operator || "";
    if (at) who += " · " + at;
    if (who) box.appendChild(el("span", "af-who", who));
    box.hidden = false;
  }

  // —— 右栏：证据展开（候选行全表 + 快照引用 + 深链三出口） ——

  /**
   * 展开/收起一条差异的证据。**每次都重新拉**，不复用上一次的结果 ——
   * 处置动作落库后账单行与快照都可能变，缓存下来的行号会让财务科
   * 拿着上一次的证据去跟渠道方对账。
   */
  function toggleEvidence(key) {
    var body = $("ev-body");
    if (!body) return;
    if (!body.hidden && body.getAttribute("data-key") === key) {
      body.hidden = true;
      return;
    }
    body.hidden = false;
    body.setAttribute("data-key", key);
    clear(body);
    body.appendChild(el("p", "empty", "正在取候选行与快照…"));
    var url = "/api/period/" + encodeURIComponent(state.period) +
      "/discrepancy/" + encodeURIComponent(key) + "/evidence";
    getJSON(url)
      .then(function (data) { renderEvidence(body, data); })
      .catch(function (err) {
        clear(body);
        // 展开失败**显示错误**而不是收起：静默收起看起来像「没有证据」，
        // 而真相多半是这条差异已被消解或账期没扫。
        body.appendChild(el("p", "ev-err", "证据展开失败：" + err.message));
      });
  }

  function renderEvidence(box, data) {
    clear(box);

    // ① 候选行全表
    box.appendChild(el("h4", null, "候选行全表（账单库现状，" + (data.candidate_total || 0) + " 行）"));
    if (!data.candidates.length) {
      box.appendChild(el("p", "empty",
        "账单库里没有这个单号的行（口径差异类条目没有渠道单号是正常的）"));
    } else {
      var table = el("table", "ev-table");
      var head = el("tr");
      ["来源", "单号", "金额", "退款", "交易时间", "科室", "结算方式", "定位"].forEach(function (h) {
        head.appendChild(el("th", null, h));
      });
      table.appendChild(el("thead")).appendChild(head);
      var tbody = el("tbody");
      data.candidates.forEach(function (c) {
        var tr = el("tr");
        tr.appendChild(el("td", null, c.side === "channel" ? ("渠道 " + c.channel) : "HIS"));
        tr.appendChild(el("td", "ono", c.order_no || "—"));
        tr.appendChild(el("td", "amount", "¥" + c.amount));
        tr.appendChild(el("td", "amount", c.refund ? ("¥" + c.refund) : "—"));
        tr.appendChild(el("td", null, c.trade_time || "—"));
        tr.appendChild(el("td", null, c.department || "—"));
        tr.appendChild(el("td", null, c.settle_type || "—"));
        tr.appendChild(el("td", "rowno", c.ref));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      box.appendChild(table);
    }

    // ② 快照引用
    box.appendChild(el("h4", null, "对齐快照（拍摄当时，" + (data.snapshot_total || 0) + " 份）"));
    if (!data.snapshots.length) {
      box.appendChild(el("p", "empty", "这条差异没有拍过对齐快照（没跑过 align-pipeline 或结论键对不上）"));
    } else {
      if (data.snapshot_conflict) {
        box.appendChild(el("p", "ev-warn",
          "同键存在多份内容不同的快照：数据在两次扫描之间变过。" +
          "两份都留着，请用 reconq verify --replay 复算，不要手工删"));
      }
      data.snapshots.forEach(function (s) {
        var sec = el("div", "ev-snap");
        sec.appendChild(el("div", "ev-snap-head",
          (s.content_hash || "（无哈希）").slice(0, 12) + "　" +
          (s.kind || "—") + "　" + (s.ambiguity_kind || "无形态") + "　参与方 " + s.participant_total + " 条"));
        sec.appendChild(el("div", "ev-snap-meta",
          "拍摄 " + (s.created_at || "—") + "　口径指纹 " +
          ((s.contract_digest || "").slice(0, 12) || "—") + "　索引版本 " + (s.index_version || "—")));
        var ul = el("ul", "ev-parts");
        (s.participants || []).forEach(function (p) {
          ul.appendChild(el("li", null,
            p.side + "/" + p.channel + "　¥" + p.amount +
            (p.refund ? ("　退款 ¥" + p.refund) : "") +
            (p.is_refund ? "（红冲）" : "") + "　" + (p.ref || "")));
        });
        sec.appendChild(ul);
        box.appendChild(sec);
      });
    }

    if (data.drifted) {
      box.appendChild(el("p", "ev-warn",
        "有 " + (data.drift || []).length + " 处快照行在账单库里已找不到（重扫覆盖或源文件被换）：" +
        (data.drift || []).join("、") + "。" + data.note));
    }

    // ③ 处置单登记的证据行。台账的 evidence_total 只是计数，定位在哪
    //    几行只有开单时写下的那一份知道。
    if ((data.ledger_evidence_rows || []).length) {
      box.appendChild(el("h4", null, "处置单登记的证据行"));
      var le = el("ul", "ev-parts");
      data.ledger_evidence_rows.forEach(function (r) { le.appendChild(el("li", null, r)); });
      box.appendChild(le);
    }

    // ④ 深链三出口
    box.appendChild(el("h4", null, "把这条差异发出去"));
    var links = data.links || {};
    [
      ["深链（粘进企微）", links.deep_link],
      ["面板回跳地址", links.panel_url],
      ["CLI 复算命令", links.cli_command]
    ].forEach(function (pair) {
      var line = el("div", "ev-link");
      line.appendChild(el("span", "ev-link-label", pair[0]));
      var val = el("code", "ev-link-val", pair[1] || "—");
      val.title = pair[1] ? "点击复制" : "本条没有结论键，无法复算";
      if (pair[1]) {
        val.addEventListener("click", function () { copyText(pair[1], val); });
      }
      line.appendChild(val);
      box.appendChild(line);
    });
    if (links.round_trip_ok === false) {
      box.appendChild(el("p", "ev-err", "深链自检未通过：构造与解析不是同一对函数，链接可能指向别处"));
    }
  }

  function renderSettled(rows) {    var box = $("detail").querySelector(".settled-box");
    if (!box) {
      box = el("section", "settled-box");
      $("detail").appendChild(box);
    }
    clear(box);
    box.appendChild(el("h4", null, "已了结结论流（" + (rows || []).length + "）"));
    if (!rows || !rows.length) {
      box.appendChild(el("p", "empty", "本期还没有确认或驳回的处置单"));
      return;
    }
    var ul = el("ul", "settled");
    rows.slice(0, 20).forEach(function (r) {
      var li = el("li");
      var st = el("span", r.status === "已确认" ? "st-confirmed" : "st-rejected", r.status + "　");
      li.appendChild(st);
      li.appendChild(document.createTextNode(r.channel + "　" + (r.conclusion || r.check_path)));
      ul.appendChild(li);
    });
    box.appendChild(ul);
  }

  // —— 数据拉取 ——

  function refreshChannels() {
    if (!state.period) return Promise.resolve();
    return getJSON("/api/period/" + encodeURIComponent(state.period) + "/channels")
      .then(function (data) { state.channels = data.channels || []; renderChannels(state.channels); })
      .catch(function () { /* 首次载入时账期还没扫，忽略 */ });
  }

  function refreshDiff() {
    if (!state.period) return Promise.resolve();
    return getJSON("/api/period/" + encodeURIComponent(state.period) + "/discrepancies" + diffQuery())
      .then(function (data) {
        renderOverview(data.overview);
        renderDiff(data);
        renderSettled(data.settled);
        if (state.selectedKey) {
          for (var i = 0; i < state.rows.length; i++) {
            if (state.rows[i].key === state.selectedKey) { renderDetail(state.rows[i]); break; }
          }
        }
      })
      .catch(function (err) {
        // 筛选参数非法 → 服务端返 4xx。这里**把错误码显示出来**而不是
        // 静默留上一屏的旧数据：那会让「筛选没生效」看起来像「本期没差异」。
        showFilterError(err);
      });
  }

  function refreshGroups() {
    if (!state.period) return Promise.resolve();
    return getJSON("/api/period/" + encodeURIComponent(state.period) + "/groups")
      .then(function (data) {
        state.groups = data.groups || [];
        $("group-note").textContent = data.group_count
          ? ("共 " + data.group_count + " 组" +
             (data.ungrouped ? "，其中口径差异 " + data.ungrouped + " 条" : ""))
          : "";
        if (state.groupMode) renderGroups(state.groups);
      })
      .catch(function () { /* 分组失败时退回不分组视图 */ });
  }

  function refreshAll() {
    syncExportLinks();
    return Promise.all([refreshChannels(), refreshDiff(), refreshGroups(), refreshTodo()]);
  }

  /** 页脚两个下载链接跟着当前账期走。**没有账期就禁用** ——
   *  一个指向空账期的下载链接点下去只会拿到一份空结论单，而那份单子上
   *  照样有指纹头，看起来完全像一份正常的结论单。 */
  function syncExportLinks() {
    [["export-md", "md"], ["export-csv", "csv"]].forEach(function (pair) {
      var a = $(pair[0]);
      if (!a) return;
      if (!state.period) {
        a.removeAttribute("href");
        a.setAttribute("aria-disabled", "true");
        a.classList.add("foot-export-off");
        return;
      }
      a.setAttribute("href", "/api/period/" + encodeURIComponent(state.period) +
        "/export?fmt=" + pair[1]);
      a.removeAttribute("aria-disabled");
      a.classList.remove("foot-export-off");
    });
  }

  function showFilterError(err) {
    var box = $("filter-note");
    box.hidden = false;
    clear(box);
    box.appendChild(el("span", "fn-warn", "筛选条件被服务端拒绝：" + err.message));
  }

  // —— 筛选控件 ——

  /** 用服务端下发的词表填下拉。**前端不硬编码任何枚举。** */
  function fillSelect(sel, items, allLabel) {
    clear(sel);
    var first = el("option", null, allLabel);
    first.value = "";
    sel.appendChild(first);
    items.forEach(function (it) {
      var o = el("option", null, it.zh || it.cause_zh || it.code);
      o.value = it.code;
      sel.appendChild(o);
    });
  }

  function loadVocab() {
    return getJSON("/api/vocab").then(function (v) {
      state.vocab = v;
      fillSelect($("f-state"), v.states.map(function (s) { return { code: s, zh: s }; }), "全部状态");
      fillSelect($("f-channel"), v.channels.map(function (s) { return { code: s, zh: s }; }), "全部渠道");
      fillSelect($("f-cause"), v.causes, "全部成因");
      fillSelect($("f-kind"), v.kinds, "全部形态");
      var rs = $("todo-role");
      clear(rs);
      v.roles.forEach(function (r) {
        var o = el("option", null, r.zh);
        o.value = r.code;
        rs.appendChild(o);
      });
      state.role = v.roles.length ? v.roles[0].code : "";
      rs.addEventListener("change", function () {
        state.role = rs.value;
        refreshTodo();
      });
    });
  }

  function wireFilters() {
    ["f-state", "f-channel", "f-cause", "f-kind"].forEach(function (id) {
      $(id).addEventListener("change", refreshDiff);
    });
    ["f-min", "f-max"].forEach(function (id) {
      // change 而非 input：每敲一个字符发一次请求会把库打满，
      // 而财务科输完金额要按 Tab 才会走。
      $(id).addEventListener("change", refreshDiff);
    });
    $("f-weak").addEventListener("change", refreshDiff);
    $("f-ambig").addEventListener("change", refreshDiff);
    $("f-reset").addEventListener("click", function () {
      ["f-state", "f-channel", "f-cause", "f-kind", "f-min", "f-max"].forEach(function (id) {
        $(id).value = "";
      });
      $("f-weak").checked = false;
      $("f-ambig").checked = false;
      refreshDiff();
    });
    $("g-toggle").addEventListener("change", function () {
      state.groupMode = $("g-toggle").checked;
      $("group-list").hidden = !state.groupMode;
      $("diff-list").hidden = state.groupMode;
      if (state.groupMode) renderGroups(state.groups);
      else {
        clear($("diff-list"));
        state.rows.forEach(function (r) { $("diff-list").appendChild(renderRow(r)); });
        if (!state.rows.length) $("diff-list").appendChild(el("p", "empty", "本期没有差异"));
      }
    });
  }

  // —— SSE ——

  function connectSSE() {
    if (state.sse) { state.sse.close(); state.sse = null; }
    var es = new EventSource("/api/events");
    state.sse = es;
    es.onopen = function () {
      $("sse-dot").className = "dot dot-on";
      $("sse-text").textContent = "实时推送已连接";
      stopPoll();
    };
    es.onerror = function () {
      $("sse-dot").className = "dot dot-off";
      $("sse-text").textContent = "实时推送已断开，正在兜底轮询";
      startPoll();
    };
    ["progress", "channel", "diff", "status"].forEach(function (kind) {
      es.addEventListener(kind, function (ev) {
        var body = JSON.parse(ev.data);
        if (body.period && state.period && body.period !== state.period) return;
        if (kind === "channel") {
          // 单卡就地替换，不重拉整栏：左栏六张卡顺序固定，重绘会让
          // 正在看的那张卡跳走。
          for (var i = 0; i < state.channels.length; i++) {
            if (state.channels[i].channel === body.channel) state.channels[i] = body;
          }
          renderChannels(state.channels);
        } else if (kind === "status") {
          refreshDiff();
        } else {
          refreshAll();
        }
      });
    });
  }

  function startPoll() {
    if (state.pollTimer) return;
    state.pollTimer = setInterval(refreshAll, FALLBACK_POLL_MS);
  }

  function stopPoll() {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  }

  // —— 启动 ——

  /**
   * 深链落地。**两种来源都要认**：
   *
   * 1. ``reconq://period/<期>/diff/<键>`` —— 企微消息里点进来的自定义
   *    协议（Task 46 推送的深链）。浏览器地址栏吃不下自定义 scheme，
   *    面板提供 ``/open?link=<URL 编码后的深链>`` 作为本机承接入口；
   * 2. ``/?period=<期>&diff=<键>`` —— 右栏「面板回跳地址」，可直接
   *    粘进工单。
   *
   * 两种都**必须落到具体那条差异并把右栏展开**（不只切账期）——深链
   * 的全部意义就是省掉「在几百条差异里找那一条」这一步。
   */
  function applyDeepLink(period, diffKey) {
    if (!period) return Promise.resolve();
    var sel = $("period");
    var found = Array.prototype.some.call(sel.options, function (o) { return o.value === period; });
    if (!found) {
      $("progress-text").textContent =
        "深链指向账期 " + period + "，但库里没有这个账期；先跑 reconq scan --period " + period;
      return Promise.resolve();
    }
    sel.value = period;
    state.period = period;
    return refreshAll().then(function () {
      if (!diffKey) return;
      // 深链可能带的是「已消解」的键（企微推的是上周的账期）。这时候
      // 显式说清楚，而不是静默什么都不做 —— 用户点了个链接却毫无反应，
      // 会以为面板坏了。
      var hit = null;
      state.rows.forEach(function (r) { if (r.key === diffKey) hit = r; });
      if (!hit) {
        $("filter-note").hidden = false;
        clear($("filter-note"));
        $("filter-note").appendChild(el("span", "fn-warn",
          "深链指向的差异 " + diffKey + " 不在账期 " + period +
          " 的当前列表里（可能已消解、被当前筛选排除，或渠道重扫后消失）"));
        return;
      }
      selectRow(diffKey);
    });
  }

  function readDeepLink() {
    var qs = new URLSearchParams(window.location.search);
    var link = qs.get("link");
    if (link) {
      // 形态 reconq://period/<期>/diff/<键>：这里只做**切账期 + 选中**，
      // 解析的权威在服务端（audit.wecom.parse_deep_link），前端不另写
      // 一份正则 —— 两份正则迟早分叉。
      var m = /^reconq:\/\/period\/([^/]+)\/diff\/(.+)$/.exec(link);
      if (!m) {
        $("progress-text").textContent = "深链格式不对：" + link;
        return {};
      }
      return { period: decodeURIComponent(m[1]), diffKey: m[2] };
    }
    return { period: qs.get("period") || "", diffKey: qs.get("diff") || "" };
  }

  function boot() {
    loadVocab().catch(function () { /* 词表拉不到时下拉只剩「全部」，筛选仍可用 */ });
    wireFilters();
    var deep = readDeepLink();
    getJSON("/api/periods").then(function (data) {
      var sel = $("period");
      clear(sel);
      if (!data.periods.length) {
        sel.appendChild(el("option", null, "（库里还没有账期）"));
        sel.disabled = true;
        $("progress-text").textContent = "先跑 reconq scan --period 2026-W36 扫一期，面板会自动出现";
        syncExportLinks();
        return;
      }
      data.periods.forEach(function (p) {
        var o = el("option", null, p);
        o.value = p;
        sel.appendChild(o);
      });
      sel.value = deep.period || data.default;
      sel.disabled = false;
      state.period = sel.value;
      sel.addEventListener("change", function () {
        state.period = sel.value;
        state.selectedKey = "";
        refreshAll();
      });
      connectSSE();
      if (deep.period) return applyDeepLink(deep.period, deep.diffKey);
      refreshAll();
    }).catch(function (err) {
      $("progress-text").textContent = "面板读库失败：" + err.message;
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
