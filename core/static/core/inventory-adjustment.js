(() => {
  const dataNode = document.getElementById("inventory-product-stock-data");
  const preview = document.getElementById("inventory-adjust-preview");
  if (!dataNode || !preview) return;

  const stocks = JSON.parse(dataNode.textContent || "{}");
  const productField = document.getElementById("id_product");
  const directionField = document.getElementById("id_direction");
  const quantityField = document.getElementById("id_quantity");
  const currentOutput = preview.querySelector("[data-current-stock]");
  const signOutput = preview.querySelector("[data-adjust-sign]");
  const resultOutput = preview.querySelector("[data-result-stock]");
  const messageOutput = preview.querySelector("[data-adjust-message]");
  const formatter = new Intl.NumberFormat("es-BO", { maximumFractionDigits: 3 });

  function updatePreview() {
    const selected = stocks[productField.value];
    preview.classList.remove("has-error");
    if (!selected) {
      currentOutput.textContent = "Selecciona un producto";
      signOutput.textContent = "—";
      resultOutput.textContent = "Ingresa la cantidad";
      messageOutput.textContent = "El cálculo es una vista previa; el movimiento se registra al guardar.";
      return;
    }

    const current = Number(selected.stock);
    const quantity = Number((quantityField.value || "").replace(",", "."));
    const unit = selected.unit;
    currentOutput.textContent = `${formatter.format(current)} ${unit}`;
    if (!Number.isFinite(quantity) || quantity <= 0) {
      signOutput.textContent = directionField.value === "OUT" ? "−" : "+";
      resultOutput.textContent = "Ingresa la cantidad";
      messageOutput.textContent = "El cálculo es una vista previa; el movimiento se registra al guardar.";
      return;
    }

    const isReduction = directionField.value === "OUT";
    const result = current + (isReduction ? -quantity : quantity);
    signOutput.textContent = `${isReduction ? "−" : "+"} ${formatter.format(quantity)} ${unit}`;
    resultOutput.textContent = `${formatter.format(Math.max(0, result))} ${unit}`;
    if (result < 0) {
      preview.classList.add("has-error");
      messageOutput.textContent = "La cantidad que reduces supera la existencia actual.";
    } else {
      messageOutput.textContent = "El cálculo es una vista previa; el movimiento se registra al guardar.";
    }
  }

  [productField, directionField, quantityField].forEach((field) => {
    field.addEventListener("input", updatePreview);
    field.addEventListener("change", updatePreview);
  });
  updatePreview();
})();
