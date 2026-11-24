"use strict";
// Larger card limits: the owner sees what the assistant asked for and answers with one click.
// It adds a block to the top of "Needs you" (waiting requests) and a note on each card that is,
// or is waiting to be, a complex card. Everything is drawn with textContent only.
(function () {
  var V = window.ChamberlainViewer;
  if (!V || !V.sections) return;
  var el = V.el;

  function answer(card, what, done) {
    V.post("/api/complex/" + encodeURIComponent(card) + "/" + what, {}, done);
  }

  function waiting(request, limits) {
    var box = el("section", { class: "card" });
    box.appendChild(el("h3", { text: "Larger card requested: " + request.title }));
    box.appendChild(el("p", { text: "The assistant says: " + request.reason }));
    box.appendChild(
      el("p", { class: "muted", text: "If you agree, this card may hold a position of " + limits.position_max + " characters and " + limits.card_max + " in all. Asked " + V.when(request.requested_at) + "." })
    );
    box.appendChild(el("button", { class: "act primary", text: "Approve", onclick: function () { answer(request.card, "approve", V.render); } }));
    box.appendChild(el("button", { class: "act", text: "Decline", onclick: function () { answer(request.card, "decline", V.render); } }));
    return box;
  }

  function holding(item) {
    var row = el("section", { class: "card" });
    row.appendChild(el("h3", { text: item.title }));
    row.appendChild(el("p", { class: "muted", text: "Complex card since " + V.when(item.decided_at) + ". Reason: " + item.reason }));
    row.appendChild(el("button", { class: "act danger", text: "Take the larger limit back", onclick: function () { answer(item.card, "revoke", V.render); } }));
    return row;
  }

  // Waiting requests, shown first on "Needs you". Cards that hold a larger limit follow.
  V.sections.needs.push(function (box) {
    var area = el("div", {});
    box.appendChild(area);
    V.api("GET", "/api/complex").then(function (data) {
      if (data.pending.length) {
        area.appendChild(el("h2", { text: "Larger cards waiting for your answer" }));
        data.pending.forEach(function (request) { area.appendChild(waiting(request, data)); });
      }
      if (data.approved.length) {
        area.appendChild(el("h2", { text: "Cards with a larger limit (" + data.approved.length + " of " + data.limit + ")" }));
        data.approved.forEach(function (item) { area.appendChild(holding(item)); });
      }
    }, function (error) { area.appendChild(V.problem(error)); });
  });

  // On the Cards page: which cards are complex and why, with a way to take it back.
  V.sections.card.push(function (node, card) {
    if (!card.complex) return;
    var note = el("div", { class: "action-preview" });
    if (card.complex.state === "approved") {
      note.appendChild(el("div", { class: "action-line", text: "Complex card: " + card.complex.reason }));
      note.appendChild(el("button", { class: "act danger", text: "Take the larger limit back", onclick: function () { answer(card.handle, "revoke", V.render); } }));
    } else {
      note.appendChild(el("div", { class: "action-line", text: "Waiting for your answer on a larger limit: " + card.complex.reason }));
    }
    node.appendChild(note);
  });
})();
