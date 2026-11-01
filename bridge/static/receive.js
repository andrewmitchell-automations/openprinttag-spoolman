// The login arrives in the URL fragment, which browsers never send to servers or put in logs.
(async () => {
  const status = document.getElementById("status");
  const done = document.getElementById("done");
  const login = window.location.hash.slice(1);
  history.replaceState(null, "", window.location.pathname);
  if (!login) {
    status.textContent = "No login was received. Go back to the setup page and try again.";
    return;
  }
  try {
    const response = await fetch(window.location.pathname, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ login }),
    });
    const result = await response.json();
    status.textContent = result.message;
    if (result.ok) done.hidden = false;
  } catch {
    status.textContent = "The bridge did not answer. Check that it is running and try again.";
  }
})();
