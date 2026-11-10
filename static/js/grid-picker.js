/* Inventory grid picker: grids grouped by freezing session.
 *
 * Each session is a drop-down headed "Frozen 12 Oct 2026 · Sample" with how
 * many grids are available and of which type; inside, each grid is shown by
 * its grid type and place in the session ("Gold Quantifoil · grid 2 of 4"),
 * never by its internal ACCEM-G-… reference (kept only as the input value).
 */
(function () {
  "use strict";

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function locationText(grid) {
    if (!grid.falcon_container_id) return "Not in storage yet";
    const label = (name, word) => (String(name).toLowerCase().includes(word.toLowerCase()) ? String(name) : `${word} ${name}`);
    let text = label(grid.falcon_container_id, "Falcon");
    if (grid.grid_box_name) text += ` · ${label(grid.grid_box_name, "box")}`;
    if (grid.grid_position) text += ` · position ${grid.grid_position}`;
    return text;
  }

  function typeSummary(grids) {
    const counts = new Map();
    grids.forEach((grid) => {
      const name = grid.grid_type_name || "Grid";
      counts.set(name, (counts.get(name) || 0) + 1);
    });
    return [...counts].map(([name, count]) => `${name} × ${count}`).join(", ");
  }

  function groupBySession(grids) {
    const groups = new Map();
    grids.forEach((grid) => {
      const key = grid.session_key || grid.sample_name || "grids";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(grid);
    });
    return [...groups.values()];
  }

  /**
   * container  element to fill (its children are replaced)
   * grids      grid objects from the inventory search API
   * options    { inputName, limit (number|null), selected (iterable of codes),
   *              unavailable (codes still selected but no longer available),
   *              onChange () }
   */
  function render(container, grids, options) {
    const opts = Object.assign({ inputName: "inventory_grid_codes", limit: null }, options || {});
    const selected = new Set(opts.selected || []);
    container.replaceChildren();
    container.classList.add("grid-picker");
    const groups = groupBySession(grids);

    const enforceLimit = (changed) => {
      if (opts.limit === null || opts.limit === undefined) return;
      const checked = container.querySelectorAll(`input[name="${opts.inputName}"]:checked`);
      if (checked.length > opts.limit && changed) changed.checked = false;
    };

    groups.forEach((sessionGrids, index) => {
      const first = sessionGrids[0];
      const details = el("details", "grid-session");
      details.open = groups.length <= 2 || index === 0
        || sessionGrids.some((grid) => selected.has(grid.grid_code));
      const summary = el("summary", "grid-session__summary");
      const title = el("span", "grid-session__title");
      const icon = el("i", "fa-solid fa-snowflake");
      icon.setAttribute("aria-hidden", "true");
      const date = el("strong", "", first.frozen_label ? `Frozen ${first.frozen_label}` : "Not frozen yet");
      const dot = el("span", "grid-session__dot", "·");
      dot.setAttribute("aria-hidden", "true");
      title.append(icon, date, dot, el("strong", "grid-session__sample", first.sample_name || "Sample"));
      const total = first.session_total || sessionGrids.length;
      const meta = el(
        "span", "grid-session__meta",
        `${sessionGrids.length} of ${total} grid${total === 1 ? "" : "s"} available · ${typeSummary(sessionGrids)}`,
      );
      summary.append(title, meta);
      details.append(summary);

      const list = el("div", "grid-session__grids");
      sessionGrids.forEach((grid) => {
        const choice = el("label", "grid-choice");
        const checkbox = el("input");
        checkbox.type = "checkbox";
        checkbox.name = opts.inputName;
        checkbox.value = grid.grid_code;
        checkbox.checked = selected.has(grid.grid_code);
        checkbox.addEventListener("change", () => {
          enforceLimit(checkbox);
          if (opts.onChange) opts.onChange();
        });
        const copy = el("span", "grid-choice__copy");
        copy.append(
          el("strong", "", grid.grid_type_name || "Grid"),
          el("small", "", grid.grid_number ? `Grid ${grid.grid_number} of ${grid.session_total || total}` : (grid.sample_name || "")),
        );
        choice.append(checkbox, copy, el("small", "grid-choice__location", locationText(grid)));
        list.append(choice);
      });
      details.append(list);
      container.append(details);
    });

    (opts.unavailable || []).forEach((code) => {
      const choice = el("label", "grid-choice grid-choice--unavailable");
      const checkbox = el("input");
      checkbox.type = "checkbox";
      checkbox.name = opts.inputName;
      checkbox.value = code;
      checkbox.checked = true;
      checkbox.addEventListener("change", () => { if (opts.onChange) opts.onChange(); });
      const copy = el("span", "grid-choice__copy");
      copy.append(el("strong", "", "Previously selected grid"),
        el("small", "", "No longer available for this username. Untick to remove it."));
      choice.append(checkbox, copy);
      container.append(choice);
    });
  }

  /* "Is this you?" choices for names recorded slightly differently
   * ("Bapan" / "Bapan Mondal" / "Bapen").  onPick(name) loads that name's grids. */
  function renderNameChoices(container, suggestions, heading, onPick) {
    if (!suggestions || !suggestions.length) return;
    const box = el("div", "grid-picker-names");
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", "Similar recorded names");
    box.append(el("p", "", heading));
    const list = el("div", "grid-picker-names__list");
    suggestions.forEach((item) => {
      const button = el("button");
      button.type = "button";
      button.append(
        el("strong", "", item.name),
        el("small", "", `${item.grid_count} grid${item.grid_count === 1 ? "" : "s"} on record · Yes, this is me`),
      );
      button.addEventListener("click", () => onPick(item.name));
      list.append(button);
    });
    box.append(list);
    container.append(box);
  }

  /* "Showing grids recorded under …  Not you?" line above the grids. */
  function renderOwnerNote(container, owner, onReset) {
    const note = el("p", "grid-picker-owner", `Showing grids recorded under “${owner}”.`);
    const reset = el("button", "", "Not you?");
    reset.type = "button";
    reset.addEventListener("click", onReset);
    note.append(reset);
    container.prepend(note);
  }

  window.AccemGridPicker = { render, renderNameChoices, renderOwnerNote };
})();
