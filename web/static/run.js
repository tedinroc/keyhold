/* Run page: follow the boot and the free look over SSE, and move on when the stage changes. */
(function () {
  "use strict";
  var body = document.body;
  if (body.getAttribute("data-page") !== "run" || !window.EventSource) return;
  var K = window.Keyhold;
  var runId = body.getAttribute("data-run-id");
  var stage = body.getAttribute("data-stage");
  var waiting = ["booting", "looking", "quoted", "checkout"];
  var paying = ["quoted", "checkout"];
  if (waiting.indexOf(stage) < 0) return;

  var source = new EventSource("/runs/" + encodeURIComponent(runId) + "/events");

  function showNotes(notes) {
    var list = document.getElementById("notes");
    if (!list || !notes) return;
    list.textContent = "";
    notes.forEach(function (note) {
      var item = K.el("li");
      item.appendChild(K.el("time", "", K.clock(note.at)));
      item.appendChild(document.createTextNode(" " + note.text));
      list.appendChild(item);
    });
  }

  source.addEventListener("run", function (event) {
    var run = JSON.parse(event.data);
    var detail = document.getElementById("run-detail");
    if (detail && run.detail) detail.textContent = run.detail;
    showNotes(run.notes);
    if (run.stage === stage) return;
    if (paying.indexOf(stage) >= 0 && paying.indexOf(run.stage) >= 0) { stage = run.stage; return; }
    source.close();
    if ((run.stage === "rescuing" || run.stage === "warranty") && run.rescue_id) {
      window.location.assign("/rescues/" + encodeURIComponent(run.rescue_id));
    } else {
      window.location.reload();
    }
  });

  source.addEventListener("thumb", function (event) {
    var thumb = JSON.parse(event.data);
    var figure = document.getElementById("screen");
    if (!figure) return;
    var img = K.el("img");
    img.src = thumb.url;
    img.alt = "Console screen, " + thumb.label;
    img.width = thumb.width;
    img.height = thumb.height;
    figure.textContent = "";
    figure.appendChild(img);
  });

  source.addEventListener("end", function () { source.close(); });
})();
