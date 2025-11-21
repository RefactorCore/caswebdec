// Decimal helpers to normalize numeric inputs and format money
(function () {
  // Format number as PHP currency (string)
  window.formatMoney = function (num) {
    const n = Number(num) || 0;
    return new Intl.NumberFormat('en-PH', { style: 'currency', currency: 'PHP' }).format(n);
  };

  // Parse a numeric input value safely (strip commas)
  window.parseInputNumber = function (el) {
    if (!el) return 0;
    let v = el.value;
    if (typeof v === 'string') {
      v = v.replace(/,/g, '').trim();
      if (v === '') return 0;
    }
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  };

  // Normalize a form: set number inputs with step containing decimals to fixed 2 dp strings,
  // and integer quantity inputs to integer strings. Call before submit.
  window.normalizeDecimalForm = function (formEl) {
    if (!formEl) return;
    // number inputs for currency (step includes ".01")
    formEl.querySelectorAll('input[type="number"]').forEach(input => {
      const step = input.getAttribute('step') || '';
      const isDecimal = step.includes('0.01') || step.includes('0.001') || step.includes('.');
      if (isDecimal) {
        const n = window.parseInputNumber(input);
        if (n > 0) {
          input.value = n.toFixed(2);
        } else {
          input.value = '';
        }
      } else {
        // integer inputs (quantities)
        const n = Math.trunc(window.parseInputNumber(input));
        if (n > 0) input.value = String(n);
        else input.value = '';
      }
    });
  };

  // Auto-attach normalization to forms with data-normalize-decimals attribute
  document.addEventListener('submit', function (ev) {
    const form = ev.target;
    if (form && form.dataset && form.dataset.normalizeDecimals !== undefined) {
      window.normalizeDecimalForm(form);
    }
  });
})();