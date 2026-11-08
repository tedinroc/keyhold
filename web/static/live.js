/* Live status page: PayPal state, ticks, timeline and console screens over SSE.
 * Events: snapshot (everything), state (ledger row + PayPal read), log (one timeline row),
 * thumb (a console screenshot), end (the rescue is final and its webhooks arrived). */
(function () {
  "use strict";
  var body = document.body;
  if (body.getAttribute("data-page") !== "rescue") return;
  var K = window.Keyhold;
  var rescueId = body.getAttribute("data-rescue-id");
  var seen = {};
  document.querySelectorAll("#timeline [data-id]").forEach(function (item) { seen[item.getAttribute("data-id")] = true; });
  var PRE_CAPTURE = ["QUOTED", "HOLD_PENDING", "HELD", "ACTIVE", "PROBING"];

  function byId(id) { return document.getElementById(id); }
  function setText(id, text) { var node = byId(id); if (node) node.textContent = text; }

  /* ---- money headline ---- */
  function renderMoney(money) {
    if (!money) return;
    var box = byId("money");
    box.className = "money money-" + money.tone;
    setText("money-label", money.label);
    setText("money-amount", "$" + money.amount);
    setText("money-sentence", money.sentence);
    var warning = byId("money-warning");
    warning.textContent = money.warning || "";
    warning.hidden = !money.warning;
  }

  /* ---- PayPal objects ---- */
  function renderPayPal(paypal) {
    if (!paypal) return;
    ["order", "authorization", "capture", "refund"].forEach(function (key) {
      var row = document.querySelector('#paypal-table tr[data-object="' + key + '"]');
      if (!row) return;
      var cells = row.querySelectorAll("td");
      var obj = paypal[key];
      cells[0].textContent = "";
      cells[0].appendChild(obj ? K.el("code", "", obj.id) : K.el("span", "muted", "—"));
      cells[1].textContent = "";
      cells[1].appendChild(obj ? K.el("span", "pill pill-status", obj.status) : K.el("span", "muted", "—"));
      cells[2].textContent = obj && obj.amount ? "$" + obj.amount : "—";
      var when = "";
      if (obj) {
        if (key === "authorization") when = obj.expiration_time ? "expires " + String(obj.expiration_time).slice(0, 19).replace("T", " ") : "";
        else if (obj.create_time) when = String(obj.create_time).slice(0, 19).replace("T", " ");
        if (key === "capture" && obj.final_capture) when += " · final_capture";
      }
      cells[3].textContent = when;
    });
    if (paypal.checked_at) {
      var source = body.getAttribute("data-mode") === "offline" ? "FakePayPal" : "the PayPal sandbox";
      setText("paypal-checked", "Read from " + source + " at " + K.clock(paypal.checked_at) + " UTC.");
    }
    var errors = byId("paypal-errors");
    errors.textContent = "";
    (paypal.errors || []).forEach(function (text) { errors.appendChild(K.el("li", "", text)); });
  }

  /* ---- first and second ticks ---- */
  function renderTicks(ticks) {
    if (!ticks) return;
    var tbody = document.querySelector("#ticks-table tbody");
    tbody.textContent = "";
    if (!ticks.length) {
      var empty = K.el("tr");
      var cell = K.el("td", "muted", "No money call yet.");
      cell.colSpan = 4;
      empty.appendChild(cell);
      tbody.appendChild(empty);
      return;
    }
    ticks.forEach(function (tick) {
      var row = K.el("tr");
      var head = K.el("th", "", tick.op);
      head.scope = "row";
      row.appendChild(head);
      var rid = K.el("td");
      rid.appendChild(K.el("code", "", tick.request_id));
      row.appendChild(rid);
      var api = K.el("td", "mono small");
      api.appendChild(tick.api_at ? K.el("span", "tick tick-on", K.clock(tick.api_at))
        : K.el("span", "tick", tick.status.toLowerCase() + (tick.error ? ": " + tick.error : "")));
      row.appendChild(api);
      var hook = K.el("td", "mono small");
      if (!tick.webhook) hook.appendChild(K.el("span", "muted", "none for this call"));
      else if (tick.webhook_at) hook.appendChild(K.el("span", "tick tick-on", K.clock(tick.webhook_at)));
      else hook.appendChild(K.el("span", "tick", "waiting"));
      row.appendChild(hook);
      tbody.appendChild(row);
    });
  }

  /* ---- state, run, checks ---- */
  function renderState(state) {
    if (!state) return;
    renderMoney(state.money);
    renderPayPal(state.paypal);
    renderTicks(state.ticks);
    var rescue = state.rescue;
    if (rescue) {
      setText("state-pill", rescue.state);
      if (state.run && state.run.detail) setText("run-detail", state.run.detail);
      else if (rescue.end_reason) setText("run-detail", rescue.end_reason);
      var stopForm = byId("stop-form");
      var runActive = state.run ? state.run.active : false;
      if (stopForm && (PRE_CAPTURE.indexOf(rescue.state) < 0 || !runActive)) stopForm.hidden = true;
    }
    var checks = byId("invariants");
    if (checks && state.invariants) {
      checks.textContent = "";
      if (state.invariants.length) {
        checks.appendChild(K.el("span", "pill pill-warn", "problem"));
        checks.appendChild(document.createTextNode(" " + state.invariants.join("; ")));
      } else {
        checks.appendChild(K.el("span", "pill pill-captured", "ok"));
        checks.appendChild(document.createTextNode(" captured ≤ authorized ≤ quote, one capture at most, no void after capture, refund ≤ captured."));
      }
    }
  }

  /* ---- timeline ---- */
  function addLog(entry) {
    var key = String(entry.id);
    if (seen[key]) return;
    seen[key] = true;
    var list = byId("timeline");
    var atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
    var className = "tl tl-" + entry.group + (entry.tone ? " tl-" + entry.tone : "") + (entry.muted ? " tl-muted" : "") + " tl-new";
    var item = K.el("li", className);
    item.setAttribute("data-id", key);
    item.appendChild(K.el("time", "mono", K.clock(entry.at)));
    item.appendChild(K.el("span", "tl-tag", entry.group_label));
    item.appendChild(K.el("span", "tl-text", entry.text));
    list.appendChild(item);
    if (atBottom) list.scrollTop = list.scrollHeight;
  }

  /* ---- console screens ---- */
  function addThumb(thumb) {
    var figure = byId("screen");
    var img = K.el("img");
    img.src = thumb.url;
    img.alt = "Console screen at " + K.clock(thumb.at) + " UTC, " + thumb.label;
    img.width = thumb.width;
    img.height = thumb.height;
    figure.textContent = "";
    figure.classList.remove("screen-none");
    figure.appendChild(img);
    var strip = byId("thumbs");
    var item = K.el("li");
    var link = K.el("a");
    link.href = thumb.url;
    var small = K.el("img");
    small.src = thumb.url;
    small.alt = img.alt;
    small.loading = "lazy";
    small.width = 120;
    small.height = Math.round(120 * thumb.height / thumb.width);
    link.appendChild(small);
    item.appendChild(link);
    strip.appendChild(item);
    while (strip.children.length > 12) strip.removeChild(strip.firstChild);
    strip.scrollLeft = strip.scrollWidth;
  }

  function setFinal(final) {
    var flag = byId("live-flag");
    if (!flag) return;
    if (final) flag.setAttribute("data-final", "1");
    else flag.removeAttribute("data-final");
    setText("live-text", final ? "Final" : "Live");
  }

  /* ---- Stop: two presses within six seconds ---- */
  var stopForm = byId("stop-form");
  if (stopForm) {
    var stopButton = byId("stop-button");
    var help = byId("stop-help");
    var armedUntil = 0;
    stopForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      var now = Date.now();
      if (now > armedUntil) {
        armedUntil = now + 6000;
        stopButton.setAttribute("data-armed", "1");
        stopButton.textContent = "Press again to void the hold";
        window.setTimeout(function () {
          if (Date.now() >= armedUntil && !stopButton.disabled) {
            stopButton.removeAttribute("data-armed");
            stopButton.textContent = "Stop and void the hold";
          }
        }, 6100);
        return;
      }
      stopButton.disabled = true;
      stopButton.textContent = "Stopping…";
      try {
        var data = await K.api("POST", "/rescues/" + encodeURIComponent(rescueId) + "/stop");
        help.textContent = data.detail;
        stopButton.textContent = data.voided ? "Voided" : "Stopped";
      } catch (error) {
        help.textContent = error.message;
        stopButton.disabled = false;
        stopButton.textContent = "Stop and void the hold";
      }
    });
  }

  if (!window.EventSource) return;
  var events = new EventSource("/rescues/" + encodeURIComponent(rescueId) + "/events");
  events.addEventListener("snapshot", function (event) {
    var snap = JSON.parse(event.data);
    renderState(snap);
    (snap.timeline || []).forEach(addLog);
    setFinal(Boolean(snap.rescue && snap.rescue.terminal && !(snap.run && snap.run.active)));
  });
  events.addEventListener("state", function (event) { renderState(JSON.parse(event.data)); });
  events.addEventListener("log", function (event) { addLog(JSON.parse(event.data)); });
  events.addEventListener("thumb", function (event) { addThumb(JSON.parse(event.data)); });
  events.addEventListener("end", function () {
    events.close();
    setFinal(true);
    var form = byId("stop-form");
    if (form) form.hidden = true;
  });
})();
