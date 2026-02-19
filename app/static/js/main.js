document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-add-row]").forEach(btn => {
    btn.addEventListener("click", function () {
      const targetId = this.getAttribute("data-add-row");
      const tbody = document.getElementById(targetId);
      if (!tbody) return;
      const lastRow = tbody.querySelector("tr:last-child");
      if (!lastRow) return;
      const clone = lastRow.cloneNode(true);
      clone.querySelectorAll("input, select").forEach(input => {
        input.value = "";
      });
      tbody.appendChild(clone);
    });
  });
});
