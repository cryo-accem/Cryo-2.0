// Publications page: keep the "Jump to year" chips in step with the search
// filter in site.js. Without JS the chips are plain in-page anchors.
(function() {
  const list = document.querySelector(".publication-list");
  const links = Array.from(document.querySelectorAll("[data-pub-year-link]"));
  if (!list || !links.length) return;

  const groups = new Map(
    Array.from(list.querySelectorAll("[data-pub-year]")).map((group) => [group.dataset.pubYear, group])
  );

  const sync = () => {
    links.forEach((link) => {
      const group = groups.get(link.dataset.pubYearLink);
      if (!group) return;
      const shown = group.querySelectorAll("[data-pub-item]:not([hidden])").length;
      const count = link.querySelector("span");
      if (count) count.textContent = String(shown);
      const headingCount = group.querySelector(".pub-year__count");
      if (headingCount) headingCount.textContent = String(shown);
      link.parentElement.hidden = group.hidden || shown === 0;
    });
  };

  // site.js toggles the `hidden` attribute on items and year groups while
  // filtering; observe those changes instead of depending on script order.
  const observer = new MutationObserver(() => window.requestAnimationFrame(sync));
  observer.observe(list, { subtree: true, attributes: true, attributeFilter: ["hidden"] });

  links.forEach((link) => {
    link.addEventListener("click", () => {
      const heading = document.getElementById(link.getAttribute("href").slice(1));
      if (heading) {
        heading.setAttribute("tabindex", "-1");
        window.requestAnimationFrame(() => heading.focus({ preventScroll: true }));
      }
    });
  });
})();
