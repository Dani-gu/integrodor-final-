(() => {
  const button = document.getElementById("theme-toggle");
  const icon = document.getElementById("theme-toggle-icon");
  if (!button || !icon) return;

  const updateControl = () => {
    const isDark = document.documentElement.dataset.theme === "dark";
    icon.textContent = isDark ? "☀" : "☾";
    button.setAttribute("aria-label", isDark ? "Cambiar a tema claro" : "Cambiar a tema oscuro");
    button.setAttribute("aria-pressed", String(isDark));
    button.title = isDark ? "Cambiar a tema claro" : "Cambiar a tema oscuro";
  };

  updateControl();
  button.addEventListener("click", () => {
    const isDark = document.documentElement.dataset.theme !== "dark";
    document.documentElement.dataset.theme = isDark ? "dark" : "light";
    try {
      localStorage.setItem("matriplast-theme", isDark ? "dark" : "light");
    } catch (error) {
      // The theme still changes for this page if browser storage is unavailable.
    }
    updateControl();
  });
})();
