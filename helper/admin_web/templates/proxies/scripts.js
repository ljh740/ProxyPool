<script>
(function() {
  var elements = {
    selectAll: document.getElementById('select-all'),
    toolbar: document.getElementById('batch-toolbar'),
    countSpan: document.getElementById('selected-count'),
    compatStartPort: document.getElementById('compat-start-port'),
    batchForm: document.getElementById('batch-form'),
    batchKeys: document.getElementById('batch-keys'),
    csrfInput: document.querySelector('#batch-form input[name="csrf_token"]'),
    detectMissingBtn: document.getElementById('detect-missing-country-btn'),
    detectI18n: document.getElementById('proxy-country-detect-i18n'),
    deleteConfirm: document.getElementById('i18n-delete-selected-confirm'),
    poolToggleError: document.getElementById('i18n-pool-toggle-error')
  };
  var state = {
    detectPollingToken: 0,
    detectInFlight: false
  };

  if (!elements.batchForm || !elements.batchKeys) {
    return;
  }

  function getVisibleChecks() {
    var checks = document.querySelectorAll('.row-check');
    var visible = [];
    for (var i = 0; i < checks.length; i++) {
      var row = checks[i].closest('tr');
      if (row && row.style.display !== 'none') {
        visible.push(checks[i]);
      }
    }
    return visible;
  }

  function getSelectedKeys() {
    var checks = document.querySelectorAll('.row-check:checked');
    var keys = [];
    for (var i = 0; i < checks.length; i++) {
      keys.push(checks[i].value);
    }
    return keys;
  }

  function syncRowStates() {
    var checks = document.querySelectorAll('.row-check');
    for (var i = 0; i < checks.length; i++) {
      var row = checks[i].closest('tr');
      if (row) {
        row.classList.toggle('pp-selected-row', !!checks[i].checked);
      }
    }
  }

  function updateToolbar() {
    var checks = getVisibleChecks();
    var checked = 0;

    syncRowStates();
    for (var i = 0; i < checks.length; i++) {
      if (checks[i].checked) {
        checked += 1;
      }
    }
    if (elements.countSpan) {
      elements.countSpan.textContent = checked;
    }
    if (elements.toolbar) {
      elements.toolbar.style.display = checked > 0 ? '' : 'none';
    }
    if (elements.selectAll) {
      elements.selectAll.checked = checks.length > 0 && checked === checks.length;
    }
  }

  function setAllVisibleChecks(checked) {
    var checks = getVisibleChecks();
    for (var i = 0; i < checks.length; i++) {
      checks[i].checked = checked;
    }
    updateToolbar();
  }

  function invertVisibleSelection() {
    var checks = getVisibleChecks();
    for (var i = 0; i < checks.length; i++) {
      checks[i].checked = !checks[i].checked;
    }
    updateToolbar();
  }

  function appendHiddenInput(name, value) {
    var input = document.createElement('input');
    input.type = 'hidden';
    input.name = name;
    input.value = value;
    elements.batchKeys.appendChild(input);
  }

  function fillBatchForm(keys, extraFields) {
    elements.batchKeys.innerHTML = '';
    for (var i = 0; i < keys.length; i++) {
      appendHiddenInput('keys', keys[i]);
    }
    if (!extraFields) {
      return;
    }
    for (var key in extraFields) {
      if (Object.prototype.hasOwnProperty.call(extraFields, key)) {
        appendHiddenInput(key, extraFields[key]);
      }
    }
  }

  function submitBatch(action, extraFields) {
    var keys = getSelectedKeys();
    if (!keys.length) {
      return [];
    }
    elements.batchForm.action = action;
    fillBatchForm(keys, extraFields);
    elements.batchForm.submit();
    return keys;
  }

  function formatTemplate(template, replacements) {
    var output = template || '';
    for (var key in replacements) {
      if (Object.prototype.hasOwnProperty.call(replacements, key)) {
        output = output.replace(new RegExp('\\{' + key + '\\}', 'g'), String(replacements[key]));
      }
    }
    return output;
  }

  async function readJson(response) {
    var text = await response.text();
    if (!text) {
      return {};
    }
    try {
      return JSON.parse(text);
    } catch (_err) {
      throw new Error(elements.detectI18n ? elements.detectI18n.dataset.unknownError : 'Request failed');
    }
  }

  function requestErrorMessage(payload) {
    return payload.error || (elements.detectI18n ? elements.detectI18n.dataset.unknownError : 'Request failed');
  }

  async function postJson(url, params) {
    var response = await fetch(url, {
      method: 'POST',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      body: params
    });
    var payload = await readJson(response);
    if (!response.ok || payload.ok === false) {
      throw new Error(requestErrorMessage(payload));
    }
    return payload;
  }

  async function fetchCountryJob(jobId) {
    var response = await fetch('/dashboard/proxies/tags/country/detect/' + encodeURIComponent(jobId), {
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    });
    var payload = await readJson(response);
    if (!response.ok || payload.ok === false) {
      throw new Error(requestErrorMessage(payload));
    }
    return payload.job;
  }

  function wait(ms) {
    return new Promise(function(resolve) {
      window.setTimeout(resolve, ms);
    });
  }

  function setCountryDetectBusy(busy) {
    var buttons = document.querySelectorAll('.pp-detect-country-btn, #detect-missing-country-btn, #batch-detect-country-btn');

    state.detectInFlight = !!busy;
    for (var i = 0; i < buttons.length; i++) {
      if (busy) {
        buttons[i].dataset.prevDisabled = buttons[i].disabled ? '1' : '0';
        buttons[i].disabled = true;
      } else {
        buttons[i].disabled = buttons[i].dataset.prevDisabled === '1';
      }
      buttons[i].classList.toggle('pp-loading', !!busy);
    }
  }

  function finishCountryDetection(job) {
    var type = job.failure_count > 0 ? 'warning' : 'success';
    var template = elements.detectI18n ? elements.detectI18n.dataset.completedTemplate : 'Completed: {success}/{total}';
    var summary = formatTemplate(template, {
      success: job.success_count || 0,
      failure: job.failure_count || 0,
      updated: job.updated_count || 0,
      total: job.total || 0
    });

    if ((job.success_count || 0) > 0 || (job.updated_count || 0) > 0) {
      var nextUrl = new URL(window.location.href);
      nextUrl.searchParams.set('msg', summary);
      nextUrl.searchParams.set('type', type);
      window.location.href = nextUrl.toString();
      return;
    }
    showToast(summary, type, 6000);
  }

  async function pollCountryJob(jobId, token) {
    while (state.detectPollingToken === token) {
      var job = await fetchCountryJob(jobId);
      if (job.status === 'completed' || job.status === 'failed') {
        return job;
      }
      await wait(job.poll_interval_ms || 750);
    }
    return null;
  }

  async function startCountryDetection(params) {
    if (state.detectInFlight || !window.fetch || !elements.csrfInput) {
      return;
    }
    params.set('csrf_token', elements.csrfInput.value);
    state.detectPollingToken += 1;
    setCountryDetectBusy(true);
    try {
      var token = state.detectPollingToken;
      var payload = await postJson('/dashboard/proxies/tags/country/detect', params);
      if (payload.message) {
        showToast(payload.message, 'success');
      }
      var job = await pollCountryJob(payload.job.job_id, token);
      if (!job) {
        return;
      }
      if (job.status === 'failed') {
        showToast(
          job.error || (elements.detectI18n ? elements.detectI18n.dataset.failedTemplate : 'Country detection failed.'),
          'error',
          6000
        );
        return;
      }
      finishCountryDetection(job);
    } catch (error) {
      showToast(
        (error && error.message) || (elements.detectI18n ? elements.detectI18n.dataset.unknownError : 'Request failed'),
        'error',
        6000
      );
    } finally {
      setCountryDetectBusy(false);
    }
  }

  function startBatchCountryDetection() {
    var keys = getSelectedKeys();
    var params;

    if (!keys.length) {
      showToast(elements.detectI18n ? elements.detectI18n.dataset.noSelection : 'No proxies selected', 'warning', 5000);
      return;
    }
    params = new URLSearchParams();
    for (var i = 0; i < keys.length; i++) {
      params.append('keys', keys[i]);
    }
    startCountryDetection(params);
  }

  function startMissingCountryDetection() {
    var params = new URLSearchParams();
    params.set('detect_missing_only', '1');
    startCountryDetection(params);
  }

  function startSingleCountryDetection(key) {
    var params = new URLSearchParams();
    params.append('keys', key);
    startCountryDetection(params);
  }

  function applyPoolButtonState(button, isOn) {
    if (!button) {
      return;
    }
    button.dataset.state = isOn ? 'on' : 'off';
    button.textContent = isOn ? button.dataset.onLabel : button.dataset.offLabel;
    button.style.border = 'none';
    button.style.fontWeight = '600';
    button.style.fontSize = '0.75rem';
    if (isOn) {
      button.style.background = 'var(--pp-success-subtle)';
      button.style.color = 'var(--pp-success)';
      return;
    }
    button.style.background = 'var(--pp-surface-hover)';
    button.style.color = 'var(--pp-text-muted)';
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
    if (payload.message) {
      showToast(payload.message, 'success');
    }
  }

  function handleBatchAction(actionButton) {
    var action = actionButton.dataset.batchAction;
    var keys;

    if (!action) {
      return;
    }
    if (action === 'invert') {
      invertVisibleSelection();
      return;
    }
    if (action === 'detect-country') {
      startBatchCountryDetection();
      return;
    }
    if (action === 'generate-compat') {
      submitBatch('/dashboard/proxies/batch/compat', {
        start_port: elements.compatStartPort ? elements.compatStartPort.value : ''
      });
      return;
    }
    if (action === 'toggle-pool') {
      submitBatch('/dashboard/proxies/batch/toggle-pool', { pool_state: actionButton.dataset.poolState || '' });
      return;
    }
    if (action !== 'delete') {
      return;
    }
    keys = getSelectedKeys();
    if (!keys.length) {
      return;
    }
    if (!confirm(formatTemplate(elements.deleteConfirm ? elements.deleteConfirm.textContent : 'Delete {count} selected proxies?', { count: keys.length }))) {
      return;
    }
    submitBatch('/dashboard/proxies/batch/delete');
  }

  function handleDocumentClick(event) {
    var batchButton = event.target.closest('[data-batch-action]');
    var detectButton;
    var key;

    if (batchButton && (!elements.toolbar || elements.toolbar.contains(batchButton))) {
      handleBatchAction(batchButton);
      return;
    }
    if (elements.detectMissingBtn && event.target.closest('#detect-missing-country-btn')) {
      startMissingCountryDetection();
      return;
    }
    detectButton = event.target.closest('.pp-detect-country-btn');
    if (!detectButton) {
      return;
    }
    key = detectButton.dataset.key;
    if (!key) {
      return;
    }
    startSingleCountryDetection(key);
  }

  function handleDocumentSubmit(event) {
    var form = event.target;
    var button;

    if (!form.classList || !form.classList.contains('pp-toggle-pool-form') || !window.fetch) {
      return;
    }
    event.preventDefault();
    button = form.querySelector('.pp-pool-toggle-btn');
    if (!button || button.disabled) {
      return;
    }
    button.disabled = true;
    togglePoolAsync(form, button).catch(function(error) {
      showToast(
        (error && error.message) || (elements.poolToggleError ? elements.poolToggleError.textContent : 'Request failed'),
        'error',
        6000
      );
    }).finally(function() {
      button.disabled = false;
    });
  }

  if (elements.selectAll) {
    elements.selectAll.addEventListener('change', function() {
      setAllVisibleChecks(elements.selectAll.checked);
    });
  }
  document.addEventListener('change', function(event) {
    if (event.target.classList.contains('row-check')) {
      updateToolbar();
    }
  });
  document.addEventListener('click', handleDocumentClick);
  document.addEventListener('submit', handleDocumentSubmit);

  updateToolbar();
})();
</script>
