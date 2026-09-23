/*
 * The case-number prompt.
 *
 * A log that arrives through Send To, a local upload, or a path typed into a
 * chatbot carries no case number, so the analysis and the conversation that
 * follows cannot be attributed to the case they were done for. The server
 * refuses to start such a conversation with 428; this turns that refusal into
 * a dialog and replays the request once the number is known.
 *
 * Installed by including the script. There is nothing to call: the three
 * chatbots reach /chat from enough different places that intercepting the
 * refusal is more reliable than finding every send button.
 */
(function () {
  "use strict";

  if (window.__ipsPromptInstalled) { return; }
  window.__ipsPromptInstalled = true;

  var IPS_LENGTH = 8;
  var TYPED_RE = /^0\d{7}$/;
  // Kept in step with utils/ips_utils.normalise_ips. Punctuation is trimmed
  // off the ends only, and six digits is the floor, so a stray "0" cannot pad
  // itself into a valid-looking 00000000.
  var EDGE_PUNCT_RE = /^[.,;:'"\-_#()\[\]]+|[.,;:'"\-_#()\[\]]+$/g;
  var DIGITS_RE = /^\d{6,8}$/;

  var pending = null;   // shared promise, so parallel 428s open one dialog

  function normalise(raw) {
    var text = String(raw == null ? "" : raw).replace(/\s+/g, "");
    text = text.replace(EDGE_PUNCT_RE, "");
    if (!DIGITS_RE.test(text)) { return ""; }
    while (text.length < IPS_LENGTH) { text = "0" + text; }
    return TYPED_RE.test(text) ? text : "";
  }

  function el(tag, props, children) {
    var node = document.createElement(tag);
    Object.keys(props || {}).forEach(function (k) {
      if (k === "style") { Object.assign(node.style, props[k]); }
      else if (k === "text") { node.textContent = props[k]; }
      else { node.setAttribute(k, props[k]); }
    });
    (children || []).forEach(function (c) { node.appendChild(c); });
    return node;
  }

  function buildDialog(state, onDone) {
    var overlay = el("div", {
      "class": "ips-prompt-overlay",
      style: {
        position: "fixed", inset: "0", zIndex: "99999",
        background: "rgba(15,23,42,.55)", display: "flex",
        alignItems: "center", justifyContent: "center"
      }
    });

    var box = el("div", {
      style: {
        background: "#fff", borderRadius: "10px", padding: "24px 28px",
        width: "min(460px, 92vw)", boxShadow: "0 18px 48px rgba(0,0,0,.28)",
        font: "14px/1.5 'Segoe UI', system-ui, sans-serif", color: "#0f172a"
      }
    });

    box.appendChild(el("div", {
      text: "IPS case number",
      style: { fontSize: "17px", fontWeight: "600", marginBottom: "6px" }
    }));
    box.appendChild(el("div", {
      text: "This log has no case attached. The number is what links the "
          + "analysis and this conversation back to the case.",
      style: { color: "#475569", marginBottom: "16px" }
    }));

    if (state.log_path) {
      box.appendChild(el("div", {
        text: state.log_path,
        title: state.log_path,
        style: {
          fontFamily: "Consolas, monospace", fontSize: "12px", color: "#64748b",
          background: "#f1f5f9", borderRadius: "6px", padding: "6px 8px",
          marginBottom: "14px", overflow: "hidden", textOverflow: "ellipsis",
          whiteSpace: "nowrap"
        }
      }));
    }

    var input = el("input", {
      type: "text", inputmode: "numeric", maxlength: "12",
      placeholder: "e.g. 01010628",
      value: state.suggested_ips || "",
      style: {
        width: "100%", boxSizing: "border-box", padding: "9px 11px",
        border: "1px solid #cbd5e1", borderRadius: "6px", fontSize: "15px",
        fontFamily: "Consolas, monospace", letterSpacing: ".06em"
      }
    });
    box.appendChild(input);

    var hint = el("div", {
      style: { minHeight: "18px", fontSize: "12px", marginTop: "6px", color: "#64748b" }
    });
    box.appendChild(hint);

    var candidates = state.ips_candidates || [];
    if (candidates.length) {
      var row = el("div", {
        style: { display: "flex", flexWrap: "wrap", gap: "6px", margin: "4px 0 14px" }
      });
      row.appendChild(el("span", {
        text: "Found in the path:",
        style: { color: "#64748b", fontSize: "12px", alignSelf: "center" }
      }));
      candidates.forEach(function (c) {
        var chip = el("button", {
          type: "button", text: c,
          style: {
            border: "1px solid #bfdbfe", background: "#eff6ff", color: "#1d4ed8",
            borderRadius: "999px", padding: "3px 10px", cursor: "pointer",
            fontFamily: "Consolas, monospace", fontSize: "12px"
          }
        });
        chip.addEventListener("click", function () { input.value = c; validate(); input.focus(); });
        row.appendChild(chip);
      });
      box.appendChild(row);
    } else {
      box.appendChild(el("div", { style: { height: "10px" } }));
    }

    var actions = el("div", {
      style: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: "10px" }
    });
    var skipBtn = el("button", {
      type: "button", text: "This log has no case",
      style: {
        background: "none", border: "none", color: "#64748b",
        textDecoration: "underline", cursor: "pointer", fontSize: "13px", padding: "0"
      }
    });
    var saveBtn = el("button", {
      type: "button", text: "Attach case",
      style: {
        background: "#2563eb", color: "#fff", border: "none", borderRadius: "6px",
        padding: "9px 18px", cursor: "pointer", fontSize: "14px", fontWeight: "600"
      }
    });
    actions.appendChild(skipBtn);
    actions.appendChild(saveBtn);
    box.appendChild(actions);
    overlay.appendChild(box);

    var busy = false;
    var skipArmed = false;

    function setBusy(on) {
      busy = on;
      saveBtn.disabled = on;
      skipBtn.disabled = on;
      saveBtn.style.opacity = on ? ".6" : "1";
    }

    function validate() {
      var raw = input.value;
      var canonical = normalise(raw);
      if (!raw.trim()) {
        hint.textContent = "";
        hint.style.color = "#64748b";
      } else if (!canonical) {
        hint.textContent = "Not a case number — 8 digits starting with 0, e.g. 01010628.";
        hint.style.color = "#b91c1c";
      } else if (canonical !== raw.trim()) {
        hint.textContent = "Will be saved as " + canonical + ".";
        hint.style.color = "#0f766e";
      } else {
        hint.textContent = "";
      }
      return canonical;
    }

    function fail(message) {
      hint.textContent = message;
      hint.style.color = "#b91c1c";
    }

    function post(payload) {
      setBusy(true);
      return fetch("/api/ips/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      }).then(function (r) {
        return r.json().catch(function () { return { success: false, error: "Server error." }; });
      }).then(function (body) {
        if (!body || !body.success) {
          setBusy(false);
          fail((body && body.error) || "Could not save the case number.");
          return;
        }
        document.removeEventListener("keydown", trap, true);
        overlay.remove();
        onDone(body);
      }).catch(function (e) {
        setBusy(false);
        fail("Could not reach the app: " + e);
      });
    }

    input.addEventListener("input", function () {
      validate();
      if (skipArmed) { disarmSkip(); }
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); saveBtn.click(); }
    });

    saveBtn.addEventListener("click", function () {
      if (busy) { return; }
      var canonical = validate();
      if (!canonical) {
        fail("Enter the 8-digit case number, e.g. 01010628.");
        input.focus();
        return;
      }
      var fromPath = (candidates || []).indexOf(canonical) >= 0;
      post({
        action: "attach",
        case_nbr: canonical,
        // Only claim the path supplied it when the user kept what we guessed.
        source: (fromPath && canonical === state.suggested_ips) ? "derived_from_path" : "explicit",
        log_path: state.log_path || ""
      });
    });

    function disarmSkip() {
      skipArmed = false;
      skipBtn.textContent = "This log has no case";
      skipBtn.style.color = "#64748b";
    }

    skipBtn.addEventListener("click", function () {
      if (busy) { return; }
      if (!skipArmed) {
        // Skipping is a claim about the log, not a way out of the dialog, so
        // it is made twice on purpose.
        skipArmed = true;
        skipBtn.textContent = "Confirm: no case number exists — don't ask again";
        skipBtn.style.color = "#b45309";
        hint.textContent = "This log will be recorded without a case.";
        hint.style.color = "#b45309";
        return;
      }
      post({ action: "skip", confirmed: true, log_path: state.log_path || "" });
    });

    // The dialog has no dismiss: closing it would put the user straight back
    // into the state it exists to prevent.
    function trap(e) {
      if (!document.body.contains(overlay)) { return; }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); }
    }
    document.addEventListener("keydown", trap, true);

    document.body.appendChild(overlay);
    input.focus();
    input.select();
    validate();
    return overlay;
  }

  function ask(state) {
    if (pending) { return pending; }
    pending = new Promise(function (resolve) {
      buildDialog(state || {}, function (body) {
        pending = null;
        resolve(body);
      });
    });
    return pending;
  }

  var originalFetch = window.fetch.bind(window);

  window.fetch = function (input, init) {
    // A Request carries its body as a stream that may only be read once, so
    // sending one consumes it and the replay below would throw "Cannot
    // construct a Request with a Request object that has already been used".
    // Take the copy before the first send, while the body is still unread.
    // Nothing in the app passes a Request today; this is so that the first
    // caller who does is not met with a dialog that resolves into a 428.
    var replayable = null;
    if (typeof Request !== "undefined" && input instanceof Request) {
      try { replayable = input.clone(); } catch (e) { replayable = null; }
    }

    return originalFetch(input, init).then(function (response) {
      if (response.status !== 428) { return response; }
      // Only 428s from this feature are ours to handle; anything else is
      // passed through untouched. The rejection handler is attached to the
      // parse alone rather than to the whole chain: as a trailing .catch it
      // also swallowed a failed replay and handed the caller back the
      // original 428, which is indistinguishable from never having asked.
      return response.clone().json().then(function (body) {
        if (!body || !body.ips_required) { return response; }
        return ask(body).then(function () {
          return originalFetch(replayable || input, init);
        });
      }, function () { return response; });
    });
  };

  window.IpsPrompt = {
    normalise: normalise,
    ask: ask,
    /** Open the prompt for a /set_log response that reported needs_ips. */
    handleSetLog: function (data) {
      if (data && data.needs_ips) { return ask(data); }
      return Promise.resolve(null);
    }
  };
})();
