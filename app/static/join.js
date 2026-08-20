/* Join page: copy the code, and normalise whatever the visitor pastes into
   the "have a code?" box before sending them to /j/<code>. */

(function () {
  "use strict";

  var copyButton = document.querySelector("[data-copy]");
  var codeText = document.querySelector("[data-code-text]");

  if (copyButton && codeText) {
    copyButton.addEventListener("click", function () {
      var value = codeText.textContent.trim();
      var done = function () {
        copyButton.textContent = "Copied";
        window.setTimeout(function () {
          copyButton.textContent = "Copy";
        }, 1600);
      };

      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(value).then(done, fallback);
      } else {
        fallback();
      }

      function fallback() {
        var field = document.createElement("textarea");
        field.value = value;
        field.setAttribute("readonly", "readonly");
        field.className = "offscreen";
        document.body.appendChild(field);
        field.select();
        try {
          document.execCommand("copy");
          done();
        } catch (err) {
          copyButton.textContent = "Copy failed";
        }
        document.body.removeChild(field);
      }
    });
  }

  var form = document.querySelector("[data-join-form]");
  if (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var input = form.querySelector('input[name="code"]');
      var raw = (input && input.value ? input.value : "").trim();
      if (!raw) return;

      // Accept a bare code, a spaced code, or a full invite URL.
      var code = raw;
      if (code.indexOf("/") !== -1) {
        code = code.replace(/[?#].*$/, "").replace(/\/+$/, "").split("/").pop();
      }
      code = code.toLowerCase().replace(/[^a-z0-9-]/g, "");
      if (code) window.location.href = "/j/" + encodeURIComponent(code);
    });
  }
})();
