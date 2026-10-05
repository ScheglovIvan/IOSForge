// Theme: apply saved preference before paint, toggle + persist. No dependencies.
(function () {
  var KEY = "iosforge-theme";
  try {
    var saved = localStorage.getItem(KEY);
    if (saved === "dark" || saved === "light") {
      document.documentElement.setAttribute("data-theme", saved);
    }
  } catch (e) {}

  function current() {
    var attr = document.documentElement.getAttribute("data-theme");
    if (attr) return attr;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest && ev.target.closest("[data-theme-toggle]");
    if (!btn) return;
    var next = current() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem(KEY, next);
    } catch (e) {}
  });

  // Job detail tabs: keep the active tab across the periodic meta-refresh.
  var TAB_KEY = "iosforge-jobtab";
  function isTabRadio(el) {
    return el && el.classList && el.classList.contains("tab-radio");
  }
  document.addEventListener("DOMContentLoaded", function () {
    var radios = document.querySelectorAll('.tab-radio[name="jobtab"]');
    if (!radios.length) return;
    var want = null;
    var h = (location.hash || "").replace("#", "");
    if (h && isTabRadio(document.getElementById(h))) want = h;
    if (!want) {
      try {
        want = sessionStorage.getItem(TAB_KEY);
      } catch (e) {}
    }
    if (want) {
      var r = document.getElementById(want);
      if (isTabRadio(r)) r.checked = true;
    }
    radios.forEach(function (rd) {
      rd.addEventListener("change", function () {
        try {
          sessionStorage.setItem(TAB_KEY, rd.id);
        } catch (e) {}
      });
    });
  });
})();

/* Delete a generated asset in place.
   A full page reload would rebuild every gallery and lose the scroll position for
   what is a one-tile change, so the row is removed from the DOM instead. The
   double confirm is deliberate: these are generations that took a minute each and
   the icon delete is not recoverable. */
(function () {
  document.addEventListener('click', function (event) {
    const button = event.target.closest('.del-x');
    if (!button || button.disabled) return;

    const what = button.dataset.what || 'this item';
    if (!confirm('Delete ' + what + '?')) return;
    if (!confirm('This cannot be undone. Delete ' + what + ' for good?')) return;

    const csrf = document.body.dataset.csrf || '';
    const body = new URLSearchParams();
    body.set('csrf_token', csrf);
    body.set(button.dataset.field, button.dataset.value);

    button.disabled = true;
    const tile = button.closest('.icon-candidate, .thumb');
    if (tile) tile.classList.add('is-going');

    fetch(button.dataset.url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Requested-With': 'fetch'
      },
      body: body.toString(),
      credentials: 'same-origin'
    })
      .then(function (response) {
        if (!response.ok) throw new Error(String(response.status));
        if (tile) tile.remove();
      })
      .catch(function () {
        button.disabled = false;
        if (tile) tile.classList.remove('is-going');
        alert('Could not delete it — the server refused. Reload and try again.');
      });
  });
})();
