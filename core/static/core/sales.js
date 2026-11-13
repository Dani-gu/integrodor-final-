(() => {
  const rows = document.getElementById("sale-lines");
  const addButton = document.getElementById("add-sale-line");
  const totalForms = document.getElementById("id_lines-TOTAL_FORMS");
  const template = document.getElementById("empty-sale-line");
  if (!rows || !addButton || !totalForms || !template) return;
  const priceNode = document.getElementById("sale-product-prices");
  const prices = priceNode ? JSON.parse(priceNode.textContent) : {};
  const totalNode = document.getElementById("sale-estimated-total");
  const paymentNode = document.getElementById("sale-estimated-payment");
  const balanceNode = document.getElementById("sale-estimated-balance");
  const linesNode = document.getElementById("sale-estimated-lines");
  const paymentWarning = document.getElementById("sale-payment-warning");
  const paymentInput = document.getElementById("id_initial_payment");
  const paymentMethod = document.getElementById("id_payment_method");
  const emptyError = document.getElementById("sale-lines-error");
  const saleForm = document.getElementById("sale-form");
  const money = new Intl.NumberFormat("es-BO", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  const applyProductPrice = (select) => {
    const match = select.id.match(/lines-(\d+)-product/);
    if (!match) return;
    const priceInput = document.getElementById(`id_lines-${match[1]}-unit_price`);
    const price = prices[select.value];
    if (priceInput && price !== undefined) priceInput.value = price;
    updateEstimate();
  };

  const updateEstimate = () => {
    let total = 0;
    let count = 0;
    rows.querySelectorAll(".sale-line").forEach((row) => {
      if (row.hidden) return;
      const product = row.querySelector('select[id$="-product"]');
      const quantity = row.querySelector('input[id$="-quantity"]');
      const price = row.querySelector('input[id$="-unit_price"]');
      const subtotal = row.querySelector(".line-subtotal");
      const quantityValue = Number(quantity?.value || 0);
      const priceValue = Number(price?.value || 0);
      const lineTotal = quantityValue > 0 && priceValue >= 0 ? quantityValue * priceValue : 0;
      if (subtotal) subtotal.textContent = `Bs ${money.format(lineTotal)}`;
      if (product?.value && quantityValue > 0) {
        total += lineTotal;
        count += 1;
      }
    });
    const initialPayment = Math.max(0, Number(paymentInput?.value || 0));
    const remainingBalance = Math.max(0, total - initialPayment);
    if (totalNode) totalNode.textContent = `Bs ${money.format(total)}`;
    if (paymentNode) paymentNode.textContent = `Bs ${money.format(initialPayment)}`;
    if (balanceNode) balanceNode.textContent = `Bs ${money.format(remainingBalance)}`;
    if (linesNode) linesNode.textContent = `${count} producto${count === 1 ? "" : "s"}`;
    if (paymentWarning) {
      const warning = initialPayment > total
        ? "El pago inicial supera el total estimado."
        : initialPayment > 0 && !paymentMethod?.value
          ? "Selecciona la forma de pago del pago inicial."
          : "";
      paymentWarning.textContent = warning;
      paymentWarning.hidden = !warning;
    }
    if (emptyError && count > 0) emptyError.hidden = true;
  };

  rows.addEventListener("change", (event) => {
    if (event.target.matches('select[id$="-product"]')) applyProductPrice(event.target);
  });
  rows.addEventListener("input", updateEstimate);
  rows.addEventListener("change", updateEstimate);
  paymentInput?.addEventListener("input", updateEstimate);
  paymentMethod?.addEventListener("change", updateEstimate);

  addButton.addEventListener("click", () => {
    const index = Number(totalForms.value);
    const html = template.innerHTML.replaceAll("__prefix__", String(index));
    rows.insertAdjacentHTML("beforeend", html);
    totalForms.value = String(index + 1);
    rows.querySelector(`select[id="id_lines-${index}-product"]`)?.focus();
  });

  rows.addEventListener("click", (event) => {
    const button = event.target.closest(".remove-line");
    if (!button) return;
    const row = button.closest(".sale-line");
    if (!row) return;
    const deleteField = row.querySelector('input[type="checkbox"][name$="-DELETE"]');
    if (deleteField) deleteField.checked = true;
    row.hidden = true;
    updateEstimate();
  });

  if (saleForm) {
    saleForm.addEventListener("submit", (event) => {
      const selectedProducts = [...rows.querySelectorAll('.sale-line:not([hidden]) select[id$="-product"]')]
        .filter((select) => select.value);
      if (selectedProducts.length === 0) {
        event.preventDefault();
        if (emptyError) {
          emptyError.hidden = false;
          emptyError.scrollIntoView({ behavior: "smooth", block: "center" });
        }
        addButton.focus();
      }
    });
  }

  rows.querySelectorAll(".sale-line").forEach((row) => {
    const deleteField = row.querySelector('input[type="checkbox"][name$="-DELETE"]');
    if (deleteField?.checked) row.hidden = true;
  });
  updateEstimate();
})();
