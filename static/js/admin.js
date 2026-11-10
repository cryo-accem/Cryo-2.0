// Admin-only behaviour (loaded with defer on /admin pages from base.html).

// Client-side table filters (text search + origin select).
document.querySelectorAll('[data-admin-filter]').forEach((filter) => {
  const scope = filter.closest('.data-stack');
  const search = filter.querySelector('[data-table-search]');
  const origin = filter.querySelector('[data-origin-filter]');
  const rows = scope ? scope.querySelectorAll('.data-table tbody tr') : [];
  const applyFilters = () => {
    const query = (search?.value || '').trim().toLowerCase();
    const selectedOrigin = (origin?.value || '').toLowerCase();
    rows.forEach((row) => {
      const matchesText = !query || row.textContent.toLowerCase().includes(query);
      const matchesOrigin = !selectedOrigin || (row.dataset.origin || '').toLowerCase() === selectedOrigin;
      row.hidden = !(matchesText && matchesOrigin);
    });
  };
  search?.addEventListener('input', applyFilters);
  origin?.addEventListener('change', applyFilters);
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
  form.addEventListener('submit', () => {
    const button = form.querySelector('button[type="submit"]');
    if (!button) return;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    const label = button.querySelector('[data-busy-text]') || button;
    label.textContent = form.dataset.busyLabel;
  });
});
