(() => {
  const rows = document.getElementById("consumption-rows");
  const addButton = document.getElementById("add-consumption");
  const totalForms = document.getElementById("id_consumptions-TOTAL_FORMS");
  const template = document.getElementById("empty-consumption-row");
  const producedQuantity = document.getElementById("id_produced_quantity");
  const overrunWarning = document.getElementById("overproduction-warning");
  const overrunConfirmation = document.getElementById("id_confirm_overproduction");
  const finishForm = document.getElementById("finish-lot-form");
  const plannedQuantity = Number(finishForm?.dataset.plannedQuantity || 0);
  const updateOverproductionWarning = () => {
    if (!producedQuantity || !overrunWarning) return;
    const produced = Number(producedQuantity.value);
    const exceedsPlan = Number.isFinite(produced) && produced > plannedQuantity;
    overrunWarning.hidden = !exceedsPlan;
    if (!exceedsPlan && overrunConfirmation) overrunConfirmation.checked = false;
  };
  updateOverproductionWarning();
  producedQuantity?.addEventListener("input", updateOverproductionWarning);
  if (!rows || !addButton || !totalForms || !template) return;

  const recipeElement = document.getElementById("production-recipe-rates");
  const recipeRates = recipeElement ? JSON.parse(recipeElement.textContent) : {};
  const stockElement = document.getElementById("production-material-stock");
  const materialStock = stockElement ? JSON.parse(stockElement.textContent) : {};
  const calculateRecipeQuantity = (row, preserveManual = true) => {
    const material = row.querySelector('select[name$="-material"]');
    const quantity = row.querySelector('input[name$="-quantity"]');
    if (!material || !quantity) return;
    const rate = recipeRates[material.value];
    if (!rate) {
      if (quantity.dataset.recipeAuto === "1") {
        quantity.value = "";
        delete quantity.dataset.recipeAuto;
      }
      return;
    }
    if (preserveManual && quantity.value && quantity.dataset.recipeAuto !== "1") return;
    const output = Number(producedQuantity?.value || 0);
    if (!Number.isFinite(output) || output <= 0) {
      if (quantity.dataset.recipeAuto === "1") quantity.value = "";
      return;
    }
    quantity.value = (Number(rate) * output).toFixed(3);
    quantity.dataset.recipeAuto = "1";
  };

  const recalculateRecipes = () => {
    rows.querySelectorAll(".consumption-row:not([hidden])").forEach((row) => calculateRecipeQuantity(row));
    updateStockHints();
  };

  const updateStockHints = () => {
    const activeRows = [...rows.querySelectorAll(".consumption-row:not([hidden])")];
    const totals = new Map();
    activeRows.forEach((row) => {
      const material = row.querySelector('select[name$="-material"]')?.value;
      const quantity = Number(row.querySelector('input[name$="-quantity"]')?.value || 0);
      if (material && Number.isFinite(quantity)) totals.set(material, (totals.get(material) || 0) + quantity);
    });
    activeRows.forEach((row) => {
      const material = row.querySelector('select[name$="-material"]')?.value;
      const hint = row.querySelector("[data-stock-hint]");
      const warning = row.querySelector("[data-stock-warning]");
      const info = materialStock[material];
      if (!info) {
        if (hint) hint.textContent = "";
        if (warning) { warning.textContent = ""; warning.hidden = true; }
        return;
      }
      const available = Number(info.stock);
      const requested = totals.get(material) || 0;
      if (hint) hint.textContent = `Disponible: ${info.stock} ${info.unit}`;
      if (warning) {
        const shortfall = requested - available;
        warning.textContent = shortfall > 0.0000001
          ? `Stock insuficiente: faltan ${shortfall.toFixed(3)} ${info.unit} (total solicitado: ${requested.toFixed(3)} ${info.unit}).`
          : "";
        warning.hidden = shortfall <= 0.0000001;
      }
    });
  };

  rows.querySelectorAll(".consumption-row").forEach((row) => {
    const deleteField = row.querySelector('input[type="checkbox"][name$="-DELETE"]');
    if (deleteField?.checked) row.hidden = true;
  });

  rows.querySelectorAll(".consumption-row").forEach((row) => calculateRecipeQuantity(row));
  updateStockHints();
  producedQuantity?.addEventListener("input", recalculateRecipes);
  rows.addEventListener("change", (event) => {
    if (event.target.matches('select[name$="-material"]')) calculateRecipeQuantity(event.target.closest(".consumption-row"));
    updateStockHints();
  });
  rows.addEventListener("input", (event) => {
    if (event.target.matches('input[name$="-quantity"]')) delete event.target.dataset.recipeAuto;
    updateStockHints();
  });

  addButton.addEventListener("click", () => {
    const index = Number(totalForms.value);
    const html = template.innerHTML.replaceAll("__prefix__", String(index));
    rows.insertAdjacentHTML("beforeend", html);
    totalForms.value = String(index + 1);
    const row = rows.querySelector(`#id_consumptions-${index}-material`)?.closest(".consumption-row");
    if (row) calculateRecipeQuantity(row);
    updateStockHints();
    rows.querySelector(`#id_consumptions-${index}-material`)?.focus();
  });

  rows.addEventListener("click", (event) => {
    const button = event.target.closest(".remove-line");
    if (!button) return;
    const row = button.closest(".consumption-row");
    const deleteField = row?.querySelector('input[type="checkbox"][name$="-DELETE"]');
    if (row && deleteField) {
      deleteField.checked = true;
      row.hidden = true;
      updateStockHints();
    }
  });
})();
