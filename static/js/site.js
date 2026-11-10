// Site-wide behaviour (loaded with defer from base.html).

// Facility announcement banner: dismiss button. Dismissal is remembered per
// notice id in localStorage, so a newly published notice shows up again.
(function() {
  const STORAGE_KEY = "accem-dismissed-notices";
  const readDismissed = () => {
    try {
      const value = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "[]");
      return Array.isArray(value) ? value.map(String) : [];
    } catch (error) {
      return [];
    }
  };
  const writeDismissed = (ids) => {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(ids.slice(-50)));
    } catch (error) {
      // Storage unavailable (private mode, blocked): dismissal lasts for this page only.
    }
  };

  document.querySelectorAll(".facility-notice").forEach((strip) => {
    const dismissed = new Set(readDismissed());
    const items = Array.from(strip.querySelectorAll("[data-notice-id]"));
    items.forEach((item) => {
      if (dismissed.has(item.dataset.noticeId)) item.remove();
    });
    if (items.length && !strip.querySelector("[data-notice-id]")) {
      strip.remove();
      return;
    }
    strip.querySelector(".facility-notice__dismiss")?.addEventListener("click", () => {
      const ids = readDismissed();
      strip.querySelectorAll("[data-notice-id]").forEach((item) => {
        if (!ids.includes(item.dataset.noticeId)) ids.push(item.dataset.noticeId);
      });
      writeDismissed(ids);
      strip.remove();
    });
  });
})();

// "Copy text" buttons (e.g. the acknowledgement on the Instructions page).
(function() {
  function fallbackCopy(text) {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (error) {
      ok = false;
    }
    area.remove();
    return ok;
  }

  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    const label = button.querySelector("[data-copy-label]");
    const status = button.parentElement?.querySelector("[data-copy-status]");
    const original = label ? label.textContent : "";
    let timer = null;
    const report = (ok) => {
      if (label) label.textContent = ok ? "Copied" : "Press Ctrl+C to copy";
      if (status) status.textContent = ok ? "Acknowledgement copied to clipboard." : "Copy failed; select the text and copy it manually.";
      button.classList.toggle("is-copied", ok);
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (label) label.textContent = original;
        if (status) status.textContent = "";
        button.classList.remove("is-copied");
      }, 2000);
    };
    button.addEventListener("click", async () => {
      const source = document.getElementById(button.dataset.copyTarget);
      if (!source) return;
      const text = source.textContent.replace(/\s+/g, " ").trim();
      let ok = false;
      if (navigator.clipboard && window.isSecureContext) {
        try {
          await navigator.clipboard.writeText(text);
          ok = true;
        } catch (error) {
          ok = fallbackCopy(text);
        }
      } else {
        ok = fallbackCopy(text);
      }
      if (!ok) {
        const range = document.createRange();
        range.selectNodeContents(source);
        const selection = window.getSelection();
        selection?.removeAllRanges();
        selection?.addRange(range);
      }
      report(ok);
    });
  });
})();

// Publications page: client-side search. Without JS every entry stays visible.
(function() {
  const form = document.querySelector("[data-pub-search]");
  if (!form) return;
  const input = form.querySelector("input[type='search']");
  const status = document.querySelector("[data-pub-status]");
  const empty = document.querySelector("[data-pub-empty]");
  const items = Array.from(document.querySelectorAll("[data-pub-item]"));
  const groups = Array.from(document.querySelectorAll("[data-pub-year]"));
  const total = items.length;
  const haystacks = new Map(items.map((item) => [item, item.textContent.toLowerCase()]));
  form.hidden = false;
  form.addEventListener("submit", (event) => event.preventDefault());

  const apply = () => {
    const terms = input.value.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    items.forEach((item) => {
      const match = terms.every((term) => haystacks.get(item).includes(term));
      item.hidden = !match;
      if (match) shown += 1;
    });
    groups.forEach((group) => {
      group.hidden = !group.querySelector("[data-pub-item]:not([hidden])");
    });
    if (empty) empty.hidden = shown !== 0;
    if (status) {
      status.textContent = terms.length
        ? `${shown} of ${total} publications`
        : `${total} publications`;
    }
  };
  input.addEventListener("input", apply);
})();

// Donut chart tooltips (used for server-rendered and dynamically rendered charts)
function initDonutTooltips(root) {
  root.querySelectorAll('[data-donut]').forEach((donut) => {
    const tooltip = donut.querySelector('.donut-tooltip');
    if (!tooltip) return;
    donut.querySelectorAll('.donut-segment').forEach((segment) => {
      const showTooltip = () => {
        const value = document.createElement("strong");
        value.textContent = segment.dataset.value;
        tooltip.replaceChildren(
          document.createTextNode(`${segment.dataset.label} `),
          document.createElement("br"),
          value,
        );
        tooltip.classList.add('is-visible');
      };
      const hideTooltip = () => tooltip.classList.remove('is-visible');
      segment.addEventListener('mouseenter', showTooltip);
      segment.addEventListener('mouseleave', hideTooltip);
      segment.addEventListener('focus', showTooltip);
      segment.addEventListener('blur', hideTooltip);
    });
  });
}
initDonutTooltips(document);

