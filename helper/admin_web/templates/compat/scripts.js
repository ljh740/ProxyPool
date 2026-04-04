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
    }
  });

  document.addEventListener('click', (event) => {
    const editButton = event.target.closest('.pp-compat-edit-btn');
    const cancelButton = event.target.closest('#compat-cancel-edit');

    if (editButton) {
      applyEditState(editButton);
      return;
    }
    if (cancelButton) {
      resetFormToDefault();
    }
  });

  document.addEventListener('submit', async (event) => {
    const form = event.target;
    const submitButton = form.querySelector('button[type="submit"]');

    if (!window.fetch || !form) return;
    if (form.id !== 'compat-form' && !form.classList.contains('pp-compat-delete-form')) return;

    event.preventDefault();
    if (submitButton && submitButton.disabled) return;

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
})();
</script>
