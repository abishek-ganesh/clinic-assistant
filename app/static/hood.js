// The "Under the hood" sidebar on the Ask and Book pages.
// Wide screens: a column beside the page, open by default, the choice remembered per browser.
// Narrow screens: a sheet over the page, closed until asked for.
// Loaded in <head> without defer, so the open or closed state is set before the first paint.

(() => {
  const KEY = "hood-open";
  const wide = window.matchMedia("(min-width: 60rem)");
  const root = document.documentElement;

  function saved() {
    try { return localStorage.getItem(KEY); } catch { return null; }
  }

  function remember(open) {
    try { localStorage.setItem(KEY, open ? "1" : "0"); } catch { /* private window: not remembered */ }
  }

  function preferred() {
    return wide.matches && saved() !== "0";
  }

  function isOpen() {
    return root.dataset.hood === "open";
  }

  function setOpen(open) {
    root.dataset.hood = open ? "open" : "closed";
    document.getElementById("hood-toggle")?.setAttribute("aria-expanded", String(open));
  }

  root.dataset.hood = preferred() ? "open" : "closed";

  // Pages call this before pointing at something inside the panel (a cited passage).
  window.showHood = () => { if (!isOpen()) setOpen(true); };

  document.addEventListener("DOMContentLoaded", () => {
    const toggle = document.getElementById("hood-toggle");
    const close = document.getElementById("hood-close");
    setOpen(isOpen());

    toggle.addEventListener("click", () => {
      const open = !isOpen();
      setOpen(open);
      if (wide.matches) remember(open);
      else if (open) close.focus();
    });

    close.addEventListener("click", () => {
      setOpen(false);
      if (wide.matches) remember(false);
      toggle.focus();
    });

    document.getElementById("hood-backdrop").addEventListener("click", () => setOpen(false));

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !wide.matches && isOpen()) {
        setOpen(false);
        toggle.focus();
      }
    });

    wide.addEventListener("change", () => setOpen(preferred()));
  });
})();
