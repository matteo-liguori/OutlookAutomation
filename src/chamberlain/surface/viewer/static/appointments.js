"use strict";
// The important-appointment dialog and its hold-to-acknowledge button, ported from the old
// dashboard. A reminder that waits for the owner opens by itself and cannot be closed until it is
// acknowledged: hold the button for five seconds with the pointer, Space or Enter. Letting go,
// leaving the button, losing focus or hiding the tab cancels the hold. The server finishes the
// reminder and records the owner as the one who acknowledged; nothing here is reachable by the
// assistant's tools.
(function () {
  var V = window.ChamberlainViewer;
  if (!V) return;
  var el = V.el;
  var HOLD_MS = 5000;

  var dialog = null;
  var parts = {};
  var current = null;
  var hold = null;
  var frame = 0;

  function ensure() {
    if (dialog) return;
    dialog = el("dialog", { "aria-labelledby": "appointment-title" });
    parts.kicker = el("p", { class: "appointment-kicker", text: "IMPORTANT APPOINTMENT" });
    parts.title = el("h2", { id: "appointment-title" });
    parts.details = el("div", { class: "appointment-details" });
    parts.button = el("button", { type: "button", class: "ack-button", text: "Hold to acknowledge", "aria-describedby": "hold-instructions" });
    parts.progress = el("progress", { value: "0", max: "5", "aria-label": "Acknowledgement hold progress" });
    parts.help = el("p", { id: "hold-instructions", class: "muted", text: "Hold for five seconds using your pointer, Space or Enter." });
    parts.ack = el("div", { class: "ack-box" }, parts.button, parts.progress, parts.help);
    parts.close = el("button", { type: "button", class: "act", text: "Close", onclick: function () { dialog.close(); } });
    parts.status = el("p", { class: "muted", role: "status" });
    dialog.appendChild(parts.kicker);
    dialog.appendChild(parts.title);
    dialog.appendChild(parts.details);
    dialog.appendChild(parts.ack);
    dialog.appendChild(parts.close);
    dialog.appendChild(parts.status);
    document.body.appendChild(dialog);

    dialog.addEventListener("cancel", function (event) {
      // Escape closes a preview, never a reminder that waits for the owner.
      stop();
      if (current && current.due) event.preventDefault();
    });
    parts.button.addEventListener("pointerdown", function (event) {
      if (!event.isPrimary || event.button !== 0 || hold) return;
      event.preventDefault();
      parts.button.focus();
      try { parts.button.setPointerCapture(event.pointerId); } catch (_) { /* not all pointers can be captured */ }
      start({ pointerId: event.pointerId });
    });
    parts.button.addEventListener("pointermove", function (event) {
      if (!hold || hold.pointerId !== event.pointerId) return;
      var r = parts.button.getBoundingClientRect();
      if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) stop();
    });
    ["pointerup", "pointercancel", "lostpointercapture"].forEach(function (type) {
      parts.button.addEventListener(type, function (event) { if (hold && hold.pointerId === event.pointerId) stop(); });
    });
    parts.button.addEventListener("keydown", function (event) {
      if (event.key !== " " && event.key !== "Enter") return;
      event.preventDefault();
      if (!event.repeat) start({ key: event.key });
    });
    parts.button.addEventListener("keyup", function (event) { if (hold && hold.key === event.key) stop(); });
    parts.button.addEventListener("blur", stop);
    window.addEventListener("blur", stop);
    document.addEventListener("visibilitychange", function () { if (document.hidden) stop(); });
  }

  function stop() {
    cancelAnimationFrame(frame);
    hold = null;
    if (!parts.button || !parts.button.disabled) parts.progress.value = 0;
  }

  function start(input) {
    if (hold || parts.button.disabled || !current || !current.due) return;
    hold = { pointerId: input.pointerId, key: input.key, started: performance.now() };
    parts.status.textContent = "";
    function tick(now) {
      if (!hold) return;
      var elapsed = now - hold.started;
      parts.progress.value = Math.min(elapsed, HOLD_MS) / 1000;
      if (elapsed < HOLD_MS) {
        frame = requestAnimationFrame(tick);
      } else {
        hold = null;
        acknowledge();
      }
    }
    frame = requestAnimationFrame(tick);
  }

  function acknowledge() {
    var item = current;
    parts.button.disabled = true;
    parts.status.textContent = "Saving the acknowledgement…";
    V.api("POST", "/api/appointments/" + encodeURIComponent(item.action) + "/acknowledge", {}).then(function () {
      parts.button.disabled = false;
      parts.progress.value = 0;
      current = null;
      dialog.close();
      V.render();
    }, function (error) {
      parts.button.disabled = false;
      parts.progress.value = 0;
      parts.status.textContent = error.message;
    });
  }

  function show(item) {
    ensure();
    stop();
    current = item;
    parts.title.textContent = item.title;
    parts.details.replaceChildren(
      el("p", { text: "Starts " + (V.local ? V.local(item.start) : item.start) + "." }),
      el("p", { class: "muted", text: "Card: " + item.card_title })
    );
    parts.ack.hidden = !item.due;
    parts.close.hidden = !!item.due;
    parts.kicker.textContent = item.due ? "IMPORTANT APPOINTMENT — ACKNOWLEDGEMENT REQUIRED" : "IMPORTANT APPOINTMENT";
    dialog.className = item.due ? "acknowledgement-due" : "";
    parts.status.textContent = "";
    if (!dialog.open) dialog.showModal();
  }

  // Open the first reminder that waits for the owner, unless a dialog is already open.
  function check(list) {
    if (!list || !list.length) return;
    ensure();
    if (dialog.open) return;
    show(list[0]);
  }

  V.appointments = { show: show, check: check };
})();
