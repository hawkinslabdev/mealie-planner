(function () {
  const btn = document.getElementById("logout-btn");
  const modal = document.getElementById("logout-modal");
  const confirmBtn = document.getElementById("logout-confirm");
  const cancelBtn = document.getElementById("logout-cancel");
  const backdrop = document.getElementById("logout-backdrop");

  if (!btn || !modal || !confirmBtn || !cancelBtn) return;

  function show() {
    modal.hidden = false;
    document.addEventListener("keydown", onKeydown);
  }

  function hide() {
    modal.hidden = true;
    document.removeEventListener("keydown", onKeydown);
  }

  function onKeydown(e) {
    if (e.key === "Escape") hide();
  }

  btn.addEventListener("click", show);

  cancelBtn.addEventListener("click", hide);
  if (backdrop) backdrop.addEventListener("click", hide);

  confirmBtn.addEventListener("click", async function () {
    confirmBtn.disabled = true;
    try {
      const res = await fetch(
        (window.INGRESS_PATH || "") + "/api/auth/logout",
        { method: "POST" }
      );
      if (!res.ok) throw new Error();
      window.location.href = (window.INGRESS_PATH || "") + "/auth";
    } catch {
      alert("Logout failed. Try again.");
      confirmBtn.disabled = false;
    }
  });
})();
