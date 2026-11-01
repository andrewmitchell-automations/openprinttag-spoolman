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
