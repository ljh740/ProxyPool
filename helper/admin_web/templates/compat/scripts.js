<script>
(() => {
  const select = document.getElementById('target_type');
  const hint = document.getElementById('compat-target-hint');
  if (!select || !hint) return;
  const updateHint = () => {
    hint.textContent = select.value === 'entry_key'
      ? hint.dataset.entryKeyHint
      : hint.dataset.sessionNameHint;
  };
  select.addEventListener('change', updateHint);
  updateHint();
})();
</script>
