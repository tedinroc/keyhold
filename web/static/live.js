/* Live status page: progress, lock, money, key moments, the PayPal table and console screens over SSE.
 * Events: snapshot (everything), state (ledger row, PayPal read, progress, PayPal table), log (one
 * timeline row with its key moment), thumb (a console screenshot), end (the rescue is final and its
 * webhooks arrived). States inside the live span update in place; a money decision reloads the page
 * once, to show the end layout. */
(function () {
  "use strict";
  var body = document.body;
  if (body.getAttribute("data-page") !== "rescue") return;
  var K = window.Keyhold;
  var byId = K.byId;
  var rescueId = body.getAttribute("data-rescue-id");
  var pageState = body.getAttribute("data-state");
  var LIVE = ["HELD", "ACTIVE", "PROBING"];
  var KEEP = { agent: 3, recheck: 1, warranty: 1 };
  var seenLog = {};
  var seenMoment = {};
  document.querySelectorAll("#timeline [data-id]").forEach(function (item) { seenLog[item.getAttribute("data-id")] = true; });
  document.querySelectorAll("#moments [data-id]").forEach(function (item) { seenMoment[item.getAttribute("data-id")] = true; });
  var reloading = false;

  function maybeReload(state) {
    if (reloading || !state || state === pageState) return;
    if (LIVE.indexOf(state) >= 0 && LIVE.indexOf(pageState) >= 0) {
      pageState = state;
      body.setAttribute("data-state", state);
      return;
    }
    reloading = true;
    K.reloadTo();
  }

  /* ---- PayPal table ---- */
  function tickList(ticks) {
    var wrap = K.el("span", "ticks");
    ticks.forEach(function (t) {
      var tick = K.el("span", "tick");
      tick.appendChild(K.icon("i-check"));
      tick.appendChild(document.createTextNode(t.at));
      if (t.label) {
        tick.appendChild(document.createTextNode(" "));
        tick.appendChild(K.el("span", "tl", t.label));
      }
      wrap.appendChild(tick);
    });
    return wrap;
  }

  function renderPayPal(pt) {
    if (!pt) return;
    K.setText(byId("paypal-checked"), pt.checked);
    var table = byId("paypal-table");
    var empty = byId("paypal-empty");
    if (table) table.hidden = Boolean(pt.empty);
    if (empty) empty.hidden = !pt.empty;
    var tbody = byId("paypal-rows");
    if (tbody) {
      var sig = JSON.stringify(pt.rows);
      if (tbody.getAttribute("data-sig") !== sig) {
        tbody.setAttribute("data-sig", sig);
        tbody.textContent = "";
        pt.rows.forEach(function (r) {
          var tr = K.el("tr", r.absent ? "absent" : "");
          tr.setAttribute("role", "row");
          tr.setAttribute("data-object", r.key);
          var cell = function (cls, text) {
            var td = K.el("td", cls, text);
            td.setAttribute("role", "cell");
            return td;
          };
          var obj = cell("c-obj");
          obj.appendChild(K.el("span", "obj", r.object));
          if (r.id) {
            var id = K.el("span", "id", r.short_id);
            id.title = r.id;
            obj.appendChild(id);
          }
          tr.appendChild(obj);
          var status = cell("c-status");
          status.appendChild(K.el("span", "pill " + r.pill, r.status_text));
          tr.appendChild(status);
          tr.appendChild(cell("c-mean", r.means));
          tr.appendChild(cell("c-amt amt", r.amount));
          var apiCell = cell("c-api");
          if (r.api.length) apiCell.appendChild(tickList(r.api));
          r.api_waiting.forEach(function (w) { apiCell.appendChild(K.el("span", "muted", w)); });
          if (!r.api.length && !r.api_waiting.length) apiCell.appendChild(K.el("span", "muted", "—"));
          if (r.final_capture) {
            var fin = K.el("span", "final", "final");
            fin.title = "final_capture=true: the rest of the hold is released, no second capture";
            apiCell.appendChild(fin);
          }
          tr.appendChild(apiCell);
          var hook = cell("c-hook");
          if (r.webhook.length) hook.appendChild(tickList(r.webhook));
          else hook.appendChild(K.el("span", "muted", r.webhook_empty));
          tr.appendChild(hook);
          tbody.appendChild(tr);
        });
      }
    }
    var errors = byId("paypal-errors");
    if (errors) {
      errors.textContent = "";
      (pt.errors || []).forEach(function (text) { errors.appendChild(K.el("li", "", text)); });
    }
  }

  function waitingForWebhook(pt) {
    return Boolean(pt && pt.rows && pt.rows.some(function (r) { return r.api.length && r.webhook_empty === "not yet" && !r.webhook.length; }));
  }

  /* ---- state ---- */
  var lastTable = null;
  function renderState(state) {
    if (!state) return;
    if (state.progress) {
      K.renderProgress(state.progress);
      K.renderMoney(state.progress.money);
      var m = state.progress.money;
      if (m) K.setText(byId("ss-money"), m.label + " $" + m.amount);
      K.setText(byId("ss-lock"), state.progress.lock && state.progress.lock.state === "lent" ? "Keyboard: agent" : "Keyboard locked");
    }
    if (state.paypal_table) { renderPayPal(state.paypal_table); lastTable = state.paypal_table; }
    // The warranty row of the money tale counts up while the checks run.
    var w = state.progress && state.progress.warranty;
    var row = byId("tale-warranty");
    if (w && row && w.total) {
      K.setText(row.querySelector(".h3"), "Warranty: " + w.done + " of " + w.total + " checks so far");
      if (w.last_at) K.setText(row.querySelector("time"), w.last_at);
    }
    var rescue = state.rescue;
    if (rescue) {
      K.setText(byId("raw-state"), rescue.state);
      var runActive = state.run ? state.run.active : false;
      var stopForm = byId("stop-form");
      if (stopForm && (LIVE.indexOf(rescue.state) < 0 || !runActive)) {
        stopForm.hidden = true;
        if (byId("stop-help")) byId("stop-help").hidden = true;
        if (byId("sticky-stop")) byId("sticky-stop").setAttribute("data-hidden", "1");
        document.documentElement.classList.remove("has-top-bar");
      }
      // A path's own line ("Path 2: press Stop now") stays; the plain one says what Stop does now.
      var helpLine = byId("stop-help");
      if (rescue.state === "PROBING" && helpLine && !helpLine.hasAttribute("data-path") &&
          (byId("stop-button") || { getAttribute: function () { return null; } }).getAttribute("aria-disabled") !== "true") {
        K.setText(byId("stop-help"), "One press. Releases the hold now. You pay $0, even if the check would pass.");
      }
      if (rescue.terminal && !runActive) setFinal(true, waitingForWebhook(lastTable));
      maybeReload(rescue.state);
    }
    var checks = byId("invariants");
    if (checks && state.invariants) {
      checks.textContent = "";
      if (state.invariants.length) {
        checks.appendChild(K.el("span", "pill problem", "problem"));
        checks.appendChild(document.createTextNode(" " + state.invariants.join("; ")));
      } else {
        checks.appendChild(K.el("span", "pill ok", "ok"));
        checks.appendChild(document.createTextNode(" captured ≤ authorized ≤ quote, one capture at most, no void after capture, refund ≤ captured."));
      }
    }
  }

  /* ---- key moments (inserted by time; repeating rows keep only their latest few) ---- */
  function momentRow(mo) {
    var li = K.el("li", "mo arrive");
    li.setAttribute("data-id", mo.id);
    li.setAttribute("data-at", mo.at || "");
    li.setAttribute("data-group", mo.group || "");
    li.appendChild(K.el("time", "", K.clock(mo.at)));
    var ic = K.el("span", "mo-ic" + (mo.tone ? " " + mo.tone : ""));
    ic.appendChild(K.icon(mo.icon));
    li.appendChild(ic);
    var what = K.el("p", "what");
    if (mo.title) what.title = mo.title; // e.g. PayPal's webhook event code
    if (mo.strong) {
      what.appendChild(K.el("strong", "", mo.strong));
      what.appendChild(document.createTextNode(" "));
    }
    if (mo.code) {
      what.appendChild(K.el("code", "", mo.code));
      what.appendChild(document.createTextNode(" "));
    }
    if (mo.text) what.appendChild(document.createTextNode(mo.text));
    li.appendChild(what);
    return li;
  }

  function insertByTime(list, node) {
    var at = node.getAttribute("data-at") || "";
    var children = list.children;
    for (var i = children.length - 1; i >= 0; i--) {
      if ((children[i].getAttribute("data-at") || "") <= at) {
        list.insertBefore(node, children[i].nextSibling);
        return;
      }
    }
    list.insertBefore(node, list.firstChild);
  }

  function addMoment(mo) {
    var list = byId("moments");
    if (!list || !mo || seenMoment[mo.id]) return;
    seenMoment[mo.id] = true;
    insertByTime(list, momentRow(mo));
    var limit = KEEP[mo.group];
    if (limit) {
      var same = list.querySelectorAll('[data-group="' + mo.group + '"]');
      for (var i = 0; i < same.length - limit; i++) same[i].remove();
    }
    list.querySelectorAll(".newest").forEach(function (n) { n.classList.remove("newest"); });
    if (list.lastElementChild) list.lastElementChild.classList.add("newest");
    var empty = byId("moments-empty");
    if (empty) empty.hidden = true;
    if (mo.strong === "Refused:") {
      var callout = byId("firewall-callout");
      var counter = byId("refusal-count");
      if (callout && counter) {
        var refused = Number(counter.getAttribute("data-n") || 0) + 1;
        counter.setAttribute("data-n", String(refused));
        K.setText(counter, refused + " command" + (refused === 1 ? "" : "s"));
        K.setText(byId("refusal-last"), mo.code || "");
        callout.hidden = false;
      }
    }
  }

  /* ---- full log ---- */
  function addLog(entry) {
    var key = String(entry.id);
    if (seenLog[key]) return;
    seenLog[key] = true;
    var list = byId("timeline");
    if (list) {
      var cls = entry.group + (entry.tone === "warn" ? " warn" : "") + (entry.muted ? " muted-row" : "");
      var item = K.el("li", cls);
      item.setAttribute("data-id", key);
      item.setAttribute("data-at", entry.at || "");
      item.appendChild(K.el("time", "", K.clock(entry.at)));
      item.appendChild(K.el("span", "tag", entry.group_label));
      var txt = K.el("span", "txt", entry.plain || entry.text);
      if (entry.plain) txt.title = entry.text;
      item.appendChild(txt);
      insertByTime(list, item);
      var n = list.children.length;
      K.setText(byId("log-count"), n + " rows");
      K.setText(byId("show-all-count"), String(n));
    }
    if (entry.kind === "agent_type") {
      var typed = byId("typed");
      if (typed) {
        K.setText(byId("typed-text"), String(entry.text || "").replace(/^typed: /, ""));
        typed.hidden = false;
      }
    }
    if (entry.moment) addMoment(entry.moment);
  }

  /* ---- live flag: Live, Reconnecting, Updates paused, Final ---- */
  var finalNow = byId("live-flag") && byId("live-flag").getAttribute("data-final") === "1";
  function note(text, withReload) {
    var n = byId("live-note");
    if (!n) return;
    n.textContent = "";
    if (!text) { n.hidden = true; return; }
    n.appendChild(document.createTextNode(text + " "));
    if (withReload) {
      var b = K.el("button", "btn btn-link", "Reload");
      b.type = "button";
      b.addEventListener("click", function () { window.location.reload(); });
      n.appendChild(b);
    }
    n.hidden = false;
  }
  function setFinal(final, waiting) {
    var flag = byId("live-flag");
    if (!flag) return;
    finalNow = final;
    if (final) flag.setAttribute("data-final", "1");
    else flag.removeAttribute("data-final");
    flag.removeAttribute("data-conn");
    K.setText(byId("live-text"), final ? "Final" : "Live");
    if (final && waiting) note("Final for PayPal. Waiting for PayPal's webhook (the second tick), usually under a minute.");
    else if (final) note("");
  }
  function lost(paused) {
    var flag = byId("live-flag");
    if (!flag || finalNow) return;
    flag.setAttribute("data-conn", "lost");
    K.setText(byId("live-text"), paused ? "Updates paused" : "Reconnecting…");
    note(paused ? "The live connection closed. The page checks again every 5 seconds." : "", paused);
  }

  /* ---- Stop: one press. The button says aria-disabled while the press is sent (a disabled
   * button would drop keyboard focus); a failure is announced and focus goes back to Stop. ---- */
  var stopForm = byId("stop-form");
  if (stopForm) {
    var stopButton = byId("stop-button");
    var help = byId("stop-help");
    var stopError = byId("stop-error");
    stopForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      if (stopButton.getAttribute("aria-disabled") === "true") return;
      stopButton.setAttribute("aria-disabled", "true");
      K.setText(byId("stop-label"), "Stopping…");
      if (stopError) stopError.hidden = true;
      try {
        var data = await K.api("POST", "/rescues/" + encodeURIComponent(rescueId) + "/stop");
        if (help) { help.removeAttribute("data-path"); K.setText(help, data.detail); }
        K.setText(byId("stop-label"), data.voided ? "Hold released" : "Stopped");
        maybeReload(data.state);
      } catch (error) {
        if (stopError) {
          stopError.textContent = "";
          stopError.hidden = false;
          window.setTimeout(function () { stopError.textContent = error.message + " Press Stop again."; }, 50);
        }
        stopButton.removeAttribute("aria-disabled");
        K.setText(byId("stop-label"), "Stop and void the hold");
        stopButton.focus();
      }
    });
  }

  /* ---- an end page's next path, while a warranty keeps the only test server: "opens in about
   * N min" counts down (common.js); at zero the countdown and the wait note go ---- */
  var opens = document.querySelector(".next .opens .free-in");
  if (opens) {
    var openAt = Date.now() + Number(opens.getAttribute("data-seconds") || 0) * 1000;
    var openTimer = window.setInterval(function () {
      if (Date.now() < openAt) return;
      window.clearInterval(openTimer);
      document.querySelectorAll(".next .opens").forEach(function (node) { node.hidden = true; });
      if (byId("slot-note")) byId("slot-note").hidden = true;
    }, 5000);
  }

  /* ---- phone: a thin Stop bar once the money box has scrolled away ---- */
  var stickyStop = byId("sticky-stop");
  var moneyBox = byId("money");
  if (stickyStop && moneyBox && "IntersectionObserver" in window) {
    new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        var form = byId("stop-form");
        var show = Boolean(!entry.isIntersecting && entry.boundingClientRect.top < 0 && form && !form.hidden);
        stickyStop.setAttribute("data-hidden", show ? "0" : "1");
        // Keyboard focus scrolled under the bar would be hidden: pad the scroll port while it shows.
        document.documentElement.classList.toggle("has-top-bar", show);
      });
    }, { threshold: 0 }).observe(moneyBox);
  }

  /* ---- "Show all N rows" opens the full log and moves focus to it ---- */
  var showAll = byId("show-all");
  if (showAll) {
    showAll.addEventListener("click", function (event) {
      var log = byId("full-log");
      if (!log) return;
      event.preventDefault();
      log.open = true;
      var summary = log.querySelector("summary");
      log.scrollIntoView({ block: "start" });
      if (summary) summary.focus({ preventScroll: true });
    });
  }

  /* ---- the stream, with a polling fallback ---- */
  var pollTimer = null;
  function poll() {
    if (pollTimer) return;
    pollTimer = window.setInterval(pollOnce, 5000);
  }

  /* One snapshot without the stream: the paused poller, a slow watchdog (a stalled connection in a
   * background tab or behind a proxy never reports an error) and a check whenever the page becomes
   * visible again. Rows already shown are skipped by addLog and addMoment. */
  var watchdog = null;
  async function pollOnce() {
    try {
      var snap = await K.api("GET", "/api/rescues/" + encodeURIComponent(rescueId));
      renderState(snap);
      (snap.timeline || []).forEach(function (entry) { addLog(entry); });
      (snap.moments || []).forEach(addMoment);
      (snap.thumbs || []).forEach(function (t) { if (K.console) K.console.add(t); });
      if (snap.rescue && snap.rescue.terminal && !(snap.run && snap.run.active)) {
        if (pollTimer) window.clearInterval(pollTimer);
        if (watchdog) window.clearInterval(watchdog);
        setFinal(true, false);
      }
    } catch (e) { /* try again on the next tick */ }
  }
  if (!finalNow) {
    watchdog = window.setInterval(pollOnce, 12000);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) pollOnce(); });
  }

  if (!window.EventSource) { if (!finalNow) poll(); return; }
  var events = new EventSource("/rescues/" + encodeURIComponent(rescueId) + "/events");
  var ended = false;
  events.addEventListener("open", function () {
    var flag = byId("live-flag");
    if (flag && flag.getAttribute("data-conn") === "lost" && !finalNow) {
      flag.removeAttribute("data-conn");
      K.setText(byId("live-text"), "Live");
      note("");
    }
  });
  events.addEventListener("error", function () {
    if (ended) return;
    if (events.readyState === EventSource.CLOSED) { lost(true); poll(); }
    else lost(false);
  });
  events.addEventListener("snapshot", function (event) {
    var snap = JSON.parse(event.data);
    renderState(snap);
    (snap.timeline || []).forEach(addLog);
    (snap.moments || []).forEach(addMoment);
    (snap.thumbs || []).forEach(function (t) { if (K.console) K.console.add(t); });
  });
  events.addEventListener("state", function (event) { renderState(JSON.parse(event.data)); });
  events.addEventListener("log", function (event) { addLog(JSON.parse(event.data)); });
  events.addEventListener("thumb", function (event) { if (K.console) K.console.add(JSON.parse(event.data)); });
  events.addEventListener("end", function () {
    ended = true;
    events.close();
    setFinal(true, false);
    var form = byId("stop-form");
    if (form) form.hidden = true;
  });
})();
