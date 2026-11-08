/* Run page: follow the boot and the free look over SSE, keep the progress strip current, and move
 * on when the stage changes; if the stream closes, check /api/runs/<id> every 5 seconds instead.
 * Also the wait timer, the checkout countdown and the "Go to approve" bar. */
(function () {
  "use strict";
  var body = document.body;
  if (body.getAttribute("data-page") !== "run") return;
  var K = window.Keyhold;
  var byId = K.byId;
  var runId = body.getAttribute("data-run-id");
  var stage = body.getAttribute("data-stage");
  var waiting = ["booting", "looking", "quoted", "checkout"];
  var paying = ["quoted", "checkout"];

  function mmss(total) {
    total = Math.max(0, Math.floor(total));
    var m = Math.floor(total / 60);
    var s = total % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  /* ---- the wait timer (in the card only): the boot counts from the start, the free look from
   * its own start; a stage change reloads ---- */
  var elapsed = byId("elapsed");
  if (elapsed && ["booting", "looking"].indexOf(stage) >= 0) {
    var started = Date.now() - Number(elapsed.getAttribute("data-since") || 0) * 1000;
    window.setInterval(function () { K.setText(elapsed, mmss((Date.now() - started) / 1000)); }, 1000);
  }

  /* ---- checkout countdown, announced only at 2 minutes and 30 seconds ---- */
  var deadline = byId("deadline");
  if (deadline && deadline.hasAttribute("data-seconds")) {
    var ends = Date.now() + Number(deadline.getAttribute("data-seconds")) * 1000;
    var announced = {};
    var tick = function () {
      var left = (ends - Date.now()) / 1000;
      if (left <= 0) {
        K.setText(deadline, "Time is up: the server is released and nothing is held.");
        return;
      }
      K.setText(deadline, mmss(left) + " left to approve, or the server is released and nothing is held.");
      [120, 30].forEach(function (mark) {
        if (left <= mark && !announced[mark]) {
          announced[mark] = true;
          K.setText(byId("deadline-alert"), (mark === 120 ? "2 minutes" : "30 seconds") + " left to approve the hold.");
        }
      });
    };
    tick();
    window.setInterval(tick, 1000);
  }

  /* ---- "Go to approve" bar: shown only while the approve button is off screen (any width). It
   * never approves; it brings the button to the middle of the screen and moves focus there: to the
   * simulated approve button offline, to the checkout heading with PayPal's button (the bar hides
   * itself, so focus must not stay on it). ---- */
  var sticky = byId("sticky-cta");
  var target = byId("paypal-area") || byId("checkout");
  if (sticky && target && "IntersectionObserver" in window) {
    new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        var show = !entry.isIntersecting;
        sticky.setAttribute("data-hidden", show ? "0" : "1");
        document.documentElement.classList.toggle("has-bottom-bar", show);
      });
    }, { threshold: 0.5 }).observe(target);
    sticky.addEventListener("click", function (event) {
      event.preventDefault();
      var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      target.scrollIntoView({ block: "center", behavior: still ? "auto" : "smooth" });
      var approve = byId("offline-approve");
      var heading = byId("checkout-title");
      if (approve && approve.getAttribute("aria-disabled") !== "true") approve.focus({ preventScroll: true });
      else if (heading) heading.focus({ preventScroll: true });
    });
  }

  if (waiting.indexOf(stage) < 0) return;

  /* A new stage: the rescue page once the hold is live, else this page again (the quote, the
   * no-hold page). The new page puts focus on its status sentence (common.js). */
  function moveOn(newStage, rescueId) {
    if ((newStage === "rescuing" || newStage === "warranty") && rescueId) {
      K.reloadTo("/rescues/" + encodeURIComponent(rescueId));
    } else {
      K.reloadTo();
    }
  }

  function changed(newStage) {
    if (newStage === stage) return false;
    if (paying.indexOf(stage) >= 0 && paying.indexOf(newStage) >= 0) { stage = newStage; return false; }
    return true;
  }

  /* ---- without the stream: "Updates paused", a Reload button, and a check every 5 seconds ---- */
  var pollTimer = null;
  function paused() {
    var flag = byId("live-flag");
    if (flag) { flag.setAttribute("data-conn", "lost"); K.setText(byId("live-text"), "Updates paused"); }
    var note = byId("live-note");
    if (note && note.hidden) {
      note.textContent = "";
      note.appendChild(document.createTextNode("The live connection closed. The page checks again every 5 seconds. "));
      var button = K.el("button", "btn btn-link", "Reload");
      button.type = "button";
      button.addEventListener("click", function () { window.location.reload(); });
      note.appendChild(button);
      note.hidden = false;
    }
    if (pollTimer) return;
    pollTimer = window.setInterval(check, 5000);
  }

  /* One look at the run without the stream. Used by the paused poller, by a slow watchdog that runs
   * even while the stream looks open (a stalled connection in a background tab or behind a proxy
   * never reports an error), and whenever the page becomes visible again. */
  var moving = false;
  async function check() {
    if (moving) return;
    try {
      var run = await K.api("GET", "/api/runs/" + encodeURIComponent(runId));
      if (run.progress) K.renderProgress(run.progress);
      if (changed(run.stage)) {
        moving = true;
        if (pollTimer) window.clearInterval(pollTimer);
        window.clearInterval(watchdog);
        if (typeof source !== "undefined" && source) source.close();
        moveOn(run.stage, run.rescueId);
      }
    } catch (e) { /* the next tick tries again */ }
  }
  var watchdog = window.setInterval(check, 8000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) check(); });

  if (!window.EventSource) { paused(); return; }
  var source = new EventSource("/runs/" + encodeURIComponent(runId) + "/events");

  function showNotes(notes) {
    var list = byId("notes");
    if (!list || !notes) return;
    if (list.children.length === notes.length) return;
    list.textContent = "";
    notes.forEach(function (n) {
      var item = K.el("li");
      item.appendChild(K.el("time", "", K.clock(n.at)));
      item.appendChild(K.el("span", "", n.text));
      list.appendChild(item);
    });
  }

  source.addEventListener("run", function (event) {
    var run = JSON.parse(event.data);
    showNotes(run.notes);
    if (run.progress) K.renderProgress(run.progress);
    if (!changed(run.stage) || moving) return;
    moving = true;
    window.clearInterval(watchdog);
    source.close();
    moveOn(run.stage, run.rescue_id);
  });

  source.addEventListener("thumb", function (event) {
    if (K.console) K.console.add(JSON.parse(event.data));
  });

  source.addEventListener("end", function () { source.close(); });
  source.addEventListener("error", function () {
    if (source.readyState === EventSource.CLOSED) paused();
  });
})();
