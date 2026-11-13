(() => {
  const productField = document.getElementById("id_product");
  const quantityField = document.getElementById("id_planned_quantity");
  const machineField = document.getElementById("id_machine");
  const message = document.getElementById("production-plan-message");
  const materialList = document.getElementById("production-plan-materials");
  const capacity = document.getElementById("production-plan-capacity");
  const planActions = document.getElementById("production-plan-actions");
  const recipeLink = document.getElementById("production-plan-recipe-link");
  const machineMaterials = document.getElementById("production-machine-materials");
  const recipeDataElement = document.getElementById("production-plan-data");
  const machineDataElement = document.getElementById("production-machine-data");
  if (!productField || !quantityField || !message || !materialList) return;

  const recipes = recipeDataElement ? JSON.parse(recipeDataElement.textContent) : {};
  const machines = machineDataElement ? JSON.parse(machineDataElement.textContent) : {};
  const formatQuantity = (value) => new Intl.NumberFormat("es-BO", {
    maximumFractionDigits: 3,
  }).format(value);

  const renderPreview = () => {
    materialList.replaceChildren();
    if (capacity) capacity.hidden = true;
    if (planActions) planActions.hidden = true;
    if (machineMaterials) machineMaterials.hidden = true;
    message.classList.remove("has-shortage", "has-availability");

    const recipe = recipes[productField.value];
    const plannedQuantity = Number(quantityField.value);
    if (!recipe || !Number.isFinite(plannedQuantity) || plannedQuantity <= 0) {
      message.textContent = "Selecciona un producto e indica la cantidad planificada.";
      if (productField.value && !recipe) showRecipeAction();
      renderMachineMaterials();
      return;
    }

    if (!recipe.materials.length) {
      message.textContent = "Este producto no tiene receta configurada; no se puede estimar el consumo de materias primas.";
      showRecipeAction();
      renderMachineMaterials();
      return;
    }

    let maximumCapacity = Infinity;
    let hasShortage = false;
    recipe.materials.forEach((material) => {
      const rate = Number(material.rate);
      const stock = Number(material.stock);
      const required = rate * plannedQuantity;
      const remaining = stock - required;
      const materialCapacity = rate > 0 ? stock / rate : 0;
      maximumCapacity = Math.min(maximumCapacity, materialCapacity);
      const insufficient = remaining < -0.0000001;
      hasShortage ||= insufficient;

      const item = document.createElement("li");
      item.className = insufficient ? "is-insufficient" : "is-available";
      const details = document.createElement("span");
      details.textContent = `${material.name}: requiere ${formatQuantity(required)} ${material.unit}; disponible ${formatQuantity(stock)} ${material.unit}`;
      const status = document.createElement("strong");
      status.textContent = insufficient
        ? `Faltan ${formatQuantity(Math.abs(remaining))} ${material.unit}`
        : `Quedarían ${formatQuantity(Math.max(0, remaining))} ${material.unit}`;
      item.append(details, status);
      materialList.append(item);
    });

    message.textContent = hasShortage
      ? "El stock actual no alcanza para completar este plan según la receta. Puedes guardar el lote, pero deberás revisar el abastecimiento antes de producir."
      : "El stock actual alcanza para cubrir los materiales de la receta.";
    message.classList.toggle("has-shortage", hasShortage);
    message.classList.toggle("has-availability", !hasShortage);
    if (capacity && Number.isFinite(maximumCapacity)) {
      capacity.hidden = false;
      capacity.textContent = `Con el stock actual, el material más limitante permitiría producir aproximadamente ${formatQuantity(maximumCapacity)} ${recipe.unit}.`;
    }
    renderMachineMaterials();
  };

  const showRecipeAction = () => {
    if (!planActions || !recipeLink || !productField.value) return;
    const template = planActions.dataset.recipeUrlTemplate || "";
    recipeLink.href = template.replace("/0/", `/${encodeURIComponent(productField.value)}/`);
    planActions.hidden = false;
  };

  const renderMachineMaterials = () => {
    if (!machineMaterials) return;
    const machine = machines[machineField?.value];
    if (!machine) {
      machineMaterials.hidden = true;
      machineMaterials.replaceChildren();
      return;
    }
    machineMaterials.hidden = false;
    const heading = document.createElement("strong");
    heading.textContent = `Materiales habituales de ${machine.name}`;
    const list = document.createElement("span");
    if (machine.materials.length) {
      list.textContent = machine.materials.join(", ");
      machineMaterials.replaceChildren(heading, list);
    } else {
      list.textContent = "Esta máquina no tiene materiales habituales asignados.";
      const link = document.createElement("a");
      link.className = "button secondary small";
      link.href = machineMaterials.dataset.machineListUrl || "/produccion/maquinas/";
      link.textContent = "Configurar materiales de máquinas";
      machineMaterials.replaceChildren(heading, list, link);
    }
  };

  productField.addEventListener("change", renderPreview);
  quantityField.addEventListener("input", renderPreview);
  machineField?.addEventListener("change", renderMachineMaterials);
  renderPreview();
})();
