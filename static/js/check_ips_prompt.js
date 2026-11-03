/*
 * Shows the case-number dialog after a 428 response, then retries the request.
 * Loaded once and automatically intercepts chatbot requests; no direct call needed.
 */
(function () {
  "use strict";

  if (window.__ipsPromptInstalled) { return; }
  window.__ipsPromptInstalled = true;

  var IPS_LENGTH = 8;
  var TYPED_RE = /^0\d{7}$/;
  // Kept in step with utils/check_ips_utils.normalise_ips. Punctuation is trimmed
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
      text: state.dismissable
        ? "Change which case this analysis and conversation are recorded against."
        : "This log has no case attached. The number is what links the "
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
      // The dialog is built in script, so there is no markup label to point
      // at. Without a name a screen reader announces only "edit text" for the
      // one control standing between the user and their work.
      "aria-label": "IPS case number",
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
        background: "transparent", border: "1px solid #e2e8f0", color: "#64748b",
        borderRadius: "999px", cursor: "pointer", fontSize: "13px",
        padding: "5px 12px", fontWeight: "500"
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

    if (state.dismissable) {
      // Opened by someone whose answer is already recorded, so leaving it
      // alone is a real choice here -- unlike the gate, where closing would
      // put them straight back into the state it exists to prevent.
      var cancelBtn = el("button", {
        type: "button", text: "Keep it as it is",
        style: {
          background: "transparent", border: "none", color: "#64748b",
          cursor: "pointer", fontSize: "13px", padding: "10px 0 0",
          textDecoration: "underline", width: "100%", textAlign: "center"
        }
      });
      cancelBtn.addEventListener("click", function () {
        if (busy) { return; }
        close();
        onDone(null);
      });
      box.appendChild(cancelBtn);
    }

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
        close();
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
      // The candidate list mixes path guesses with remembered answers, so the
      // origin comes from the server's per-candidate map, not from membership
      // in the list. The server checks the claim again when it records it.
      var sources = state.ips_candidate_sources || {};
      post({
        action: "attach",
        case_nbr: canonical,
        source: sources[canonical] || "explicit",
        log_path: state.log_path || ""
      });
    });

    // Both states are the same quiet pill, differing only by fill. Resting is
    // a slate outline that reads as a secondary control; armed is a soft
    // amber whose shape has not changed, so the eye sees "the same thing,
    // now about to act" rather than a new element appearing.
    var SKIP_REST = {
      background: "transparent", borderColor: "#e2e8f0", color: "#64748b"
    };
    var SKIP_ARMED = {
      background: "#fffbeb", borderColor: "#fde68a", color: "#92400e"
    };

    function styleSkip(style) {
      Object.keys(style).forEach(function (k) { skipBtn.style[k] = style[k]; });
    }

    function disarmSkip() {
      skipArmed = false;
      skipBtn.textContent = "This log has no case";
      styleSkip(SKIP_REST);
    }

    skipBtn.style.transition = "background .15s, border-color .15s, color .15s";

    skipBtn.addEventListener("click", function () {
      if (busy) { return; }
      if (!skipArmed) {
        // Skipping is a claim about the log, not a way out of the dialog, so
        // it is made twice on purpose.
        skipArmed = true;
        skipBtn.textContent = "Click again to confirm";
        styleSkip(SKIP_ARMED);
        hint.textContent = "This log will be recorded without a case.";
        hint.style.color = "#b45309";
        return;
      }
      post({ action: "skip", confirmed: true, log_path: state.log_path || "" });
    });

    // The gate has no dismiss: closing it would put the user straight back
    // into the state it exists to prevent. The voluntary variant does.
    function close() {
      document.removeEventListener("keydown", trap, true);
      overlay.remove();
    }

    function trap(e) {
      if (!document.body.contains(overlay)) { return; }
      if (e.key !== "Escape") { return; }
      e.preventDefault();
      e.stopPropagation();
      if (state.dismissable && !busy) {
        close();
        onDone(null);
      }
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

  var NOTICE_MS = 5000;

  /**
   * Say what was recorded when nothing asked.
   *
   * A case read off the folder name is attached without interrupting anybody,
   * which is the point -- but it also left no sign that it had happened, so
   * there was no way to tell "this log is filed under 01006388" from "nobody
   * ever asked". The notice says which, and carries the same dialog for
   * anyone who disagrees with it.
   */
  function notice(state) {
    if (!state) { return; }
    var source = state.case_ref_source || "absent";
    var attached = state.case_nbr || "";
    if (!attached && source !== "skipped") { return; }   // nothing was decided
    if (!document.body) { return; }

    var existing = document.querySelector(".ips-attached-notice");
    if (existing) { existing.remove(); }

    var bar = el("div", {
      "class": "ips-attached-notice",
      role: "status",
      style: {
        // Top right: the bottom right corner is the Send button.
        position: "fixed", right: "18px", top: "76px", zIndex: "99998",
        display: "flex", alignItems: "center", gap: "10px",
        background: "#f8fafc", border: "1px solid #e2e8f0",
        borderLeft: "3px solid " + (attached ? "#93c5fd" : "#cbd5e1"),
        borderRadius: "8px", padding: "9px 14px", color: "#475569",
        font: "13px/1.4 'Segoe UI', system-ui, sans-serif",
        boxShadow: "0 6px 20px rgba(15,23,42,.10)",
        opacity: "0", transition: "opacity .25s"
      }
    });
    bar.appendChild(el("span", {
      text: attached ? "Filed under case " + attached
                     : "Recorded as having no case number"
    }));

    var change = el("a", {
      href: "#", text: "Change",
      style: { color: "#2563eb", textDecoration: "underline", cursor: "pointer" }
    });
    bar.appendChild(change);

    var timer = null;
    function dismiss() {
      if (timer) { clearTimeout(timer); timer = null; }
      bar.style.opacity = "0";
      setTimeout(function () { bar.remove(); }, 250);
    }
    function hold() {
      if (timer) { clearTimeout(timer); timer = null; }
    }

    change.addEventListener("click", function (e) {
      e.preventDefault();
      hold();
      bar.remove();
      var opened = {};
      Object.keys(state).forEach(function (k) { opened[k] = state[k]; });
      opened.dismissable = true;
      opened.suggested_ips = attached || state.suggested_ips || "";
      ask(opened).then(function (body) {
        // Only a cancelled dialog leaves the case as it was. "This log has no
        // case" comes back successful with an empty number, and is as much a
        // change as any other -- the description and the time on screen still
        // belong to the case that has just been detached.
        if (!body || !body.success) { return; }
        // The number changed, so what the page is showing came from the
        // previous one. Whoever owns the input box re-reads it.
        if (typeof window.IpsPrompt.onCaseChanged === "function") {
          window.IpsPrompt.onCaseChanged(body);
        }
        notice({ case_nbr: body.case_nbr || "",
                 case_ref_source: body.case_ref_source || "skipped",
                 log_path: state.log_path || "" });
      });
    });
    // Reading it should not race the timer away.
    bar.addEventListener("mouseenter", hold);

    document.body.appendChild(bar);
    requestAnimationFrame(function () { bar.style.opacity = "1"; });
    timer = setTimeout(dismiss, NOTICE_MS);
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
    notice: notice,
    /** Set by whoever owns the input box; called after the case is changed. */
    onCaseChanged: null,
    /** Open the prompt for a /set_log response that reported needs_ips. */
    handleSetLog: function (data) {
      if (data && data.needs_ips) { return ask(data); }
      // Nothing was asked, so say what is already on file instead.
      notice(data);
      return Promise.resolve(null);
    }
  };
})();
