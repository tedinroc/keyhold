/* Small helpers shared by the pages. Untrusted text is only ever set with textContent. */
window.Keyhold = (function () {
  "use strict";
  var csrf = document.body.getAttribute("data-csrf") || "";

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

  function clock(iso) { return iso ? String(iso).slice(11, 19) : ""; }

  // Copy buttons: <button data-copy="element-id">. The text is read from the element, never built.
  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-copy]");
    if (!button) return;
    var source = document.getElementById(button.getAttribute("data-copy"));
    if (!source || !navigator.clipboard) return;
    navigator.clipboard.writeText(source.textContent).then(function () {
      var label = button.textContent;
      button.textContent = "Copied";
      setTimeout(function () { button.textContent = label; }, 1500);
    }, function () { /* the text stays visible to copy by hand */ });
  });

  return { api: api, el: el, clock: clock, csrf: csrf };
})();
