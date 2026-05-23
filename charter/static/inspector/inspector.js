/* Charter Inspector — lightweight progressive enhancement.
 *
 * Clause folding is handled by Alpine.js `x-data="{ openId: null }"` in the
 * template itself, so this file is intentionally small. It adds:
 *
 *   1. Keyboard a11y for clause-toggle buttons (Enter / Space already work
 *      because they're <button>; we add an explicit focus ring outline).
 *   2. A "copy URL" helper for the inspector header — handy for sharing
 *      audit findings via chat/email.
 *
 * Loaded after HTMX + Alpine via the template's `<script defer>` tags.
 */

(function () {
  "use strict";

  // 1) Focus-visible outline for clause toggles.
  const style = document.createElement("style");
  style.textContent =
    ".clause-toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }";
  document.head.appendChild(style);

  // 2) Click-to-copy on the .charter-id header span.
  document.addEventListener("click", function (e) {
    const target = e.target;
    if (!(target instanceof Element)) return;
    if (target.classList.contains("charter-id")) {
      const text = target.textContent || "";
      if (navigator.clipboard && text) {
        navigator.clipboard.writeText(text.trim()).then(
          () => {
            const orig = target.getAttribute("title");
            target.setAttribute("title", "copied!");
            setTimeout(() => target.setAttribute("title", orig || ""), 1200);
          },
          () => {
            /* clipboard denied — silent no-op */
          }
        );
      }
    }
  });
})();
