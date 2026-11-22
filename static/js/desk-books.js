/**
 * Jane's books chip + charge confirm. No fourth tile.
 */
(function (global) {
  "use strict";

  function esc(s) {
    return String(s || "").replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function booksChip(books) {
    books = books || {};
    var owed = Number(books.owed_cents || 0);
    if (owed > 0) {
      return (
        '<span class="home-chip owed">' + esc(books.owed_pretty || "") + " owed</span>"
      );
    }
    return '<span class="home-chip ok">Current</span>';
  }

  function linesHtml(books) {
    var lines = (books && books.lines) || [];
    if (!lines.length) return "";
    return (
      "<ul class='home-list home-books-list'>" +
      lines
        .map(function (ln) {
          return (
            "<li><span>" +
            esc(ln.dos || "") +
            (ln.note ? " · " + esc(ln.note) : "") +
            "</span><span class='desk-meta'>" +
            esc(ln.amount_pretty || "") +
            " · " +
            esc(ln.status || "") +
            "</span></li>"
          );
        })
        .join("") +
      "</ul>"
    );
  }

  function formHtml(books, cardOk) {
    books = books || {};
    var today = new Date().toISOString().slice(0, 10);
    var amt = (books.suggest_pretty || "$0.00").replace("$", "").replace(",", "");
    var inv = books.invoice_url
      ? '<p class="home-kv"><a href="' +
        esc(books.invoice_url) +
        '" target="_blank" rel="noopener">Open pay link</a>' +
        ' · <button type="button" class="desk-num" id="hm-inv-copy">Copy</button></p>'
      : "";
    return (
      '<div class="home-sec home-books" id="hm-books">' +
      "<h4>Books</h4>" +
      linesHtml(books) +
      inv +
      '<div class="home-actions" style="margin:0.35rem 0 0">' +
      '<button class="btn" type="button" id="hm-invoice">Send invoice</button>' +
      "</div>" +
      '<div class="home-charge" id="hm-charge-box" hidden>' +
      '<label class="field">Amount <input id="hm-fee-amt" inputmode="decimal" value="' +
      esc(amt) +
      '" /></label>' +
      '<label class="field">Date <input id="hm-fee-dos" type="date" value="' +
      esc(today) +
      '" /></label>' +
      '<label class="field">Note <input id="hm-fee-note" maxlength="80" placeholder="30m visit" /></label>' +
      '<p class="home-empty" id="hm-fee-st" style="margin:0.35rem 0 0"></p>' +
      '<div class="home-actions" style="margin-top:0.4rem">' +
      (cardOk
        ? '<button class="btn" type="button" id="hm-fee-go">Charge card on file</button>'
        : "") +
      '<button class="btn secondary" type="button" id="hm-fee-owed">Add to books</button>' +
      "</div></div></div>"
    );
  }

  function bind(root, opts) {
    opts = opts || {};
    var box = root.querySelector("#hm-charge-box");
    var toggle = root.querySelector("#hm-charge");
    if (toggle && box) {
      toggle.onclick = function () {
        box.hidden = !box.hidden;
      };
    }
    function post(charge) {
      var st = root.querySelector("#hm-fee-st");
      var amt = root.querySelector("#hm-fee-amt");
      var dos = root.querySelector("#hm-fee-dos");
      var note = root.querySelector("#hm-fee-note");
      if (!opts.clientId) {
        if (st) st.textContent = "Open a directory client first.";
        return;
      }
      if (st) st.textContent = charge ? "Charging…" : "Saving…";
      var url =
        String(opts.apiBase || "").replace(/\/$/, "") +
        "/api/desk/fees";
      (window.deskFetch || fetch)(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          client_id: opts.clientId,
          dos: dos ? dos.value : "",
          amount: amt ? amt.value : "",
          note: note ? note.value : "",
          kind: "visit",
          event_id: opts.eventId || "",
          charge: !!charge,
        }),
      })
        .then(function (r) {
          return r.text().then(function (t) {
            var d = {};
            try {
              d = t ? JSON.parse(t) : {};
            } catch (_) {
              d = { detail: t.slice(0, 160) };
            }
            return { ok: r.ok, d: d };
          });
        })
        .then(function (x) {
          if (!x.ok || !x.d || !x.d.ok) {
            if (st) st.textContent = (x.d && (x.d.detail || x.d.error)) || "Could not post.";
            return;
          }
          if (typeof opts.onDone === "function") opts.onDone(x.d);
        })
        .catch(function (e) {
          if (st) st.textContent = (e && e.message) || "failed";
        });
    }
    var go = root.querySelector("#hm-fee-go");
    var add = root.querySelector("#hm-fee-owed");
    if (go) go.onclick = function () { post(true); };
    if (add) add.onclick = function () { post(false); };
    var invBtn = root.querySelector("#hm-invoice");
    var copyBtn = root.querySelector("#hm-inv-copy");
    var booksBox = root.querySelector("#hm-books");
    function say(t) {
      var st = root.querySelector("#hm-fee-st");
      if (!st && booksBox) {
        st = document.createElement("p");
        st.className = "home-empty";
        st.id = "hm-fee-st";
        st.style.margin = "0.35rem 0 0";
        booksBox.appendChild(st);
      }
      if (st) st.textContent = t || "";
    }
    if (copyBtn && opts.books && opts.books.invoice_url) {
      copyBtn.onclick = function () {
        var u = opts.books.invoice_url;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(u).then(function () { say("Pay link copied."); });
        }
      };
    }
    function invoiceOpen() {
      var url =
        String(opts.apiBase || "").replace(/\/$/, "") +
        "/api/desk/fees/invoice";
      return (window.deskFetch || fetch)(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_id: opts.clientId }),
      }).then(function (r) {
        return r.text().then(function (t) {
          var d = {};
          try { d = t ? JSON.parse(t) : {}; } catch (_) { d = { detail: t.slice(0, 160) }; }
          return { ok: r.ok, d: d };
        });
      });
    }
    if (invBtn) {
      invBtn.onclick = function () {
        if (!opts.clientId) {
          say("Open a directory client first.");
          return;
        }
        invBtn.disabled = true;
        say("Sending invoice…");
        var owed = Number((opts.books || {}).owed_cents || 0);
        var start = owed > 0
          ? Promise.resolve({ ok: true, d: { ok: true } })
          : (window.deskFetch || fetch)(
              String(opts.apiBase || "").replace(/\/$/, "") + "/api/desk/fees",
              {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  client_id: opts.clientId,
                  dos: opts.dos || new Date().toISOString().slice(0, 10),
                  amount: (opts.books && opts.books.suggest_pretty || "").replace(/[$,]/g, ""),
                  note: "Visit",
                  kind: "visit",
                  event_id: opts.eventId || "",
                  charge: false,
                }),
              }
            ).then(function (r) {
              return r.json().then(function (d) { return { ok: r.ok && d.ok, d: d }; });
            });
        start.then(function (made) {
          if (!made.ok) {
            invBtn.disabled = false;
            say((made.d && (made.d.detail || made.d.error)) || "Could not add the visit fee.");
            return null;
          }
          return invoiceOpen();
        }).then(function (x) {
          if (!x) return;
          invBtn.disabled = false;
          if (!x.ok || !x.d || !x.d.ok) {
            say((x.d && (x.d.detail || x.d.error)) || "Could not invoice.");
            return;
          }
          if (x.d.pay_url && navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(x.d.pay_url);
          }
          say("Invoice emailed. Pay link copied.");
          if (typeof opts.onDone === "function") opts.onDone(x.d);
        }).catch(function (e) {
          invBtn.disabled = false;
          say((e && e.message) || "failed");
        });
      };
    }
    var books = opts.books || {};
    if (root.querySelector("#hm-fee-note") && books.suggest_pretty && !root.querySelector("#hm-fee-note").value) {
      var cents = Number(books.suggest_cents || 0);
      var hint = cents === 60000 ? "90m visit" : cents === 42000 ? "60m visit" : "30m visit";
      root.querySelector("#hm-fee-note").placeholder = hint;
    }
    if (opts.dos && root.querySelector("#hm-fee-dos")) {
      root.querySelector("#hm-fee-dos").value = opts.dos;
    }
  }

  function byId(id) {
    return (
      document.getElementById(id) ||
      (global.__deskPop && global.__deskPop.document && global.__deskPop.document.getElementById(id))
    );
  }

  var booksOn = false;
  var booksMonth = "";

  function shiftMonth(key, delta) {
    var parts = (key || "2026-01").split("-").map(Number);
    var d = new Date(parts[0], parts[1] - 1 + delta, 1);
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0");
  }

  function paintBooks(data) {
    var mo = data.month || {};
    var yr = data.year || {};
    var title = byId("books-title");
    if (title) title.textContent = mo.label || "Books";
    function block(label, s, extra) {
      return (
        '<div class="books-block"><h4 style="margin:0 0 0.35rem">' + esc(label) + "</h4>" +
        '<div class="books-stat"><span>Collected</span><strong>' + esc(s.collected_pretty || "$0.00") + "</strong></div>" +
        '<div class="books-stat"><span>Outstanding</span><span>' + esc(s.owed_pretty || "$0.00") + "</span></div>" +
        '<div class="books-stat"><span>Waived</span><span>' + esc(s.waived_pretty || "$0.00") + "</span></div>" +
        '<div class="books-stat"><span>Visits · no-shows</span><span>' +
        esc(String(s.visits || 0)) + " · " + esc(String(s.no_shows || 0)) + "</span></div>" +
        (extra || "") +
        "</div>"
      );
    }
    var lines = (mo.lines || []).map(function (ln) {
      return (
        "<li><span>" + esc(ln.dos || "") +
        (ln.client_name ? " · " + esc(ln.client_name) : "") +
        (ln.note ? " · " + esc(ln.note) : "") +
        "</span><span class='desk-meta'>" + esc(ln.amount_pretty || "") +
        " · " + esc(ln.status || "") + "</span></li>"
      );
    }).join("");
    var recent = lines
      ? "<ul class='home-list'>" + lines + "</ul>"
      : "<p class='desk-meta'>No ledger lines in this month yet.</p>";
    byId("books-body").innerHTML = block(mo.label || "This month", mo, recent) + block(yr.label ? "Year " + yr.label : "This year", yr, "");
  }

  function loadBooks(month) {
    var status = byId("books-status");
    if (status) status.textContent = "Loading…";
    var q = month ? "?month=" + encodeURIComponent(month) : "";
    return global.deskFetch("/api/desk/books" + q).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        return { ok: res.ok, data: data };
      });
    }).then(function (x) {
      if (!x.ok || !x.data.ok) {
        if (status) status.textContent = x.data.detail || "Could not load books.";
        return;
      }
      booksMonth = (x.data.month && x.data.month.period) || month || "";
      if (status) status.textContent = "";
      paintBooks(x.data);
    });
  }

  function closeBooks() {
    booksOn = false;
    var pop = byId("books-pop");
    if (pop) pop.hidden = true;
    if (typeof global.syncSheetLock === "function") global.syncSheetLock();
    if (typeof global.applyTiles === "function") global.applyTiles();
  }

  function openBooks() {
    if (typeof global.booksWanted === "function" && global.booksWanted()) {
      closeBooks();
      return Promise.resolve();
    }
    if (typeof global.closeAllDrawers === "function") global.closeAllDrawers();
    if (typeof global.closePop === "function") global.closePop();
    if (typeof global.closeSend === "function") global.closeSend();
    if (typeof global.closeNote === "function") global.closeNote();
    if (typeof global.closeSuperbill === "function") global.closeSuperbill();
    var files = byId("files-pop");
    if (files) files.hidden = true;
    booksOn = true;
    var pop = byId("books-pop");
    if (pop) pop.hidden = false;
    if (typeof global.applyTiles === "function") global.applyTiles();
    return loadBooks(booksMonth).catch(function (e) {
      var status = byId("books-status");
      if (status) status.textContent = (e && e.message) || "failed";
    });
  }

  function bindBooksPanel() {
    var openBtn = byId("open-books");
    if (openBtn) {
      openBtn.onclick = function () {
        if (typeof global.closeMoreMenu === "function") global.closeMoreMenu();
        if (typeof global.needUnlock === "function" && global.needUnlock()) return;
        openBooks();
      };
    }
    var closeBtn = byId("books-close");
    if (closeBtn) closeBtn.onclick = closeBooks;
    var prev = byId("books-prev");
    if (prev) prev.onclick = function () { loadBooks(shiftMonth(booksMonth, -1)); };
    var next = byId("books-next");
    if (next) next.onclick = function () { loadBooks(shiftMonth(booksMonth, 1)); };
    var now = byId("books-now");
    if (now) now.onclick = function () { booksMonth = ""; loadBooks(""); };
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bindBooksPanel);
  else bindBooksPanel();

  global.DeskBooks = {
    chip: booksChip,
    form: formHtml,
    bind: bind,
    isOpen: function () { return booksOn; },
    markClosed: function () { booksOn = false; },
    close: closeBooks,
    open: openBooks,
  };
})(window);
