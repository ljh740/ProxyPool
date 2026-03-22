<script>
(function(){
  var root = document.querySelector('[data-proxy-summary-card]');
  if (!root) return;

  function setButtonState(button, state) {
    if (!button) return;
    var icon = button.querySelector('.ti');
    var label = button.querySelector('.pp-copy-btn-label');
    button.dataset.copyState = state;
    if (state === 'copied') {
      if (icon) icon.className = 'ti ti-check';
      if (label) label.textContent = button.dataset.copiedLabel || 'Copied';
      return;
    }
    if (icon) {
      icon.className = button.id === 'copy-entry-key-btn' ? 'ti ti-key' : 'ti ti-copy';
    }
    if (label) label.textContent = button.dataset.defaultLabel || 'Copy';
  }

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch (error) {}
    }

    var helper = document.createElement('textarea');
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
    var copied = false;
    try {
      copied = document.execCommand('copy');
    } finally {
      document.body.removeChild(helper);
    }
    if (!copied) {
      throw new Error('copy_failed');
    }
  }

  function bindCopy(button) {
    if (!button) return;
    var target = document.getElementById(button.dataset.copyTarget || '');
    if (!target) return;

    button.addEventListener('click', async function() {
      if (button.disabled) return;
      button.disabled = true;
      try {
        await copyText(target.value || target.textContent || '');
        setButtonState(button, 'copied');
        if (button.dataset.successMessage) {
          showToast(button.dataset.successMessage, 'success', 2200);
        }
        clearTimeout(button._resetTimer);
        button._resetTimer = window.setTimeout(function() {
          setButtonState(button, 'idle');
        }, 1600);
      } catch (error) {
        showToast(button.dataset.errorMessage || 'Copy failed', 'error', 4000);
      } finally {
        button.disabled = false;
      }
    });
  }

  bindCopy(document.getElementById('copy-entry-key-btn'));
  bindCopy(document.getElementById('copy-chain-uri-btn'));
})();
</script>
