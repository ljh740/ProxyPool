<script>
(function(){
  var selectAll = document.getElementById('select-all');
  var toolbar = document.getElementById('batch-toolbar');
  var countSpan = document.getElementById('selected-count');
  if (!selectAll) return;

  function getVisibleChecks() {
    var checks = document.querySelectorAll('.row-check');
    var visible = [];
    for (var i = 0; i < checks.length; i++) {
      if (checks[i].closest('tr').style.display !== 'none') visible.push(checks[i]);
    }
    return visible;
  }
  function syncRowStates() {
    var checks = document.querySelectorAll('.row-check');
    for (var i = 0; i < checks.length; i++) {
      var row = checks[i].closest('tr');
      if (row) row.classList.toggle('pp-selected-row', !!checks[i].checked);
    }
  }
  function updateToolbar() {
    syncRowStates();
    var checks = getVisibleChecks();
    var checked = 0;
    for (var i = 0; i < checks.length; i++) { if (checks[i].checked) checked++; }
    if (countSpan) countSpan.textContent = checked;
    if (toolbar) toolbar.style.display = checked > 0 ? '' : 'none';
    if (selectAll) selectAll.checked = checks.length > 0 && checked === checks.length;
  }
  selectAll.addEventListener('change', function() {
    var checks = getVisibleChecks();
    for (var i = 0; i < checks.length; i++) checks[i].checked = selectAll.checked;
    updateToolbar();
  });
  document.addEventListener('change', function(e) {
    if (e.target.classList.contains('row-check')) updateToolbar();
  });

  // Expose helpers globally for onclick handlers
  window.invertSelection = function() {
    var checks = getVisibleChecks();
    for (var i = 0; i < checks.length; i++) checks[i].checked = !checks[i].checked;
    updateToolbar();
  };
  function getSelectedKeys() {
    var checks = document.querySelectorAll('.row-check:checked');
    var keys = [];
    for (var i = 0; i < checks.length; i++) keys.push(checks[i].value);
    return keys;
  }
  function submitBatch(action) {
    var keys = getSelectedKeys();
    if (!keys.length) return;
    var form = document.getElementById('batch-form');
    var container = document.getElementById('batch-keys');
    form.action = action;
    container.innerHTML = '';
    for (var i = 0; i < keys.length; i++) {
      var inp = document.createElement('input');
      inp.type = 'hidden'; inp.name = 'keys'; inp.value = keys[i];
      container.appendChild(inp);
    }
    form.submit();
  }
  window.batchDelete = function() {
    var keys = getSelectedKeys();
    if (!keys.length) return;
    var tpl = document.getElementById('i18n-delete-selected-confirm');
    var msg = tpl ? tpl.textContent.replace('{count}', keys.length) : ('Delete ' + keys.length + ' selected proxies?');
    if (!confirm(msg)) return;
    submitBatch('/dashboard/proxies/batch/delete');
  };
  window.batchTogglePool = function(state) {
    var keys = getSelectedKeys();
    if (!keys.length) return;
    var form = document.getElementById('batch-form');
    var container = document.getElementById('batch-keys');
    form.action = '/dashboard/proxies/batch/toggle-pool';
    container.innerHTML = '';
    for (var i = 0; i < keys.length; i++) {
      var inp = document.createElement('input');
      inp.type = 'hidden'; inp.name = 'keys'; inp.value = keys[i];
      container.appendChild(inp);
    }
    var stateInput = document.createElement('input');
    stateInput.type = 'hidden'; stateInput.name = 'pool_state'; stateInput.value = state;
    container.appendChild(stateInput);
    form.submit();
  };

  function applyPoolButtonState(button, isOn) {
    if (!button) return;
    button.dataset.state = isOn ? 'on' : 'off';
    button.textContent = isOn ? button.dataset.onLabel : button.dataset.offLabel;
    button.style.border = 'none';
    button.style.fontWeight = '600';
    button.style.fontSize = '0.75rem';
    if (isOn) {
      button.style.background = 'var(--pp-success-subtle)';
      button.style.color = 'var(--pp-success)';
    } else {
      button.style.background = 'var(--pp-surface-hover)';
      button.style.color = 'var(--pp-text-muted)';
    }
  }

  async function togglePoolAsync(form, button) {
    var response = await fetch(form.action, {
      method: 'POST',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      body: new URLSearchParams(new FormData(form))
    });
    var payload = await response.json();
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || '');
    }
    applyPoolButtonState(button, !!(payload.entry && payload.entry.in_random_pool));
    if (payload.message) showToast(payload.message, 'success');
  }

  document.addEventListener('submit', function(e) {
    var form = e.target;
    if (!form.classList || !form.classList.contains('pp-toggle-pool-form')) return;
    if (!window.fetch) return;
    e.preventDefault();
    var button = form.querySelector('.pp-pool-toggle-btn');
    if (!button || button.disabled) return;
    button.disabled = true;
    togglePoolAsync(form, button).catch(function(error) {
      var fallback = document.getElementById('i18n-pool-toggle-error');
      showToast((error && error.message) || (fallback ? fallback.textContent : 'Request failed'), 'error', 6000);
    }).finally(function() {
      button.disabled = false;
    });
  });
})();
</script>
