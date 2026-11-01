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
