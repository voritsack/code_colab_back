/* Live view of one session for an administrator.

   Polls the same way the dashboard does. Read-only on purpose: this page
   never opens a socket into the session, so watching cannot leak a
   keystroke into somebody else's editor or appear in their roster. */

(function () {
  "use strict";

  var POLL_MS = 3000;
  var root = document.querySelector("[data-watch]");
  if (!root) return;

  var url = root.getAttribute("data-url");
  var selected = root.getAttribute("data-selected") || "";

  var filesRoot = root.querySelector("[data-files]");
  var fileCount = root.querySelector("[data-file-count]");
  var codeRoot = root.querySelector("[data-code]");
  var codeTitle = root.querySelector("[data-code-title]");
  var codeMeta = root.querySelector("[data-code-meta]");
  var peopleRoot = root.querySelector("[data-people]");
  var chatRoot = root.querySelector("[data-chat]");
  var attachRoot = root.querySelector("[data-attachments]");
  var statusPill = root.querySelector("[data-status]");
  var clock = document.querySelector("[data-live-clock]");

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function bytes(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(1) + " MB";
  }

  function clockTime(iso) {
    if (!iso) return "—";
    var d = new Date(iso.indexOf("Z") === -1 ? iso + "Z" : iso);
    return isNaN(d.getTime()) ? "—" : d.toLocaleTimeString();
  }

  function select(path) {
    selected = path;
    root.setAttribute("data-selected", path);
    // Keep the address bar honest, so a reload or a shared link lands on
    // the same file. replaceState: this is not a navigation.
    var next = window.location.pathname + (path ? "?path=" + encodeURIComponent(path) : "");
    window.history.replaceState({}, "", next);
    tick();
  }

  function renderFiles(files) {
    filesRoot.textContent = "";
    if (fileCount) fileCount.textContent = files.length + " file(s)";
    if (!files.length) {
      filesRoot.appendChild(el("li", "empty", "Nothing shared yet."));
      return;
    }
    files.forEach(function (f) {
      var li = el("li", f.path === selected ? "fileitem is-open" : "fileitem");
      var button = el("button", "filebtn");
      button.type = "button";
      button.appendChild(el("span", "path", f.path));
      var meta = bytes(f.size) + " · " + clockTime(f.updated_at);
      if (f.locked_by) meta += " · held by " + f.locked_by;
      button.appendChild(el("span", "muted small", meta));
      button.addEventListener("click", function () {
        select(f.path);
      });
      li.appendChild(button);
      filesRoot.appendChild(li);
    });
  }

  function renderCode(file) {
    if (!file) {
      codeTitle.textContent = selected ? selected + " (gone)" : "Pick a file";
      codeMeta.textContent = "";
      codeRoot.textContent = selected
        ? "That file is no longer in the session."
        : "Select a file on the left to read what everyone is looking at.";
      return;
    }
    codeTitle.textContent = file.path;
    codeMeta.textContent =
      bytes(file.size) +
      " · updated " +
      clockTime(file.updated_at) +
      (file.truncated ? " · showing the first part only" : "");

    // Only repaint when the text actually changed, or a scrolled reader
    // gets yanked back to the top every few seconds.
    if (codeRoot.textContent !== file.content) codeRoot.textContent = file.content;
  }

  function renderPeople(people) {
    peopleRoot.textContent = "";
    if (!people.length) {
      peopleRoot.appendChild(el("li", "empty", "Nobody has joined."));
      return;
    }
    people.forEach(function (p) {
      var li = el("li", "personitem");
      var line = el("div");
      line.appendChild(el("span", "dot" + (p.connected ? " dot-on" : "")));
      line.appendChild(document.createTextNode(p.name + " "));
      line.appendChild(el("span", "tag", p.role));
      if (p.state === "pending") line.appendChild(el("span", "tag tag-warn", "waiting"));
      li.appendChild(line);
      li.appendChild(
        el(
          "div",
          "muted small path",
          (p.active_file || "no file open") + " · " + p.edits + " edits"
        )
      );
      peopleRoot.appendChild(li);
    });
  }

  function renderChat(messages) {
    chatRoot.textContent = "";
    if (!messages.length) {
      chatRoot.appendChild(el("li", "empty", "Nothing said yet."));
      return;
    }
    messages.slice(-40).forEach(function (m) {
      var li = el("li");
      li.appendChild(el("span", "feed-kind", m.display_name));
      li.appendChild(el("span", "feed-msg", m.text));
      li.appendChild(el("span", "feed-actor", clockTime(m.at)));
      chatRoot.appendChild(li);
    });
  }

  function renderAttachments(rows) {
    attachRoot.textContent = "";
    if (!rows.length) {
      attachRoot.appendChild(el("li", "empty", "None."));
      return;
    }
    rows.forEach(function (a) {
      var li = el("li", "fileitem");
      li.appendChild(el("span", "path", a.name));
      li.appendChild(
        el("span", "muted small", bytes(a.size) + " · from " + (a.uploaded_by || "unknown"))
      );
      attachRoot.appendChild(li);
    });
  }

  function stamp() {
    if (clock) clock.textContent = "updated " + new Date().toLocaleTimeString();
  }

  function tick() {
    var target = url + (selected ? "?path=" + encodeURIComponent(selected) : "");
    fetch(target, { credentials: "same-origin" })
      .then(function (res) {
        if (res.status === 401 || res.status === 303) {
          window.location.href = "/admin/login";
          return null;
        }
        if (res.status === 404) {
          window.location.href = "/admin";
          return null;
        }
        if (!res.ok) throw new Error("watch " + res.status);
        return res.json();
      })
      .then(function (data) {
        if (!data) return;
        if (statusPill) {
          statusPill.textContent = data.status;
          statusPill.className = "pill pill-" + data.status;
        }
        renderFiles(data.files);
        renderCode(data.selected);
        renderPeople(data.participants);
        renderChat(data.chat);
        renderAttachments(data.attachments);
        stamp();
      })
      .catch(function () {
        if (clock) clock.textContent = "reconnecting…";
      });
  }

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
