// Primary navigation: collapsible menu on narrow screens and the "Facility" /
// "Using ACCEM" dropdowns on desktop.
// The "js" class lets CSS collapse the menu only when this script runs; without
// JavaScript the full link list stays visible and dropdowns open on hover/focus.
document.documentElement.classList.add("js");

document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.querySelector(".nav-toggle");
  const nav = toggle && document.getElementById(toggle.getAttribute("aria-controls"));
  if (toggle && nav) {
    toggle.hidden = false;
    const setOpen = (open) => {
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      nav.classList.toggle("is-open", open);
    };
    toggle.addEventListener("click", () => setOpen(toggle.getAttribute("aria-expanded") !== "true"));
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") {
        setOpen(false);
        toggle.focus();
      }
    });
  }

  // Dropdown groups: click/Enter toggles, Escape or a click outside closes,
  // and only one group is open at a time.
  const groups = [...document.querySelectorAll(".nav-group")];
  const closeAll = (except) => {
    groups.forEach((group) => {
      if (group === except) return;
      group.classList.remove("is-open");
      group.querySelector(".nav-group__toggle")?.setAttribute("aria-expanded", "false");
    });
  };
  groups.forEach((group) => {
    const button = group.querySelector(".nav-group__toggle");
    if (!button) return;
    button.addEventListener("click", () => {
      const open = !group.classList.contains("is-open");
      closeAll(group);
      group.classList.toggle("is-open", open);
      button.setAttribute("aria-expanded", open ? "true" : "false");
    });
    group.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && group.classList.contains("is-open")) {
        closeAll();
        button.focus();
      }
    });
    group.addEventListener("focusout", (event) => {
      if (!group.contains(event.relatedTarget)) {
        group.classList.remove("is-open");
        button.setAttribute("aria-expanded", "false");
      }
    });
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".nav-group")) closeAll();
  });
});
