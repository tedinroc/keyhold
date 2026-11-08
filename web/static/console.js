/* The console figure: the screenshot cropped to its text, a stepper through earlier screens (shown
 * once there are two), and the full screen in a <dialog> (the link keeps working without script).
 * New screens arrive from SSE through Keyhold.console.add(thumb); the view follows them only while
 * it shows the latest one. A step with the arrows is announced; a new screen from the stream is not. */
(function () {
  "use strict";
  var K = window.Keyhold;
  if (!K) return;
  var list = [];
  var index = -1;

  function cropVars(text) {
    var out = {};
    String(text || "").split(";").forEach(function (part) {
      var kv = part.split(":");
      if (kv.length === 2 && kv[0].trim().indexOf("--") === 0) out[kv[0].trim()] = kv[1].trim();
    });
    return out;
  }

  function fromCrop(c) {
    if (!c) return {};
    return { "--iw": c.iw, "--cx": c.x, "--cy": c.y, "--cw": c.w, "--ch": c.h, "--pcx": c.px, "--pcw": c.pw,
      "--pcy": c.py, "--pch": c.ph };
  }

  // Thumbs rendered by the server
  var holder = K.byId("thumbs");
  if (holder) {
    Array.prototype.forEach.call(holder.children, function (li) {
      list.push({
        n: Number(li.getAttribute("data-n")),
        url: li.getAttribute("data-url"),
        at: li.getAttribute("data-at"),
        label: li.getAttribute("data-label"),
        width: Number(li.getAttribute("data-width")),
        height: Number(li.getAttribute("data-height")),
        vars: cropVars(li.getAttribute("data-crop")),
        cut: li.getAttribute("data-cut") === "1",
        dcut: li.getAttribute("data-dcut") === "1"
      });
    });
    index = list.length - 1;
  }

  function altFor(t) { return "Console screen at " + K.clock(t.at) + " UTC, " + t.label; }

  function ensureShot() {
    var shot = K.byId("shot");
    if (shot) return shot;
    var empty = K.byId("shot-empty");
    var figure = K.byId("console");
    if (!figure) return null;
    shot = K.el("a", "shot");
    shot.id = "shot";
    shot.setAttribute("data-zoom", "");
    var img = K.el("img");
    img.id = "shot-img";
    shot.appendChild(img);
    if (empty) figure.replaceChild(shot, empty);
    else figure.insertBefore(shot, figure.children[1] || null);
    return shot;
  }

  function show(i) {
    var t = list[i];
    if (!t) return;
    index = i;
    var shot = ensureShot();
    if (!shot) return;
    var img = shot.querySelector("img");
    ["--iw", "--cx", "--cy", "--cw", "--ch", "--pcx", "--pcw", "--pcy", "--pch"].forEach(function (name) {
      if (t.vars[name] !== undefined) shot.style.setProperty(name, t.vars[name]);
      else shot.style.removeProperty(name);
    });
    shot.classList.toggle("crop", t.vars["--cw"] !== undefined);
    if (t.cut) shot.setAttribute("data-cut", "1"); else shot.removeAttribute("data-cut");
    if (t.dcut) shot.setAttribute("data-dcut", "1"); else shot.removeAttribute("data-dcut");
    var full = K.byId("step-full");
    if (full) full.hidden = false;
    shot.href = t.url;
    shot.setAttribute("aria-label", "Open the full screen: " + altFor(t));
    img.src = t.url;
    img.alt = altFor(t);
    img.width = t.width;
    img.height = t.height;
    K.setText(K.byId("shot-time"), K.clock(t.at) + " UTC");
    updateNav();
  }

  /* The arrows say aria-disabled instead of disabled, so a focused arrow keeps focus at either
   * end; they and "n / N" show only once there are two screens to step through. */
  function updateNav() {
    var stepper = K.byId("stepper");
    if (stepper) stepper.hidden = list.length < 2;
    var pos = K.byId("step-pos");
    if (pos) K.setText(pos, (index + 1) + " / " + list.length);
    var prev = K.byId("step-prev");
    var next = K.byId("step-next");
    if (prev) prev.setAttribute("aria-disabled", index <= 0 ? "true" : "false");
    if (next) next.setAttribute("aria-disabled", index >= list.length - 1 ? "true" : "false");
  }

  function add(thumb) {
    if (list.some(function (t) { return t.n === thumb.n; })) return;
    var following = index === list.length - 1;
    list.push({ n: thumb.n, url: thumb.url, at: thumb.at, label: thumb.label, width: thumb.width,
      height: thumb.height, vars: fromCrop(thumb.crop), cut: Boolean(thumb.crop && thumb.crop.cut),
      dcut: Boolean(thumb.crop && thumb.crop.dcut) });
    if (following || index < 0) show(list.length - 1);
    else updateNav();
  }

  var prevButton = K.byId("step-prev");
  var nextButton = K.byId("step-next");
  function stepTo(i) {
    show(i);
    var t = list[i];
    K.announce("Screen " + (i + 1) + " of " + list.length + ", " + K.clock(t.at) + " UTC, " + t.label + ".");
  }
  if (prevButton) prevButton.addEventListener("click", function () { if (index > 0) stepTo(index - 1); });
  if (nextButton) nextButton.addEventListener("click", function () { if (index < list.length - 1) stepTo(index + 1); });

  // Full screen in a dialog; without <dialog> support the link opens the image.
  var dialog = K.byId("zoom");
  function openZoom(event) {
    var t = list[index];
    if (!dialog || typeof dialog.showModal !== "function" || !t) return;
    event.preventDefault();
    var img = K.byId("zoom-img");
    img.src = t.url;
    img.alt = altFor(t);
    K.setText(K.byId("zoom-title"), "Console screen at " + K.clock(t.at) + " UTC · " + t.label);
    dialog.showModal();
  }
  document.addEventListener("click", function (event) {
    var target = event.target.closest && event.target.closest("[data-zoom], [data-zoom-open]");
    if (target) openZoom(event);
  });
  var close = K.byId("zoom-close");
  if (close && dialog) {
    close.addEventListener("click", function () { dialog.close(); });
    dialog.addEventListener("click", function (event) { if (event.target === dialog) dialog.close(); });
  }

  K.console = { add: add, show: show };
})();
