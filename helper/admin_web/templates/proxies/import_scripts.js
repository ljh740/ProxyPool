<script>
(function(){
  var form = document.getElementById('proxy-import-form');
  if (!form) return;

  var checkbox = document.getElementById('probe_before_import');
  var compatCheckbox = document.getElementById('generate_compat_ports');
  var compatStartPort = document.getElementById('compat_start_port');
  var submitBtn = form.querySelector('button[type="submit"]');
  var csrfInput = form.querySelector('input[name="csrf_token"]');
  var i18n = document.getElementById('proxy-import-i18n');
  var modalEl = document.getElementById('import-check-modal');
  var statusText = document.getElementById('import-check-status-text');
  var countsText = document.getElementById('import-check-counts');
  var summaryText = document.getElementById('import-check-summary');
  var resultsBody = document.getElementById('import-check-results');
  var progressBar = document.getElementById('import-check-progress-bar');
  var commitBtn = document.getElementById('import-check-commit');
  var closeBtn = document.getElementById('import-check-close');
  var closeXBtn = document.getElementById('import-check-close-x');
  var modal = window.bootstrap && modalEl ? new window.bootstrap.Modal(modalEl, {
    backdrop: 'static',
    keyboard: false
  }) : null;
  var activeJobId = '';
  var pollingToken = 0;

  if (!checkbox || !submitBtn || !csrfInput || !i18n || !modalEl) return;

  function syncCompatOptions() {
    if (!compatCheckbox || !compatStartPort) return;
    compatStartPort.disabled = !compatCheckbox.checked;
  }

  function dataset(key) {
    return i18n.dataset[key] || '';
  }

  function setButtonBusy(btn, busy) {
    if (!btn) return;
    btn.disabled = !!busy;
    btn.classList.toggle('pp-loading', !!busy);
  }

  function setModalClosable(closable) {
    if (closeBtn) closeBtn.disabled = !closable;
    if (closeXBtn) closeXBtn.disabled = !closable;
  }

  function showModal() {
    if (modal) {
      modal.show();
      return;
    }
    modalEl.style.display = 'block';
    modalEl.classList.add('show');
    modalEl.removeAttribute('aria-hidden');
    document.body.classList.add('modal-open');
  }

  function hideModal() {
    if (modal) {
      modal.hide();
      return;
    }
    modalEl.classList.remove('show');
    modalEl.style.display = 'none';
    modalEl.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('modal-open');
    pollingToken += 1;
  }

  function resetModal() {
    if (statusText) statusText.textContent = dataset('processing');
    if (countsText) countsText.textContent = '0 / 0';
    if (summaryText) summaryText.textContent = '';
    if (resultsBody) resultsBody.innerHTML = '';
    if (progressBar) {
      progressBar.style.width = '0%';
      progressBar.classList.add('progress-bar-indeterminate');
      progressBar.className = 'progress-bar progress-bar-indeterminate';
    }
    if (commitBtn) commitBtn.disabled = true;
    setModalClosable(false);
  }

  function buildBadge(result) {
    var badge = document.createElement('span');
    badge.className = 'badge';
    if (result.status === 'ok') {
      badge.className += ' bg-success-lt text-success';
      badge.textContent = dataset('statusOk');
      return badge;
    }
    if (result.status === 'error') {
      badge.className += ' bg-danger-lt text-danger';
      badge.textContent = dataset('statusError');
      return badge;
    }
    badge.className += ' bg-secondary-lt text-secondary';
    badge.textContent = dataset('statusPending');
    return badge;
  }

  function renderResults(results) {
    if (!resultsBody) return;
    resultsBody.innerHTML = '';
    for (var i = 0; i < results.length; i++) {
      var result = results[i];
      var tr = document.createElement('tr');

      var lineTd = document.createElement('td');
      lineTd.textContent = String(result.line);

      var proxyTd = document.createElement('td');
      var proxyCode = document.createElement('code');
      proxyCode.textContent = result.display;
      proxyTd.appendChild(proxyCode);

      var statusTd = document.createElement('td');
      statusTd.appendChild(buildBadge(result));

      var messageTd = document.createElement('td');
      var message = result.message || '';
      if (result.duration_ms !== null && result.duration_ms !== undefined && result.status === 'ok') {
        message = message ? (message + ' (' + result.duration_ms + ' ms)') : (result.duration_ms + ' ms');
      }
      messageTd.textContent = message;

      tr.appendChild(lineTd);
      tr.appendChild(proxyTd);
      tr.appendChild(statusTd);
      tr.appendChild(messageTd);
      resultsBody.appendChild(tr);
    }
  }

  function updateJob(job) {
    var total = job.total || 0;
    var completed = job.completed || 0;
    var successCount = job.success_count || 0;
    var failureCount = job.failure_count || 0;
    var percent = total ? Math.round((completed / total) * 100) : 0;

    if (countsText) countsText.textContent = completed + ' / ' + total;
    if (summaryText) summaryText.textContent = successCount + ' ok, ' + failureCount + ' failed';
    if (progressBar) {
      progressBar.style.width = percent + '%';
      progressBar.classList.remove('progress-bar-indeterminate');
      if (job.status === 'completed') {
        progressBar.className = 'progress-bar bg-success';
      } else if (job.status === 'failed') {
        progressBar.className = 'progress-bar bg-danger';
      } else {
        progressBar.className = 'progress-bar bg-primary';
      }
    }
    if (statusText) {
      if (job.status === 'completed') {
        statusText.textContent = dataset('completed');
      } else if (job.status === 'failed') {
        statusText.textContent = dataset('failed');
      } else {
        statusText.textContent = dataset('running');
      }
    }
    renderResults(job.results || []);

    var done = job.status === 'completed' || job.status === 'failed';
    if (commitBtn) commitBtn.disabled = !(job.status === 'completed' && successCount > 0);
    setModalClosable(done);
    if (done && job.status === 'failed' && job.error) {
      showToast(job.error, 'error', 6000);
    }
  }

  function buildFormParams(extra) {
    var params = new URLSearchParams(new FormData(form));
    if (extra) {
      for (var key in extra) {
        if (Object.prototype.hasOwnProperty.call(extra, key)) {
          params.set(key, extra[key]);
        }
      }
    }
    return params;
  }

  async function readJson(response) {
    var text = await response.text();
    if (!text) return {};
    try {
      return JSON.parse(text);
    } catch (_err) {
      throw new Error(dataset('unknownError'));
    }
  }

  async function postJson(url, params) {
    var response = await fetch(url, {
      method: 'POST',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      body: params
    });
    var payload = await readJson(response);
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || dataset('unknownError'));
    }
    return payload;
  }

  async function fetchJob(jobId) {
    var response = await fetch('/dashboard/proxies/import/check/' + encodeURIComponent(jobId), {
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    });
    var payload = await readJson(response);
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || dataset('unknownError'));
    }
    return payload.job;
  }

  function wait(ms) {
    return new Promise(function(resolve) { window.setTimeout(resolve, ms); });
  }

  async function pollJob(jobId, token) {
    while (pollingToken === token) {
      var job = await fetchJob(jobId);
      updateJob(job);
      if (job.status === 'completed' || job.status === 'failed') return job;
      await wait(job.poll_interval_ms || 750);
    }
    return null;
  }

  form.addEventListener('submit', async function(event) {
    if (!checkbox.checked) return;
    event.preventDefault();
    if (submitBtn.disabled) return;

    setButtonBusy(submitBtn, true);
    resetModal();
    showModal();
    activeJobId = '';
    pollingToken += 1;
    var token = pollingToken;

    try {
      var payload = await postJson('/dashboard/proxies/import/check', buildFormParams());
      activeJobId = payload.job.job_id;
      updateJob(payload.job);
      await pollJob(activeJobId, token);
    } catch (error) {
      if (statusText) statusText.textContent = dataset('failed');
      if (summaryText) summaryText.textContent = error.message || dataset('unknownError');
      if (progressBar) {
        progressBar.className = 'progress-bar bg-danger';
        progressBar.style.width = '100%';
      }
      setModalClosable(true);
      showToast(error.message || dataset('unknownError'), 'error', 6000);
    } finally {
      setButtonBusy(submitBtn, false);
    }
  });

  if (compatCheckbox) {
    compatCheckbox.addEventListener('change', syncCompatOptions);
  }

  if (commitBtn) {
    commitBtn.addEventListener('click', async function() {
      if (!activeJobId || commitBtn.disabled) return;
      setButtonBusy(commitBtn, true);
      try {
        var payload = await postJson('/dashboard/proxies/import/commit', buildFormParams({
          job_id: activeJobId,
          csrf_token: csrfInput.value
        }));
        if (payload.redirect_url) {
          window.location.href = payload.redirect_url;
          return;
        }
        showToast(payload.message || dataset('completed'));
      } catch (error) {
        showToast(error.message || dataset('unknownError'), 'error', 6000);
      } finally {
        setButtonBusy(commitBtn, false);
      }
    });
  }

  if (modal) {
    modalEl.addEventListener('hidden.bs.modal', function() {
      pollingToken += 1;
    });
  } else {
    if (closeBtn) {
      closeBtn.addEventListener('click', function() {
        if (!closeBtn.disabled) hideModal();
      });
    }
    if (closeXBtn) {
      closeXBtn.addEventListener('click', function() {
        if (!closeXBtn.disabled) hideModal();
      });
    }
  }
  syncCompatOptions();
})();
</script>
