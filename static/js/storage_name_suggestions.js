(() => {
  const script = document.currentScript;
  const endpoint = script?.dataset.suggestionsUrl;
  if (!endpoint) return;

  const suggestionsByType = {};
  let suggestionsRequest;
  let nextMenuId = 0;
  let openMenu = null;
  const inputSelector = [
    'input:not([type])[name*="box"]',
    'input[type="text"][name*="box"]',
    'input[type="search"][name*="box"]',
    'input:not([type])[name*="falcon"]',
    'input[type="text"][name*="falcon"]',
    'input[type="search"][name*="falcon"]',
    'input:not([type])[name*="container"]',
    'input[type="text"][name*="container"]',
    'input[type="search"][name*="container"]',
  ].join(",");

  const typeFor = (input) =>
    input.name.toLowerCase().includes("box") ? "boxes" : "falcons";

  const loadSuggestions = () => {
    if (!suggestionsRequest) {
      suggestionsRequest = fetch(endpoint, { headers: { Accept: "application/json" } })
        .then((response) => {
          if (!response.ok) throw new Error("Storage suggestions could not be loaded.");
          return response.json();
        })
        .then((data) => {
          suggestionsByType.boxes = Array.isArray(data.boxes) ? data.boxes : [];
          suggestionsByType.falcons = Array.isArray(data.falcons) ? data.falcons : [];
          return suggestionsByType;
        })
        .catch((error) => {
          suggestionsRequest = null;
          throw error;
        });
    }
    return suggestionsRequest;
  };

  const hideMenu = (input, menu) => {
    menu.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    if (openMenu?.menu === menu) openMenu = null;
  };

  const positionMenu = (input, menu) => {
    const rect = input.getBoundingClientRect();
    const width = Math.min(rect.width, window.innerWidth - 16);
    const left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
    const below = rect.bottom + 5;
    const top = below + menu.offsetHeight <= window.innerHeight - 8
      ? below
      : Math.max(8, rect.top - menu.offsetHeight - 5);
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
    menu.style.width = `${width}px`;
  };

  const updateMenu = (input, menu, error = false) => {
    menu.replaceChildren();
    input.removeAttribute("aria-activedescendant");
    const heading = document.createElement("span");
    heading.className = "storage-name-suggestions__heading";
    heading.textContent = typeFor(input) === "boxes" ? "Previous grid boxes" : "Previous Falcon names";
    menu.append(heading);

    if (error) {
      const message = document.createElement("span");
      message.className = "storage-name-suggestions__empty";
      message.textContent = "Suggestions are unavailable. You can still enter a name.";
      menu.append(message);
      menu.hidden = false;
      input.setAttribute("aria-expanded", "true");
      openMenu = { input, menu };
      positionMenu(input, menu);
      return;
    }

    const query = input.value.trim().toLocaleLowerCase();
    const matches = (suggestionsByType[typeFor(input)] || [])
      .filter((name) => name.toLocaleLowerCase().includes(query))
      .slice(0, 7);
    if (!matches.length) {
      const message = document.createElement("span");
      message.className = "storage-name-suggestions__empty";
      message.textContent = query
        ? "No previous name matches. You can enter a new name."
        : "No previous names yet. You can enter a new name.";
      menu.append(message);
      menu.hidden = false;
      input.setAttribute("aria-expanded", "true");
      openMenu = { input, menu };
      positionMenu(input, menu);
      return;
    }

    matches.forEach((name, index) => {
      const option = document.createElement("button");
      option.type = "button";
      option.id = `${menu.id}-option-${index}`;
      option.className = "storage-name-suggestions__option";
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", "false");
      option.textContent = name;
      option.addEventListener("pointerdown", (event) => event.preventDefault());
      option.addEventListener("click", () => {
        input.value = name;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        hideMenu(input, menu);
        input.focus();
      });
      menu.append(option);
    });
    menu.hidden = false;
    input.setAttribute("aria-expanded", "true");
    openMenu = { input, menu };
    positionMenu(input, menu);
  };

  const attach = (input) => {
    if (input.dataset.storageSuggestionsAttached) return;
    input.dataset.storageSuggestionsAttached = "true";
    input.setAttribute("autocomplete", "off");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");

    const menu = document.createElement("div");
    menu.id = `storage-name-suggestions-${nextMenuId++}`;
    menu.className = "storage-name-suggestions";
    menu.setAttribute("role", "listbox");
    menu.setAttribute("aria-label", "Previously used storage names");
    menu.hidden = true;
    document.body.append(menu);
    input.setAttribute("aria-controls", menu.id);
    menu.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        hideMenu(input, menu);
        input.focus();
      }
    });

    const show = async () => {
      try {
        await loadSuggestions();
        if (document.activeElement !== input) return;
        updateMenu(input, menu);
      } catch {
        if (document.activeElement === input) updateMenu(input, menu, true);
      }
    };

    input.addEventListener("focus", show);
    input.addEventListener("input", () => {
      if (!menu.hidden && suggestionsByType[typeFor(input)]) updateMenu(input, menu);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        hideMenu(input, menu);
        return;
      }
      if (menu.hidden || !["ArrowDown", "ArrowUp", "Enter"].includes(event.key)) return;
      const options = [...menu.querySelectorAll(".storage-name-suggestions__option")];
      if (!options.length) return;
      const activeIndex = options.findIndex((option) => option.id === input.getAttribute("aria-activedescendant"));
      if (event.key === "Enter" && activeIndex >= 0) {
        event.preventDefault();
        options[activeIndex].click();
        return;
      }
      event.preventDefault();
      const nextIndex = event.key === "ArrowUp"
        ? (activeIndex <= 0 ? options.length - 1 : activeIndex - 1)
        : (activeIndex + 1) % options.length;
      options.forEach((option, index) => option.setAttribute("aria-selected", index === nextIndex ? "true" : "false"));
      input.setAttribute("aria-activedescendant", options[nextIndex].id);
    });
    input.addEventListener("focusout", () => {
      requestAnimationFrame(() => {
        if (
          !input.parentElement.contains(document.activeElement)
          && !menu.contains(document.activeElement)
        ) hideMenu(input, menu);
      });
    });
  };

  const scan = (root = document) => {
    if (root.matches?.(inputSelector)) attach(root);
    root.querySelectorAll?.(inputSelector).forEach(attach);
  };

  scan();
  new MutationObserver((records) => {
    records.forEach((record) => record.addedNodes.forEach((node) => {
      if (node.nodeType === Node.ELEMENT_NODE) scan(node);
    }));
  }).observe(document.body, { childList: true, subtree: true });

  window.addEventListener("scroll", () => {
    if (openMenu) positionMenu(openMenu.input, openMenu.menu);
  }, true);
  window.addEventListener("resize", () => {
    if (openMenu) positionMenu(openMenu.input, openMenu.menu);
  });
})();
