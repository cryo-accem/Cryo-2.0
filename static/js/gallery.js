// Photo lightbox for [data-lightbox-gallery] containers (events, community gallery).
// Progressive enhancement: without JS the photos and their links behave as before.
// - Items whose photo is NOT inside a link: the image itself becomes a button.
// - Items whose photo IS inside a link (event cards): the link is left alone and a
//   separate, server-rendered "Enlarge" button ([data-lightbox-open]) is revealed.
(function() {
  const galleries = Array.from(document.querySelectorAll("[data-lightbox-gallery]"));
  if (!galleries.length || typeof HTMLDialogElement !== "function") return;

  const largestSource = (img) => {
    let best = { url: img.currentSrc || img.src, width: 0 };
    (img.getAttribute("srcset") || "").split(",").forEach((candidate) => {
      const [url, descriptor] = candidate.trim().split(/\s+/);
      const width = parseInt(descriptor, 10);
      if (url && width > best.width) best = { url, width };
    });
    return best.url;
  };

  const captionFor = (item, img) => {
    const caption = item.querySelector("figcaption, h3");
    return (caption && caption.textContent.trim()) || img.alt || "";
  };

  // Build one dialog for the page.
  const dialog = document.createElement("dialog");
  dialog.className = "lightbox";
  dialog.setAttribute("aria-label", "Photo viewer");
  dialog.innerHTML = `
    <div class="lightbox__frame">
      <figure class="lightbox__figure">
        <img class="lightbox__image" alt="">
        <figcaption class="lightbox__caption"><span data-lightbox-caption></span> <span class="lightbox__counter" data-lightbox-counter></span></figcaption>
      </figure>
      <button class="lightbox__button lightbox__close" type="button" data-lightbox-close aria-label="Close photo viewer"><i class="fa-solid fa-xmark" aria-hidden="true"></i></button>
      <button class="lightbox__button lightbox__nav lightbox__nav--prev" type="button" data-lightbox-prev aria-label="Previous photo"><i class="fa-solid fa-chevron-down" aria-hidden="true"></i></button>
      <button class="lightbox__button lightbox__nav lightbox__nav--next" type="button" data-lightbox-next aria-label="Next photo"><i class="fa-solid fa-chevron-down" aria-hidden="true"></i></button>
    </div>
    <p class="sr-only" aria-live="polite" data-lightbox-status></p>`;
  document.body.appendChild(dialog);

  const image = dialog.querySelector(".lightbox__image");
  const captionEl = dialog.querySelector("[data-lightbox-caption]");
  const counterEl = dialog.querySelector("[data-lightbox-counter]");
  const statusEl = dialog.querySelector("[data-lightbox-status]");
  const prevButton = dialog.querySelector("[data-lightbox-prev]");
  const nextButton = dialog.querySelector("[data-lightbox-next]");
  let current = { photos: [], index: 0, opener: null };

  const show = (index) => {
    const { photos } = current;
    current.index = (index + photos.length) % photos.length;
    const photo = photos[current.index];
    image.src = photo.src;
    image.alt = photo.alt;
    captionEl.textContent = photo.caption;
    counterEl.textContent = photos.length > 1 ? `${current.index + 1} / ${photos.length}` : "";
    statusEl.textContent = `Photo ${current.index + 1} of ${photos.length}: ${photo.caption}`;
    const single = photos.length < 2;
    prevButton.hidden = single;
    nextButton.hidden = single;
  };

  const open = (photos, index, opener) => {
    current = { photos, index, opener };
    show(index);
    document.documentElement.classList.add("lightbox-open");
    dialog.showModal();
    dialog.querySelector("[data-lightbox-close]").focus();
  };

  dialog.addEventListener("close", () => {
    document.documentElement.classList.remove("lightbox-open");
    image.removeAttribute("src");
    if (current.opener) current.opener.focus();
  });
  dialog.querySelector("[data-lightbox-close]").addEventListener("click", () => dialog.close());
  prevButton.addEventListener("click", () => show(current.index - 1));
  nextButton.addEventListener("click", () => show(current.index + 1));
  // Clicking the dimmed backdrop (the dialog box itself, outside the frame) closes it.
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("keydown", (event) => {
    if (current.photos.length < 2) return;
    if (event.key === "ArrowLeft") { event.preventDefault(); show(current.index - 1); }
    if (event.key === "ArrowRight") { event.preventDefault(); show(current.index + 1); }
  });

  galleries.forEach((gallery) => {
    const items = Array.from(gallery.querySelectorAll("[data-lightbox-item]"))
      .map((item) => ({ item, img: item.querySelector("img") }))
      .filter(({ img }) => img);
    const photos = items.map(({ item, img }) => ({
      src: largestSource(img),
      alt: img.alt,
      caption: captionFor(item, img),
    }));

    items.forEach(({ item, img }, index) => {
      const existing = item.querySelector("[data-lightbox-open]");
      let trigger = existing;
      if (existing) {
        existing.hidden = false;
      } else if (!img.closest("a")) {
        trigger = document.createElement("button");
        trigger.type = "button";
        trigger.className = "gallery-trigger";
        trigger.setAttribute("aria-label", `Enlarge photo: ${photos[index].caption}`);
        img.replaceWith(trigger);
        trigger.appendChild(img);
      }
      if (trigger) trigger.addEventListener("click", () => open(photos, index, trigger));
    });
  });
})();
