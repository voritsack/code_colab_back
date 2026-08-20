/* Polls /admin/api/stats and re-renders the live parts of the dashboard.
   Polling rather than a socket: the dashboard is one page with a handful of
   viewers, and a plain fetch survives every proxy without ceremony. */

(function () {
  "use strict";

  var POLL_MS = 4000;
  var kpiRoot = document.querySelector("[data-kpis]");
  var sessionsRoot = document.querySelector("[data-sessions]");
  var feedRoot = document.querySelector("[data-feed]");
  var clock = document.querySelector("[data-live-clock]");
  if (!kpiRoot) return;

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function renderTotals(totals) {
    Object.keys(totals).forEach(function (key) {
      var target = kpiRoot.querySelector('[data-kpi="' + key + '"]');
      if (target && target.textContent !== String(totals[key])) {
        target.textContent = totals[key];
      }
    });
  }

  function participantRow(p) {
    var tr = el("tr");

    var name = el("td");
    name.appendChild(el("span", "dot" + (p.connected ? " dot-on" : "")));
    name.appendChild(document.createTextNode(p.name));
    if (p.state === "pending") name.appendChild(el("span", "tag tag-warn", "waiting"));
    tr.appendChild(name);

    tr.appendChild(el("td", null, p.role));
    tr.appendChild(el("td", "path", p.active_file || "—"));
    tr.appendChild(el("td", "num", p.edits));
    return tr;
  }

  function sessionCard(row) {
    var card = el("article", "card session-card");

    var head = el("header", "session-head");
    var left = el("div");
    var link = el("a", "session-title", row.title);
    link.href = "/admin/sessions/" + row.public_id;
    left.appendChild(link);
    left.appendChild(
      el(
        "p",
        "muted small",
        "host " + row.host + " · code " + row.join_code + " · " + row.files + " files"
      )
    );
    head.appendChild(left);
    head.appendChild(el("span", "pill pill-" + row.status, row.status));
    card.appendChild(head);

    var table = el("table", "table");
    var thead = el("thead");
    var headRow = el("tr");
    ["Participant", "Role", "Working on"].forEach(function (label) {
      headRow.appendChild(el("th", null, label));
    });
    headRow.appendChild(el("th", "num", "Edits"));
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = el("tbody");
    row.participants.forEach(function (p) {
      tbody.appendChild(participantRow(p));
    });
    table.appendChild(tbody);
    card.appendChild(table);
    return card;
  }

  function renderSessions(rows) {
    if (!sessionsRoot) return;
    sessionsRoot.textContent = "";
    if (!rows.length) {
      sessionsRoot.appendChild(el("p", "empty", "No live sessions right now."));
      return;
    }
    rows.forEach(function (row) {
      sessionsRoot.appendChild(sessionCard(row));
    });
  }

  function renderFeed(events) {
    if (!feedRoot) return;
    feedRoot.textContent = "";
    if (!events.length) {
      feedRoot.appendChild(el("li", "empty", "Nothing yet."));
      return;
    }
    events.forEach(function (e) {
      var li = el("li");
      li.appendChild(el("span", "feed-kind", e.kind));
      li.appendChild(el("span", "feed-msg", e.message));
      li.appendChild(el("span", "feed-actor", e.actor));
      feedRoot.appendChild(li);
    });
  }

  function stamp() {
    if (!clock) return;
    var now = new Date();
    clock.textContent =
      "updated " +
      String(now.getHours()).padStart(2, "0") +
      ":" +
      String(now.getMinutes()).padStart(2, "0") +
      ":" +
      String(now.getSeconds()).padStart(2, "0");
  }

  function tick() {
    fetch("/admin/api/stats", { credentials: "same-origin" })
      .then(function (res) {
        if (res.status === 401 || res.status === 303) {
          window.location.href = "/admin/login";
          return null;
        }
        if (!res.ok) throw new Error("stats " + res.status);
        return res.json();
      })
      .then(function (data) {
        if (!data) return;
        renderTotals(data.totals);
        renderSessions(data.sessions);
        renderFeed(data.events);
        stamp();
      })
      .catch(function () {
        if (clock) clock.textContent = "reconnecting…";
      });
  }

  // Stop hammering the server when nobody is looking at the tab.
  var timer = window.setInterval(function () {
    if (!document.hidden) tick();
  }, POLL_MS);

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) tick();
  });

  window.addEventListener("beforeunload", function () {
    window.clearInterval(timer);
  });

  tick();
})();
