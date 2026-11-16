// Admin-only behaviour (loaded with defer on /admin pages from base.html).

// Client-side table filters (text search + origin select).
document.querySelectorAll('[data-admin-filter]').forEach((filter) => {
  const scope = filter.closest('.data-stack');
  const search = filter.querySelector('[data-table-search]');
  const origin = filter.querySelector('[data-origin-filter]');
  const sheet = filter.querySelector('[data-sheet-filter]');
  const count = filter.querySelector('[data-filter-count]');
  const rows = scope ? scope.querySelectorAll('.data-table tbody tr') : [];
  // "Open Billing" from the dashboard's "Bills to send" card arrives with
  // ?sheet=unsent, so only bookings still waiting for a charge sheet show.
  const requestedSheet = new URLSearchParams(window.location.search).get('sheet');
  if (sheet && ['unsent', 'sent'].includes(requestedSheet)) sheet.value = requestedSheet;
  const applyFilters = () => {
    const query = (search?.value || '').trim().toLowerCase();
    const selectedOrigin = (origin?.value || '').toLowerCase();
    const selectedSheet = sheet?.value || '';
    let shown = 0;
    rows.forEach((row) => {
      const matchesText = !query || row.textContent.toLowerCase().includes(query);
      const matchesOrigin = !selectedOrigin || (row.dataset.origin || '').toLowerCase() === selectedOrigin;
      const matchesSheet = !selectedSheet || row.dataset.sheet === selectedSheet;
      row.hidden = !(matchesText && matchesOrigin && matchesSheet);
      if (!row.hidden) shown += 1;
    });
    // Hide service sections left with no matching rows while filtering.
    const filtering = Boolean(selectedSheet || query || selectedOrigin);
    scope?.querySelectorAll('[data-history-panel]').forEach((panel) => {
      const visible = [...panel.querySelectorAll('.data-table tbody tr')].some((row) => !row.hidden);
      panel.classList.toggle('is-filtered-empty', filtering && !visible);
    });
    if (count) {
      count.textContent = selectedSheet || query || selectedOrigin
        ? `${shown} booking${shown === 1 ? '' : 's'} shown${selectedSheet === 'unsent' ? ' · charge sheet not sent' : ''}`
        : '';
    }
  };
  search?.addEventListener('input', applyFilters);
  origin?.addEventListener('change', applyFilters);
  sheet?.addEventListener('change', applyFilters);
  if (sheet?.value) applyFilters();
});

// Billing history service tabs.
document.querySelectorAll('[data-history-tabs]').forEach((tabs) => {
  const stack = tabs.nextElementSibling;
  const panels = stack?.querySelectorAll('[data-history-panel]') || [];
  tabs.querySelectorAll('[data-history-tab]').forEach((tab) => {
    tab.addEventListener('click', () => {
      const selectedService = tab.dataset.historyTab;
      tabs.querySelectorAll('[data-history-tab]').forEach((item) => {
        const active = item === tab;
        item.classList.toggle('is-active', active);
        item.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      panels.forEach((panel) => {
        panel.hidden = selectedService !== 'all' && panel.dataset.historyPanel !== selectedService;
      });
    });
  });
});

// Long-running form posts (e.g. the publication finder search): disable the
// button and show progress text so the request is not submitted twice.
document.querySelectorAll('form[data-busy-label]').forEach((form) => {
  form.addEventListener('submit', (event) => {
    if (event.defaultPrevented) return;
    const button = form.querySelector('button[type="submit"]');
    if (!button) return;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    const label = button.querySelector('[data-busy-text]') || button;
    label.textContent = form.dataset.busyLabel;
  });
});

// Dashboard revenue bar chart: when there are more bars than fit, start the
// scrollable chart at the most recent period instead of the oldest one, and
// let keyboard users scroll it.
document.querySelectorAll('.revenue-bars').forEach((chart) => {
  if (chart.scrollWidth <= chart.clientWidth) return;
  chart.classList.add('is-scrollable');
  chart.tabIndex = 0;
  chart.scrollLeft = chart.scrollWidth;
});

// Phone layout: the sidebar becomes a horizontal strip, so bring the current
// page's link into view instead of always showing the first few items.
(() => {
  const nav = document.querySelector('.admin-sidebar nav');
  const active = nav && nav.querySelector('a.active');
  if (!active) return;
  const centre = () => {
    if (nav.scrollWidth <= nav.clientWidth) return;
    const navBox = nav.getBoundingClientRect();
    const linkBox = active.getBoundingClientRect();
    nav.scrollLeft += linkBox.left - navBox.left - (navBox.width - linkBox.width) / 2;
  };
  centre();
  // Link widths change once the web font has loaded; centre again then.
  if (document.fonts) document.fonts.ready.then(centre);
})();

// Billing & payments filters run on the server: apply a dropdown as soon as
// it changes (the search box applies on Enter or with the Apply button).
document.querySelectorAll('[data-history-filters]').forEach((form) => {
  form.querySelectorAll('select').forEach((select) => {
    select.addEventListener('change', () => form.requestSubmit());
  });
  // Leave empty filters out of the URL (?receipt=awaiting, not ?q=&origin=&...).
  form.addEventListener('submit', () => {
    form.querySelectorAll('[name]').forEach((field) => { if (!field.value) field.disabled = true; });
  });
  // Coming back with the browser's Back button restores the page as it was.
  window.addEventListener('pageshow', () => {
    form.querySelectorAll('[name]').forEach((field) => { field.disabled = false; });
  });
});