// Dynamic user statistics chart loader.
// The home page may embed the initial payload as
// <script type="application/json" id="user-stats-initial"> (same shape as
// /api/user-statistics); it is rendered immediately and the API is only
// fetched when that payload is missing or invalid.
(function() {
  let lastDataHash = null;

  function hashData(data) {
    return JSON.stringify(data).split('').reduce((a, b) => {
      a = ((a << 5) - a) + b.charCodeAt(0);
      return a & a;
    }, 0);
  }

  function showResult(result) {
    const dataHash = hashData([result.total, result.data, result.metrics]);

    // Only update if data has changed
    if (lastDataHash !== null && lastDataHash === dataHash) return;

    lastDataHash = dataHash;
    renderChart(result);
  }

  async function loadUserStatistics() {
    try {
      const response = await fetch('/api/user-statistics');
      if (!response.ok) throw new Error('Failed to fetch user statistics');

      const result = await response.json();
      if (!result.success) throw new Error(result.error);

      showResult(result);
    } catch (error) {
      const indicator = document.getElementById('loading-indicator');
      if (indicator) indicator.textContent = 'Statistics are unavailable right now.';
    }
  }

  function readInitialStatistics() {
    const node = document.getElementById('user-stats-initial');
    if (!node) return null;
    try {
      const result = JSON.parse(node.textContent);
      if (!result || result.success === false || !Array.isArray(result.data)) return null;
      return result;
    } catch (error) {
      return null;
    }
  }

  function setText(container, selector, text) {
    container.querySelectorAll(selector).forEach((node) => { node.textContent = text; });
  }

  // Updates the server-rendered tiles + donut in place (same markup as home.html).
  function renderChart(result) {
    const container = document.getElementById('user-stats-container');
    if (!container) return;

    const { total, data } = result;
    const metrics = result.metrics || {};

    setText(container, '[data-stat="users"]', String(total));
    setText(container, '[data-stat="users-note"]', `across ${data.length} affiliation categories`);
    setText(container, '[data-stat="users-total"]', `${total} users`);
    setText(container, '.donut-center strong', String(total));
    const internal = data
      .filter((item) => item.label === 'Internal')
      .reduce((sum, item) => sum + Number(item.value || 0), 0);
    setText(container, '[data-stat="external"]', String(total - internal));
    if (metrics.publications !== undefined && metrics.publications !== null) {
      setText(container, '[data-stat="publications"]', String(metrics.publications));
    }
    if (metrics.structures !== undefined && metrics.structures !== null) {
      setText(container, '[data-stat="structures"]', String(metrics.structures));
    }

    const segmentGroup = container.querySelector("[data-chart-segments]");
    const legend = container.querySelector("[data-stats-legend]");
    const svgNamespace = "http://www.w3.org/2000/svg";
    if (segmentGroup) segmentGroup.replaceChildren();
    if (legend) legend.replaceChildren();
    data.forEach((item) => {
      if (segmentGroup) {
        const segment = document.createElementNS(svgNamespace, "circle");
        segment.setAttribute("class", "donut-segment");
        segment.setAttribute("tabindex", "0");
        segment.setAttribute("role", "img");
        segment.setAttribute("aria-label", `${item.label}: ${item.value} users`);
        segment.setAttribute("cx", "60");
        segment.setAttribute("cy", "60");
        segment.setAttribute("r", "52");
        segment.setAttribute("pathLength", "100");
        // Presentation attributes (not inline styles) so the markup matches home.html.
        segment.setAttribute("color", item.color);
        segment.setAttribute("stroke-dasharray", `${item.end - item.start} 100`);
        segment.setAttribute("stroke-dashoffset", `-${item.start}`);
        segment.dataset.label = item.label;
        segment.dataset.value = `${item.value} users`;
        segmentGroup.append(segment);
      }
      if (legend) {
        const row = document.createElement("li");
        const swatch = document.createElementNS(svgNamespace, "svg");
        swatch.setAttribute("class", "stats-e__swatch");
        swatch.setAttribute("viewBox", "0 0 12 12");
        swatch.setAttribute("aria-hidden", "true");
        const swatchRect = document.createElementNS(svgNamespace, "rect");
        swatchRect.setAttribute("width", "12");
        swatchRect.setAttribute("height", "12");
        swatchRect.setAttribute("rx", "3");
        swatchRect.setAttribute("fill", item.color);
        swatch.append(swatchRect);
        const label = document.createElement("span");
        label.textContent = item.label;
        const value = document.createElement("strong");
        value.textContent = String(item.value);
        row.append(swatch, label, value);
        legend.append(row);
      }
    });
    container.dataset.statsState = 'ready';
    initDonutTooltips(container);
  }

  // Render once on page load (embedded payload first, API otherwise), then
  // refresh at most every 5 minutes while the tab is visible.
  if (document.getElementById('user-stats-container')) {
    const REFRESH_MS = 5 * 60 * 1000;
    let lastLoaded = Date.now();
    const initial = readInitialStatistics();
    const container = document.getElementById('user-stats-container');
    if (initial && container.dataset.statsState === 'ready') {
      // Already rendered server-side from the same payload: just remember it.
      lastDataHash = hashData([initial.total, initial.data, initial.metrics]);
    } else if (initial) {
      showResult(initial);
    } else {
      loadUserStatistics();
    }
    const refreshIfStale = () => {
      if (document.visibilityState !== 'visible' || Date.now() - lastLoaded < REFRESH_MS) return;
      lastLoaded = Date.now();
      loadUserStatistics();
    };
    setInterval(refreshIfStale, REFRESH_MS);
    document.addEventListener('visibilitychange', refreshIfStale);
  }
})();
