<script>
(() => {
  const i18n = document.getElementById('compat-page-i18n');

  if (!i18n) return;

  function compatForm() {
    return document.getElementById('compat-form');
  }

  function compatFormPanel() {
    return document.getElementById('compat-form-panel');
  }

  function compatMappingsPanel() {
    return document.getElementById('compat-mappings-panel');
  }

  function fallbackMessage() {
    return i18n.dataset.requestFailed || 'Request failed';
  }

  function compatBatchForm() {
    return document.getElementById('compat-batch-delete-form');
  }

  function compatBatchPorts() {
    return document.getElementById('compat-batch-ports');
  }

  function compatRowChecks() {
    return Array.from(document.querySelectorAll('.pp-compat-row-check'));
  }

  function selectedPorts() {
    return compatRowChecks()
      .filter((input) => input.checked)
      .map((input) => input.value);
  }

  function formatTemplate(template, replacements) {
    let output = template || '';
    Object.keys(replacements || {}).forEach((key) => {
      output = output.replace(new RegExp('\\{' + key + '\\}', 'g'), String(replacements[key]));
    });
    return output;
  }

  function fillBatchPorts(ports) {
    const container = compatBatchPorts();
    if (!container) return;
    container.innerHTML = '';
    ports.forEach((port) => {
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'listen_ports';
      input.value = port;
      container.appendChild(input);
    });
  }

  function compatPortLines(ports) {
    return ports.map((port) => `http://127.0.0.1:${port}`);
  }

  async function copyText(text) {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch (error) {}
    }

    const helper = document.createElement('textarea');
    helper.value = text;
    helper.setAttribute('readonly', '');
    helper.style.position = 'fixed';
    helper.style.top = '0';
    helper.style.left = '0';
    helper.style.opacity = '0';
    helper.style.pointerEvents = 'none';
    document.body.appendChild(helper);
    helper.focus();
    helper.select();
    helper.setSelectionRange(0, helper.value.length);
    let copied = false;
    try {
      copied = document.execCommand('copy');
    } finally {
      document.body.removeChild(helper);
    }
    if (!copied) {
      throw new Error('copy_failed');
    }
  }

  async function copyCompatPorts(ports) {
    if (!ports.length) return;
    await copyText(compatPortLines(ports).join('\n'));
  }

  function exportCompatPorts(ports) {
    if (!ports.length) return;
    const lines = compatPortLines(ports);
    const blob = new Blob([lines.join('\n') + '\n'], { type: 'text/plain;charset=utf-8' });
    const link = document.createElement('a');
    const objectUrl = window.URL.createObjectURL(blob);
    link.href = objectUrl;
    link.download = 'compat-ports.txt';
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => {
      window.URL.revokeObjectURL(objectUrl);
    }, 0);
  }

  function syncBatchToolbar() {
    const toolbar = compatBatchForm();
    const checks = compatRowChecks();
    const checked = checks.filter((input) => input.checked).length;
    const count = document.getElementById('compat-selected-count');
    const selectAll = document.getElementById('compat-select-all');

    checks.forEach((input) => {
      const row = input.closest('tr');
      if (row) row.classList.toggle('pp-selected-row', input.checked);
    });
    if (count) count.textContent = String(checked);
    if (toolbar) toolbar.style.display = checked > 0 ? '' : 'none';
    if (selectAll) {
      selectAll.checked = checks.length > 0 && checked === checks.length;
    }
  }

  function setAllChecked(checked) {
    compatRowChecks().forEach((input) => {
      input.checked = checked;
    });
    syncBatchToolbar();
  }

  function invertSelection() {
    compatRowChecks().forEach((input) => {
      input.checked = !input.checked;
    });
    syncBatchToolbar();
  }

  function updateHint() {
    const select = document.getElementById('target_type');
    const hint = document.getElementById('compat-target-hint');

    if (!select || !hint) return;
    hint.textContent = select.value === 'entry_key'
      ? hint.dataset.entryKeyHint
      : hint.dataset.sessionNameHint;
  }

  function resetFormToDefault() {
    const form = compatForm();
    const originalPort = document.getElementById('compat-original-listen-port');
    const listenPort = document.getElementById('listen_port');
    const targetType = document.getElementById('target_type');
    const targetValue = document.getElementById('target_value');
    const note = document.getElementById('note');
    const enabled = document.getElementById('compat-enabled');
    const submitLabel = document.getElementById('compat-submit-label');
    const cancelButton = document.getElementById('compat-cancel-edit');

    if (!form) return;
    form.reset();
    if (originalPort) originalPort.value = '';
    if (listenPort) listenPort.value = form.dataset.defaultListenPort || listenPort.value;
    if (targetType) targetType.value = form.dataset.defaultTargetType || 'session_name';
    if (targetValue) targetValue.value = '';
    if (note) note.value = '';
    if (enabled) enabled.checked = true;
    if (submitLabel) {
      submitLabel.innerHTML = '<i class="ti ti-device-floppy me-1"></i>' + (form.dataset.saveLabel || i18n.dataset.saveLabel || '');
    }
    if (cancelButton) cancelButton.style.display = 'none';
    updateHint();
  }

  function applyEditState(button) {
    const form = compatForm();
    const originalPort = document.getElementById('compat-original-listen-port');
    const listenPort = document.getElementById('listen_port');
    const targetType = document.getElementById('target_type');
    const targetValue = document.getElementById('target_value');
    const note = document.getElementById('note');
    const enabled = document.getElementById('compat-enabled');
    const submitLabel = document.getElementById('compat-submit-label');
    const cancelButton = document.getElementById('compat-cancel-edit');

    if (!form || !button) return;
    if (originalPort) originalPort.value = button.dataset.listenPort || '';
    if (listenPort) listenPort.value = button.dataset.listenPort || '';
    if (targetType) targetType.value = button.dataset.targetType || 'session_name';
    if (targetValue) targetValue.value = button.dataset.targetValue || '';
    if (note) note.value = button.dataset.note || '';
    if (enabled) enabled.checked = button.dataset.enabled !== '0';
    if (submitLabel) {
      submitLabel.innerHTML = '<i class="ti ti-device-floppy me-1"></i>' + (form.dataset.updateLabel || i18n.dataset.updateLabel || '');
    }
    if (cancelButton) cancelButton.style.display = '';
    updateHint();
    form.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function replaceCompatPanels(payload) {
    const formPanel = compatFormPanel();
    const mappingsPanel = compatMappingsPanel();

    if (formPanel && payload.form_html) formPanel.innerHTML = payload.form_html;
    if (mappingsPanel && payload.mappings_html) mappingsPanel.innerHTML = payload.mappings_html;
    updateHint();
    syncBatchToolbar();
  }

  async function submitCompatAction(form) {
    const payload = await adminPostForm(form, fallbackMessage());
    replaceCompatPanels(payload);
    resetFormToDefault();
    if (payload.message) showToast(payload.message, 'success');
  }

  document.addEventListener('change', (event) => {
    if (event.target && event.target.id === 'target_type') {
      updateHint();
      return;
    }
    if (event.target && event.target.id === 'compat-select-all') {
      setAllChecked(event.target.checked);
      return;
    }
    if (event.target && event.target.classList.contains('pp-compat-row-check')) {
      syncBatchToolbar();
    }
  });

  document.addEventListener('click', (event) => {
    const editButton = event.target.closest('.pp-compat-edit-btn');
    const cancelButton = event.target.closest('#compat-cancel-edit');
    const batchActionButton = event.target.closest('[data-compat-batch-action]');

    if (editButton) {
      applyEditState(editButton);
      return;
    }
    if (batchActionButton && batchActionButton.dataset.compatBatchAction === 'copy') {
      const ports = selectedPorts();
      if (!ports.length) {
        showToast(i18n.dataset.noSelection || fallbackMessage(), 'error', 4000);
        syncBatchToolbar();
        return;
      }
      copyCompatPorts(ports)
        .then(() => {
          showToast(
            formatTemplate(i18n.dataset.copySuccess || '', { count: ports.length }),
            'success',
            2200
          );
        })
        .catch(() => {
          showToast(i18n.dataset.copyFailed || fallbackMessage(), 'error', 4000);
        });
      return;
    }
    if (batchActionButton && batchActionButton.dataset.compatBatchAction === 'export') {
      const ports = selectedPorts();
      if (!ports.length) {
        showToast(i18n.dataset.noSelection || fallbackMessage(), 'error', 4000);
        syncBatchToolbar();
        return;
      }
      exportCompatPorts(ports);
      return;
    }
    if (batchActionButton && batchActionButton.dataset.compatBatchAction === 'invert') {
      invertSelection();
      return;
    }
    if (cancelButton) {
      resetFormToDefault();
    }
  });

  document.addEventListener('submit', async (event) => {
    const form = event.target;
    const submitButton = form.querySelector('button[type="submit"]');
    const batchForm = form.id === 'compat-batch-delete-form';

    if (!window.fetch || !form) return;
    if (
      form.id !== 'compat-form'
      && form.id !== 'compat-batch-delete-form'
      && !form.classList.contains('pp-compat-delete-form')
    ) return;

    event.preventDefault();
    if (submitButton && submitButton.disabled) return;
    if (batchForm) {
      const ports = selectedPorts();
      if (!ports.length) {
        showToast(i18n.dataset.noSelection || fallbackMessage(), 'error', 4000);
        syncBatchToolbar();
        return;
      }
      if (!window.confirm(formatTemplate(i18n.dataset.deleteSelectedConfirm || '', { count: ports.length }))) {
        return;
      }
      fillBatchPorts(ports);
    }

    setLoading(submitButton);
    try {
      await submitCompatAction(form);
    } catch (error) {
      if (error && error.payload) {
        replaceCompatPanels(error.payload);
      }
      showToast((error && error.message) || fallbackMessage(), 'error', 6000);
    } finally {
      clearLoading(submitButton);
    }
  });

  updateHint();
  syncBatchToolbar();
})();
</script>
