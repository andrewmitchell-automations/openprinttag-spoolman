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

// The warehouse page: search, filter, sort and open link forms. Without JavaScript every spool and form is listed.
document.querySelectorAll("[data-inventory]").forEach((body) => {
  // Each spool row, with the link form row that follows it, if any.
  const pairs = [...body.querySelectorAll(":scope > .inv-row")].map((row) => {
    const next = row.nextElementSibling;
    return { row, form: next && next.matches("[data-form-for]") ? next : null };
  });
  const search = document.querySelector("[data-inventory-search]");
  const segments = [...document.querySelectorAll("[data-show]")];
  const chips = [...document.querySelectorAll(".chip[data-material]")];
  const heads = [...document.querySelectorAll(".sort-head")];
  const none = document.querySelector("[data-inventory-none]");
  const materials = new Set();
  const open = new Set();
  let show = "";
  let sortKey = "";
  let ascending = true;

  // Unknown weights are -1 and go last either way.
  const number = (value) => (Number(value) < 0 ? null : Number(value));
  const compare = (a, b) => {
    const x = a.row.dataset[sortKey];
    const y = b.row.dataset[sortKey];
    if (["id", "left", "used", "spoolman"].includes(sortKey)) {
      const [m, n] = [number(x), number(y)];
      if (m === null || n === null) return (m === null) - (n === null);
      return ascending ? m - n : n - m;
    }
    return ascending ? x.localeCompare(y) : y.localeCompare(x);
  };

  const update = () => {
    const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    pairs.forEach(({ row, form }) => {
      const text = row.dataset.search.toLowerCase();
      const visible = (!show || row.dataset.state.split(" ").includes(show))
        && (!materials.size || materials.has(row.dataset.material))
        && words.every((word) => text.includes(word));
      row.hidden = !visible;
      if (form) form.hidden = !visible || !open.has(form.id);
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
  heads.forEach((head) => head.addEventListener("click", () => {
    ascending = sortKey === head.dataset.sort ? !ascending : true;
    sortKey = head.dataset.sort;
    heads.forEach((other) => {
      const th = other.closest("th");
      if (other === head) th.setAttribute("aria-sort", ascending ? "ascending" : "descending");
      else th.removeAttribute("aria-sort");
    });
    body.replaceChildren(...[...pairs].sort(compare).flatMap(({ row, form }) => (form ? [row, form] : [row])));
  }));
  body.querySelectorAll("[data-link-toggle]").forEach((toggle) => {
    toggle.addEventListener("click", (event) => {
      event.preventDefault();
      const id = toggle.dataset.linkToggle;
      if (open.has(id)) open.delete(id); else open.add(id);
      toggle.setAttribute("aria-expanded", String(open.has(id)));
      update();
    });
  });
  update();
});
