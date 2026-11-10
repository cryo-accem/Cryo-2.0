/* Completion modal: choose which reserved grids were actually used.
 *
 * Every linked grid card gets a "Used in this session" tick box (ticked by
 * default) and a hidden inventory_screened value sent with the form.  When
 * Actual grids is lower than the number of reserved grids, the admin ticks
 * exactly that many; the unticked grids go back to storage unused (not
 * clipped, not billed).  The server enforces the same rule.
 */
(function () {
  "use strict";

  function cardsIn(container) {
    return [...container.querySelectorAll("[data-used-grid]")];
  }

  function statusFor(container) {
    let status = container.querySelector("[data-used-status]");
    if (!status) {
      status = document.createElement("p");
      status.dataset.usedStatus = "";
      status.className = "completion-used-status";
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      const heading = container.querySelector("h3");
      if (heading) heading.after(status);
      else container.prepend(status);
    }
    return status;
  }

  function refresh(container, actualInput) {
    const cards = cardsIn(container);
    if (!cards.length) return;
    const actual = Number.parseInt(actualInput.value, 10) || 0;
    const needed = actual ? Math.min(actual, cards.length) : cards.length;
    const ticked = cards.filter((card) => card.querySelector("[data-used-toggle]").checked).length;
    const status = statusFor(container);
    const mismatch = ticked !== needed;
    if (!mismatch && ticked === cards.length) {
      status.hidden = true;
    } else {
      status.hidden = false;
      status.textContent = mismatch
        ? `Actual grids is ${actual}: tick the ${needed} reserved grid${needed === 1 ? "" : "s"} that were used (${ticked} ticked now).`
        : `${ticked} of ${cards.length} reserved grids used. The other ${cards.length - ticked} go back still Frozen — not clipped or billed.`;
    }
    status.classList.toggle("is-error", mismatch);
    const first = cards[0].querySelector("[data-used-toggle]");
    first.setCustomValidity(mismatch ? `Tick exactly ${needed} used grid${needed === 1 ? "" : "s"}.` : "");
  }

  function usedGrids(container) {
    return cardsIn(container)
      .filter((card) => card.querySelector("[data-used-toggle]").checked)
      .map((card) => card.usedGrid);
  }

  /**
   * card     the grid's fieldset (already holds the inventory_grid_code input)
   * options  { grid, outcome (select), syncOutcome (), container, actualInput, form }
   */
  function attach(card, options) {
    const { grid, outcome, syncOutcome, container, actualInput, form } = options;
    card.dataset.usedGrid = "";
    card.usedGrid = grid;
    const hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = "inventory_screened";
    hidden.value = "1";
    const code = card.querySelector("input[name=inventory_grid_code]");
    (code || card.querySelector("legend")).after(hidden);

    const toggleLabel = document.createElement("label");
    toggleLabel.className = "completion-used-toggle";
    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.checked = true;
    toggle.dataset.usedToggle = "";
    toggleLabel.append(toggle, document.createTextNode(" Used in this session"));
    hidden.after(toggleLabel);

    const note = document.createElement("p");
    note.className = "completion-unused-note";
    note.textContent = "Not inserted — this grid goes back to its place still Frozen (its status before this booking), not Stored. It is not clipped or billed. Check the place below.";
    note.hidden = true;
    toggleLabel.after(note);

    const outcomeLabel = outcome.closest("label");
    toggle.addEventListener("change", () => {
      const used = toggle.checked;
      hidden.value = used ? "1" : "0";
      card.classList.toggle("is-unused", !used);
      note.hidden = used;
      if (!used) outcome.value = "store";
      if (outcomeLabel) outcomeLabel.hidden = !used;
      syncOutcome();
      refresh(container, actualInput);
      form.dispatchEvent(new CustomEvent("used-grids-change", { detail: { grids: usedGrids(container) } }));
    });
  }

  window.AccemUsedGrids = { attach, refresh, usedGrids };
})();
