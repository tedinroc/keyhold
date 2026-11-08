/* /try: one submit per press (the button says "Starting…"), and while the test server is busy, a
 * 10-second check of /try/status that turns the Break buttons back on once it is free (and says
 * so in a live region). Buttons held back by the hourly run limit stay off. The marked card ("Start
 * here" or "Next") is the primary once its button is on; a link to one card (/try#fault-…) makes
 * that card's button the primary instead. Countdowns and local times are in common.js. */
(function () {
  "use strict";
  var body = document.body;
  if (body.getAttribute("data-page") !== "try") return;
  var K = window.Keyhold;

  function buttons() { return document.querySelectorAll("form.fault-form button[type=submit]"); }

  buttons().forEach(function (button) {
    if (!button.hasAttribute("data-text")) button.setAttribute("data-text", button.innerHTML);
  });
  document.querySelectorAll("form.fault-form").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      var button = form.querySelector("button[type=submit]");
      if (!button) return;
      if (button.getAttribute("data-sent") === "1") { event.preventDefault(); return; }
      button.setAttribute("data-sent", "1");
      button.setAttribute("aria-disabled", "true");
      button.textContent = "Starting…";
    });
  });

  /* Back from the run page: a page restored from the back/forward cache still says "Starting…"
   * and would refuse every press. Put the buttons back as they were. */
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    buttons().forEach(function (button) {
      if (button.getAttribute("data-sent") !== "1") return;
      button.removeAttribute("data-sent");
      button.removeAttribute("aria-disabled");
      if (button.hasAttribute("data-text")) button.innerHTML = button.getAttribute("data-text");
    });
  });

  /* The card the URL points to (from an end page's "Try the Stop path") is the one to press. */
  function wanted() {
    var hash = window.location.hash || "";
    if (hash.indexOf("#fault-") !== 0) return null;
    var card = document.getElementById(hash.slice(1));
    return card ? card.querySelector("form.fault-form button[type=submit]") : null;
  }

  function makePrimary(target) {
    buttons().forEach(function (b) {
      var primary = b === target;
      b.classList.toggle("btn-primary", primary);
      b.classList.toggle("btn-secondary", !primary);
    });
  }

  if (body.getAttribute("data-busy") !== "1") {
    var asked = wanted();
    if (asked && !asked.disabled) makePrimary(asked);
    return;
  }

  function turnOn(status) {
    var limited = status.runsLeft === 0;
    var on = [];
    buttons().forEach(function (b) {
      if (b.getAttribute("data-block") === "runs" || limited) return;
      b.disabled = false;
      on.push(b);
    });
    if (on.length) {
      var target = wanted();
      if (!target || on.indexOf(target) < 0) {
        target = on.filter(function (b) { return b.getAttribute("data-primary") === "1"; })[0] || null;
      }
      if (target) makePrimary(target);
      document.querySelectorAll(".fault.start.blocked").forEach(function (card) { card.classList.remove("blocked"); });
    }
    if (!limited) {
      document.querySelectorAll(".fault-form .wait-reason").forEach(function (p) { p.hidden = true; });
      document.querySelectorAll(".fault-form .free-reason").forEach(function (p) { p.hidden = false; });
    }
    var box = K.byId("busy-box");
    if (box) box.hidden = true;
    var capacity = K.byId("capacity");
    if (capacity) {
      if (!limited) capacity.classList.remove("busy");
      var text = capacity.querySelector("span:last-child");
      if (text) {
        text.childNodes.forEach(function (node) {
          if (node.nodeType !== 3) return;
          node.nodeValue = node.nodeValue
            .replace("Test server in use", "Test server free")
            .replace(/All (\d+) test servers in use/, status.slotsFree + " of $1 test servers free");
        });
      }
    }
    K.setText(K.byId("try-live"), limited
      ? "The test server is free, but this address has no runs left this hour."
      : "The test server is free. The Break this server buttons are on.");
    body.setAttribute("data-busy", "0");
  }

  var timer = window.setInterval(async function () {
    try {
      var status = await K.api("GET", "/try/status");
      if (status.busy || !status.ready) return;
      window.clearInterval(timer);
      turnOn(status);
    } catch (e) { /* keep the buttons as they are; the next tick tries again */ }
  }, 10000);
})();
