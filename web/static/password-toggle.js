// Adds a Show/Hide button to every password field so the password can be checked while typing.
(function () {
  function addToggle(input) {
    if (input.dataset.toggleReady) { return; }
    input.dataset.toggleReady = "1";
    var wrap = document.createElement("div");
    wrap.className = "pw-wrap";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pw-toggle";
    btn.textContent = "Show";
    btn.setAttribute("aria-label", "Show password");
    btn.setAttribute("aria-pressed", "false");
    btn.addEventListener("click", function () {
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.textContent = show ? "Hide" : "Show";
      btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
      btn.setAttribute("aria-pressed", show ? "true" : "false");
    });
    wrap.appendChild(btn);
  }
  document.querySelectorAll('input[type="password"]').forEach(addToggle);
})();
