// navigator.clipboard needs HTTPS, and this page usually runs on plain HTTP.
document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", () => {
    const source = document.getElementById(button.dataset.copy);
    const field = document.createElement("textarea");
    field.value = source.textContent;
    document.body.append(field);
    field.select();
    const copied = document.execCommand("copy");
    field.remove();
    button.textContent = copied ? "Copied" : "Select and copy it yourself";
  });
});

// Options are removed rather than hidden, because Safari ignores hidden on an option.
document.querySelectorAll("[data-spool-filter]").forEach((search) => {
  const select = search.nextElementSibling;
  const radio = search.closest("label").querySelector("input[type=radio]");
  const [placeholder, ...spools] = select.options;
  const chooseAnother = () => { radio.checked = true; };

  search.hidden = false;
  search.addEventListener("input", () => {
    const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
    const chosen = select.value;
    const matches = spools.filter((option) => {
      const text = option.dataset.search.toLowerCase();
      return words.every((word) => text.includes(word));
    });
    select.replaceChildren(placeholder, ...matches);
    select.value = words.length && matches.length === 1 ? matches[0].value : chosen;
    if (select.selectedIndex < 0) select.value = "";
    chooseAnother();
  });
  // Enter would otherwise submit the form before a spool is chosen.
  search.addEventListener("keydown", (event) => {
    if (event.key === "Enter") event.preventDefault();
  });
  select.addEventListener("change", chooseAnother);
});

// The warehouse page: search, filter and sort. Without JavaScript every spool is simply listed.
document.querySelectorAll("[data-inventory]").forEach((list) => {
  const rows = [...list.children];
  const search = document.querySelector("[data-inventory-search]");
  const sort = document.querySelector("[data-inventory-sort]");
  const segments = [...document.querySelectorAll("[data-show]")];
  const chips = [...document.querySelectorAll(".chip[data-material]")];
  const none = document.querySelector("[data-inventory-none]");
  let show = "";
  const materials = new Set();

  const orders = {
    where: () => 0,
    material: (a, b) => a.dataset.material.localeCompare(b.dataset.material) || a.dataset.name.localeCompare(b.dataset.name),
    least: (a, b) => left(a) - left(b),
    most: (a, b) => left(b) - left(a),
    name: (a, b) => a.dataset.name.localeCompare(b.dataset.name),
  };
  // Unknown weights go last either way.
  const left = (row) => (Number(row.dataset.left) < 0 ? Infinity : Number(row.dataset.left));

  const update = () => {
    const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    rows.forEach((row) => {
      const text = row.dataset.search.toLowerCase();
      const visible = (!show || row.dataset.state.split(" ").includes(show))
        && (!materials.size || materials.has(row.dataset.material))
        && words.every((word) => text.includes(word));
      row.hidden = !visible;
      if (visible) shown += 1;
    });
    none.hidden = shown > 0;
  };

  document.querySelectorAll("[data-inventory-tools]").forEach((tools) => { tools.hidden = false; });
  search.addEventListener("input", update);
  segments.forEach((button) => button.addEventListener("click", () => {
    show = button.dataset.show;
    segments.forEach((other) => other.setAttribute("aria-pressed", String(other === button)));
    update();
  }));
  chips.forEach((chip) => chip.addEventListener("click", () => {
    const on = chip.getAttribute("aria-pressed") !== "true";
    chip.setAttribute("aria-pressed", String(on));
    if (on) materials.add(chip.dataset.material); else materials.delete(chip.dataset.material);
    update();
  }));
  sort.addEventListener("change", () => {
    const order = orders[sort.value] || orders.where;
    // The server's order is "where", so a stable sort on rows keeps it as the tie breaker.
    list.replaceChildren(...[...rows].sort(order));
  });
});
