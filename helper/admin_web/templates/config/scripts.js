<script>
(() => {
  const form = document.getElementById('config-form');
  const i18n = document.getElementById('config-form-i18n');

  if (!form || !window.fetch) return;

  form.addEventListener('submit', async (event) => {
    const submitButton = form.querySelector('.btn-primary');
    const fallbackMessage = i18n ? i18n.dataset.requestFailed : 'Request failed';

    event.preventDefault();
    if (submitButton && submitButton.disabled) return;

    setLoading(submitButton);
    try {
      const payload = await adminPostForm(form, fallbackMessage);
      showToast(payload.message || '', 'success');
    } catch (error) {
      showToast((error && error.message) || fallbackMessage, 'error', 6000);
    } finally {
      clearLoading(submitButton);
    }
  });
})();
</script>
