/* Shared helpers for the pages, and the renderers for the story pieces the server sends over SSE:
 * the progress strip (desktop and phone), the status sentence, the lock plate and the money box.
 * Untrusted text is only ever set with textContent. A piece is rewritten only when it changed, and
 * a step whose words alone changed ("warranty 2 of 6" -> "3 of 6") keeps its node, so a status
 * change animates once and then holds still. Also: clock times in the visitor's own time, copy
 * buttons, and focus on the status line after a page reloads itself. */
window.Keyhold = (function () {
  "use strict";
  var csrf = document.body.getAttribute("data-csrf") || "";
  var SVG = "http://www.w3.org/2000/svg";

  async function api(method, url, body) {
    var options = {
      method: method,
      credentials: "same-origin",
      headers: { "Accept": "application/json", "X-Keyhold-CSRF": csrf }
    };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    var response = await fetch(url, options);
    var data = null;
    try { data = await response.json(); } catch (e) { data = null; }
    if (!response.ok) {
      var error = new Error((data && data.error) || ("The request failed (HTTP " + response.status + ")."));
      error.status = response.status;
      error.code = data && data.code;
      throw error;
    }
    return data;
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function icon(name, className) {
    var svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("class", className || "ic");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    var use = document.createElementNS(SVG, "use");
    use.setAttribute("href", "#" + name);
    svg.appendChild(use);
    return svg;
  }

  function clock(iso) { return iso ? String(iso).slice(11, 19) : ""; }
  function byId(id) { return document.getElementById(id); }
  function setText(node, text) {
    if (node && node.textContent !== String(text)) node.textContent = String(text);
  }

  /* ---- progress strip ---- */
  var SR = { done: " (done)", current: " (now)", skipped: " (skipped)", failed: " (failed)" };

  function stepSig(s) { return [s.state, s.tone, s.note, s.icon, s.label].join("|"); }
  // Everything but the note: when only the note differs, the node stays and its text changes.
  function baseOf(sig, noteAt) {
    var parts = String(sig || "").split("|");
    parts.splice(noteAt, 1);
    return parts.join("|");
  }

  function buildStep(s) {
    var li = el("li", "st " + s.state + (s.tone ? " " + s.tone : ""));
    li.setAttribute("data-key", s.key);
    li.setAttribute("data-sig", stepSig(s));
    if (s.state === "current") li.setAttribute("aria-current", "step");
    var node = el("span", "st-node");
    node.appendChild(icon(s.icon));
    if (s.state === "done") {
      var badge = el("span", "badge");
      badge.appendChild(icon("i-check"));
      node.appendChild(badge);
    }
    li.appendChild(node);
    var label = el("span", "st-label", s.label);
    label.appendChild(el("span", "sr-only", SR[s.state] || " (next)"));
    li.appendChild(label);
    li.appendChild(el("span", "st-note", s.note));
    return li;
  }

  function outcomeSig(o) { return [o.state, o.label, o.note, o.icon].join("|"); }

  function buildOutcome(o) {
    var li = el("li", "st outcome " + o.state);
    li.setAttribute("data-key", "outcome");
    li.setAttribute("data-sig", outcomeSig(o));
    var node = el("span", "st-node");
    node.appendChild(icon(o.icon));
    node.appendChild(el("span", "o-label", o.label));
    li.appendChild(node);
    li.appendChild(el("span", "st-note", o.note));
    return li;
  }

  /* The phone strip: the line's words change in place; the row of icons is rebuilt only when a
   * step's state, tone or icon (or the outcome) changes, so its badges pop once. */
  function renderPhoneStrip(p) {
    var root = byId("story-m");
    if (!root) return;
    setText(byId("line-lead"), p.line.lead);
    setText(byId("line-text"), p.line.text);
    var sig = p.steps.map(function (s) { return [s.state, s.tone, s.icon].join("|") + ";"; }).join("") +
      [p.outcome.state, p.outcome.label, p.outcome.icon].join("|");
    if (root.getAttribute("data-sig") === sig) return;
    root.setAttribute("data-sig", sig);
    var old = root.querySelector(".sm-row");
    if (old) old.remove();
    var row = el("div", "sm-row");
    row.setAttribute("aria-hidden", "true"); // the icons only repeat the line above, which is read
    p.steps.forEach(function (s, i) {
      var node = el("span", "sm-node " + s.state + (s.tone ? " " + s.tone : ""));
      node.appendChild(icon(s.icon));
      if (s.state === "done") {
        var badge = el("span", "badge");
        badge.appendChild(icon("i-check"));
        node.appendChild(badge);
      }
      row.appendChild(node);
      if (i < p.steps.length - 1) {
        var next = p.steps[i + 1];
        var joined = s.state === "done" && (next.state === "done" || next.state === "current");
        row.appendChild(el("span", "sm-link" + (joined ? " " + (s.tone || "done") : "")));
      }
    });
    var pill = el("span", "sm-pill " + p.outcome.state);
    pill.appendChild(icon(p.outcome.icon));
    pill.appendChild(document.createTextNode(p.outcome.label));
    row.appendChild(pill);
    root.appendChild(row);
  }

  /* The page title follows the status (web/story.py status_title does the same on the server):
   * "Fixed. Charged $9.00." -> "Fixed, charged $9.00 · Typo in sshd_config · Keyhold". */
  function statusTitle(head) {
    head = String(head || "").trim().replace(/\.$/, "");
    var at = head.indexOf(". ");
    if (at >= 0) {
      var rest = head.slice(at + 2);
      head = head.length <= 42 ? head.slice(0, at) + ", " + rest.charAt(0).toLowerCase() + rest.slice(1) : head.slice(0, at);
    }
    return head;
  }

  function renderStatus(p) {
    var status = byId("status");
    if (!status || !p.status) return;
    var s = p.status;
    status.setAttribute("data-state", p.state);
    status.classList.toggle("xl", Boolean(s.xl));
    var sig = s.head + "|" + s.tail;
    if (status.getAttribute("data-sig") !== sig) {
      var suffix = document.body.getAttribute("data-title");
      if (suffix) document.title = statusTitle(s.head) + " · " + suffix;
      status.setAttribute("data-sig", sig);
      status.textContent = "";
      status.appendChild(el("span", "head", s.head));
      if (s.tail) {
        status.appendChild(el("span", "dotsep", " · "));
        status.appendChild(el("span", "tail", s.tail));
      }
    }
    var sub = byId("substatus");
    if (sub) {
      setText(sub, s.sub || "");
      sub.hidden = !s.sub;
    }
  }

  var SHACKLE = { lent: "M8 10.5V7a4 4 0 0 1 7.75-1.4", locked: "M8 10.5V7.5a4 4 0 0 1 8 0v3" };

  function renderLock(lock) {
    var plate = byId("lock");
    if (!plate || !lock) return;
    if (plate.getAttribute("data-lock") !== lock.state) {
      plate.setAttribute("data-lock", lock.state);
      var shackle = plate.querySelector(".shackle");
      if (shackle) shackle.setAttribute("d", SHACKLE[lock.state] || SHACKLE.locked);
    }
    setText(byId("lock-title"), lock.title);
    setText(byId("lock-evidence"), lock.evidence || "");
    var chip = byId("kchip");
    if (chip) {
      var lent = lock.state === "lent";
      if (chip.classList.contains("locked") === lent) {
        chip.classList.toggle("locked", !lent);
        var use = chip.querySelector("use");
        if (use) use.setAttribute("href", lent ? "#i-unlock" : "#i-lock");
      }
      setText(byId("kchip-text"), lent ? "Keyboard: agent · hold live" : "Keyboard locked");
    }
  }

  function renderProgress(p) {
    if (!p || !p.steps) return;
    var root = byId("progress");
    if (!root) return;
    root.setAttribute("data-phase", p.phase);
    var story = byId("story");
    if (story) {
      p.steps.forEach(function (s, i) {
        var li = story.children[i];
        var sig = stepSig(s);
        if (li && li.getAttribute("data-sig") === sig) return;
        if (li && baseOf(li.getAttribute("data-sig"), 2) === baseOf(sig, 2)) {
          li.setAttribute("data-sig", sig);
          setText(li.querySelector(".st-note"), s.note);
          return;
        }
        var fresh = buildStep(s);
        if (li) story.replaceChild(fresh, li); else story.appendChild(fresh);
      });
      var o = p.outcome;
      var cap = story.children[p.steps.length];
      var osig = outcomeSig(o);
      if (cap && cap.getAttribute("data-sig") !== osig && baseOf(cap.getAttribute("data-sig"), 2) === baseOf(osig, 2)) {
        cap.setAttribute("data-sig", osig);
        setText(cap.querySelector(".st-note"), o.note);
      } else if (!cap || cap.getAttribute("data-sig") !== osig) {
        var built = buildOutcome(o);
        if (cap) story.replaceChild(built, cap); else story.appendChild(built);
      }
    }
    renderPhoneStrip(p);
    renderLock(p.lock);
    renderStatus(p);
  }

  /* ---- money box ---- */
  function renderMoney(m) {
    var box = byId("money");
    if (!box || !m) return;
    var cls = "money " + m.tone;
    if (box.className !== cls) box.className = cls;
    var use = box.querySelector(".m-ill use");
    if (use && use.getAttribute("href") !== "#" + m.icon) use.setAttribute("href", "#" + m.icon);
    setText(byId("money-label"), m.label);
    setText(byId("money-amount"), "$" + m.amount);
    setText(byId("money-unit"), m.unit || "");
    var sentence = byId("money-sentence");
    if (sentence) { setText(sentence, m.sentence || ""); sentence.hidden = !m.sentence; }
    var stats = byId("money-stats");
    if (stats) {
      var sig = JSON.stringify(m.stats || []);
      if (stats.getAttribute("data-sig") !== sig) {
        stats.setAttribute("data-sig", sig);
        stats.textContent = "";
        (m.stats || []).forEach(function (st) {
          var p = el("p", "stat");
          p.appendChild(el("b", "", st.value));
          p.appendChild(el("span", "", st.label));
          stats.appendChild(p);
        });
      }
      stats.hidden = !(m.stats && m.stats.length);
    }
    var warning = byId("money-warning");
    if (warning) { setText(warning, m.warning || ""); warning.hidden = !m.warning; }
  }

  // Copy buttons: <button data-copy="element-id">. The text is read from the element, never built.
  // The label is kept in data-label, so a second click within the 1.5 s cannot make "Copied" stick,
  // and the result is announced in a polite live region.
  var copyLive = null;
  function announce(text) {
    if (!copyLive) {
      copyLive = el("p", "sr-only");
      copyLive.setAttribute("role", "status");
      copyLive.setAttribute("aria-live", "polite");
      document.body.appendChild(copyLive);
    }
    copyLive.textContent = "";
    window.setTimeout(function () { copyLive.textContent = text; }, 50);
  }
  // Without a clipboard (an older browser, a page not served over https), the text is selected so
  // it can be copied by hand, and the page says so.
  function selectText(node) {
    try {
      var range = document.createRange();
      range.selectNodeContents(node);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    } catch (e) { /* nothing to select with */ }
  }
  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-copy]");
    if (!button) return;
    var source = byId(button.getAttribute("data-copy"));
    if (!source) return;
    if (!navigator.clipboard || !navigator.clipboard.writeText) {
      selectText(source);
      announce("Copy is not available here. The text is selected: copy it by hand.");
      return;
    }
    var label = button.querySelector(".copy-label") || button.lastChild;
    if (label && !button.hasAttribute("data-label")) button.setAttribute("data-label", label.textContent);
    navigator.clipboard.writeText(source.textContent).then(function () {
      if (label) label.textContent = "Copied";
      announce("Copied to the clipboard.");
      window.clearTimeout(Number(button.getAttribute("data-timer") || 0));
      button.setAttribute("data-timer", String(window.setTimeout(function () {
        if (label) label.textContent = button.getAttribute("data-label") || "";
      }, 1500)));
    }, function () {
      selectText(source);
      announce("Copy did not work. The text is selected: copy it by hand.");
    });
  });

  /* Clock times (<time class="clock" data-epoch>) in the visitor's own time; UTC stays in the
   * title. Without script they read "14:43 UTC". */
  function localClocks(root) {
    (root || document).querySelectorAll("time.clock[data-epoch]").forEach(function (node) {
      var epoch = Number(node.getAttribute("data-epoch"));
      if (!epoch) return;
      try {
        // 24-hour, like the UTC times on the page, and said to be the visitor's own.
        setText(node, new Date(epoch * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }) +
          " your time");
        node.setAttribute("datetime", new Date(epoch * 1000).toISOString());
      } catch (e) { /* keep the UTC text */ }
    });
  }
  localClocks();

  /* A page that reloads itself (a stage change, Stop) sets this flag first; the new page then puts
   * focus on its status sentence, so a screen reader hears what changed. */
  var FOCUS_KEY = "keyhold-focus-status";
  function reloadTo(url) {
    try { window.sessionStorage.setItem(FOCUS_KEY, "1"); } catch (e) { /* focus stays at the top */ }
    if (url) window.location.assign(url); else window.location.reload();
  }
  try {
    if (window.sessionStorage.getItem(FOCUS_KEY) === "1") {
      window.sessionStorage.removeItem(FOCUS_KEY);
      var status = byId("status");
      if (status) status.focus();
    }
  } catch (e) { /* storage blocked */ }

  /* "(in about 12 min)": counted down from the server's number, no clock of our own. */
  function countdowns() {
    var nodes = Array.prototype.slice.call(document.querySelectorAll(".free-in[data-seconds]"));
    if (!nodes.length) return;
    var start = Date.now();
    var tick = function () {
      nodes.forEach(function (node) {
        var left = Math.max(0, Math.round(Number(node.getAttribute("data-seconds")) - (Date.now() - start) / 1000));
        var minutes = Math.ceil(left / 60);
        setText(node, left < 60 ? "in under a minute"
          : (node.getAttribute("data-within") === "1" ? "within " : "in about ") + minutes + " min");
      });
    };
    window.setInterval(tick, 15000);
  }
  countdowns();

  return {
    api: api, el: el, icon: icon, clock: clock, csrf: csrf, byId: byId, setText: setText, announce: announce,
    renderProgress: renderProgress, renderMoney: renderMoney, renderLock: renderLock, renderStatus: renderStatus,
    reloadTo: reloadTo, localClocks: localClocks
  };
})();
