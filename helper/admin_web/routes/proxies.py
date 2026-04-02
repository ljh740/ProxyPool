"""Proxy-management routes for the admin Flask app."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import quote as _url_quote

from flask import render_template, request

from i18n import get_translations, t
from persistence import load_batch_params, save_batch_params
from proxy_tags import (
    COUNTRY_TAG,
    entry_country_tag,
    merge_country_tag_updates,
    normalize_country_tag,
    resolve_entry_country_tag,
    without_entry_tags,
)
from upstream_pool import (
    SUPPORTED_UPSTREAM_SCHEMES,
    UpstreamEntry,
    UpstreamHop,
    compute_entry_key,
    parse_upstream_line,
    parse_upstream_hop,
)

from .. import resources as admin_resources
from ..app_runtime import build_redirect_location
from ..http import is_ajax_request as _is_ajax_request
from ..http import json_response as _json_response

IMPORT_CHECK_TARGET_HOST = "example.com"
IMPORT_CHECK_TARGET_PORT = 443
IMPORT_CHECK_TIMEOUT_CAP = 8.0
IMPORT_CHECK_JOB_TTL_SECONDS = 1800
IMPORT_CHECK_POLL_INTERVAL_MS = 750
_IMPORT_CHECK_JOBS = {}
_IMPORT_CHECK_JOBS_LOCK = threading.RLock()
COUNTRY_DETECT_JOB_TTL_SECONDS = 1800
COUNTRY_DETECT_POLL_INTERVAL_MS = 750
_COUNTRY_DETECT_JOBS = {}
_COUNTRY_DETECT_JOBS_LOCK = threading.RLock()


@dataclass
class _ImportCheckJob:
    job_id: str
    locale: str
    parsed_entries: list
    status: str = "pending"
    results: list = field(default_factory=list)
    successful_entries: list = field(default_factory=list)
    completed: int = 0
    created_at: float = field(default_factory=time.time)
    error: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def snapshot(self):
        with self.lock:
            return _build_job_snapshot(
                self,
                poll_interval_ms=IMPORT_CHECK_POLL_INTERVAL_MS,
                success_count=len(self.successful_entries),
            )


@dataclass
class _CountryDetectJob:
    job_id: str
    locale: str
    target_entries: list
    status: str = "pending"
    results: list = field(default_factory=list)
    completed: int = 0
    success_count: int = 0
    updated_count: int = 0
    created_at: float = field(default_factory=time.time)
    error: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def snapshot(self):
        with self.lock:
            return _build_job_snapshot(
                self,
                poll_interval_ms=COUNTRY_DETECT_POLL_INTERVAL_MS,
                success_count=self.success_count,
                extra_fields={"updated_count": self.updated_count},
            )


def _build_job_snapshot(job, *, poll_interval_ms, success_count, extra_fields=None):
    snapshot = {
        "job_id": job.job_id,
        "status": job.status,
        "total": len(job.results),
        "completed": job.completed,
        "success_count": success_count,
        "failure_count": job.completed - success_count,
        "error": job.error,
        "results": [dict(item) for item in job.results],
        "poll_interval_ms": poll_interval_ms,
    }
    if extra_fields:
        snapshot.update(extra_fields)
    return snapshot


def _prune_jobs(job_store, jobs_lock, ttl_seconds):
    cutoff = time.time() - ttl_seconds
    with jobs_lock:
        stale_ids = [job_id for job_id, job in job_store.items() if job.created_at < cutoff]
        for job_id in stale_ids:
            job_store.pop(job_id, None)


def _get_job(job_id, *, job_store, jobs_lock, ttl_seconds):
    _prune_jobs(job_store, jobs_lock, ttl_seconds)
    with jobs_lock:
        return job_store.get(job_id)


def _remove_job(job_id, *, job_store, jobs_lock):
    with jobs_lock:
        job_store.pop(job_id, None)


def _launch_background_job(job, *, job_store, jobs_lock, ttl_seconds, target, args):
    _prune_jobs(job_store, jobs_lock, ttl_seconds)
    with jobs_lock:
        job_store[job.job_id] = job
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()
    return job


def _format_hop_uri(hop):
    auth = ""
    if hop.username or hop.password:
        auth = "%s:%s@" % (_url_quote(hop.username), _url_quote(hop.password))
    return "%s://%s%s:%s" % (hop.scheme, auth, hop.host, hop.port)


def _prepend_hop_value(hops):
    if len(hops) <= 1:
        return ""
    return ", ".join(_format_hop_uri(hop) for hop in hops[:-1])


def _format_chain_uri(hops):
    return " | ".join(_format_hop_uri(hop) for hop in hops)


def _build_proxy_summary(entry):
    return {
        "entry_key": entry.key,
        "chain_uri": _format_chain_uri(entry.hops),
        "hop_count": len(entry.hops),
    }

def _build_proxy_list_state(locale, entries, source_filter, country_filter, page_value, proxies_per_page):
    manual_count = sum(1 for entry in entries if entry.source_tag == "manual")
    auto_count = len(entries) - manual_count
    available_countries = sorted(
        {entry_country_tag(entry) for entry in entries if entry_country_tag(entry)}
    )
    missing_country_count = sum(1 for entry in entries if not entry_country_tag(entry))
    active_filter = (source_filter or "all").strip().lower()
    if active_filter not in {"all", "manual", "auto"}:
        active_filter = "all"
    active_country_filter = normalize_country_tag(country_filter)

    if active_filter == "all":
        filtered_entries = entries
    else:
        filtered_entries = [entry for entry in entries if entry.source_tag == active_filter]
    if active_country_filter:
        filtered_entries = [
            entry
            for entry in filtered_entries
            if entry_country_tag(entry) == active_country_filter
        ]
    filtered_total = len(filtered_entries)
    total_pages = max(1, math.ceil(filtered_total / proxies_per_page))
    try:
        page = int(page_value)
    except (TypeError, ValueError):
        page = 1
    page = max(1, min(page, total_pages))

    start = (page - 1) * proxies_per_page
    end = start + proxies_per_page
    page_entries = filtered_entries[start:end]

    def _page_url(page_number, source=None, country=None):
        page_source = active_filter if source is None else source
        page_country = active_country_filter if country is None else normalize_country_tag(country)
        params = []
        if page_source != "all":
            params.append(("source", page_source))
        if page_country:
            params.append(("country", page_country))
        if page_number != 1:
            params.append(("page", str(page_number)))
        return build_redirect_location("/dashboard/proxies", **dict(params)) if params else "/dashboard/proxies"

    if total_pages <= 7:
        page_range = list(range(1, total_pages + 1))
    else:
        page_range = sorted(
            set([1, total_pages] + list(range(max(1, page - 2), min(total_pages, page + 2) + 1)))
        )

    translations = dict(get_translations(locale))
    translations["proxies_clear_confirm"] = t("proxies_clear_confirm", locale, count=auto_count)
    translations["proxies_showing"] = t(
        "proxies_showing",
        locale,
        start=start + 1 if filtered_total else 0,
        end=min(end, filtered_total),
        total=filtered_total,
    )
    return {
        "entries": page_entries,
        "total": len(entries),
        "manual_count": manual_count,
        "auto_count": auto_count,
        "available_countries": available_countries,
        "active_country_filter": active_country_filter,
        "missing_country_count": missing_country_count,
        "active_filter": active_filter,
        "filter_urls": {
            "all": _page_url(1, "all"),
            "manual": _page_url(1, "manual"),
            "auto": _page_url(1, "auto"),
        },
        "page": page,
        "total_pages": total_pages,
        "page_range": page_range,
        "page_urls": {page_number: _page_url(page_number) for page_number in page_range},
        "prev_page_url": _page_url(page - 1),
        "next_page_url": _page_url(page + 1),
        "i": translations,
    }


def _build_proxy_form_context(
    locale,
    *,
    title,
    action_url,
    submit_label,
    scheme,
    host,
    port,
    username,
    password,
    prepend_hop,
    in_random_pool,
    proxy_summary=None,
    error="",
):
    return {
        "title": title,
        "action_url": action_url,
        "submit_label": submit_label,
        "schemes": sorted(SUPPORTED_UPSTREAM_SCHEMES),
        "scheme": scheme,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "prepend_hop": prepend_hop,
        "in_random_pool": in_random_pool,
        "proxy_summary": proxy_summary,
        "error": error,
        "i": get_translations(locale),
    }


def _render_proxy_form_page(
    runtime,
    ui,
    *,
    title,
    action_url,
    submit_label,
    scheme,
    host,
    port,
    username,
    password,
    prepend_hop,
    in_random_pool,
    proxy_summary=None,
    error="",
):
    content = render_template(
        "proxies/form.html",
        **_build_proxy_form_context(
            ui.locale,
            title=title,
            action_url=action_url,
            submit_label=submit_label,
            scheme=scheme,
            host=host,
            port=port,
            username=username,
            password=password,
            prepend_hop=prepend_hop,
            in_random_pool=in_random_pool,
            proxy_summary=proxy_summary,
            error=error,
        ),
    )
    return runtime.build_page_response(
        title=title,
        content=content,
        active_nav="nav_proxies",
        ui=ui,
        extra_scripts=admin_resources.load_template_source("proxies/form_scripts.js"),
    )


def _render_batch_form_page(runtime, ui, *, form_data, error=""):
    content = render_template(
        "proxies/batch_form.html",
        schemes=sorted(SUPPORTED_UPSTREAM_SCHEMES),
        scheme=form_data.get("scheme", "http"),
        host=form_data.get("host", ""),
        username=form_data.get("username", ""),
        password=form_data.get("password", ""),
        port_first=form_data.get("port_first", "10001"),
        port_last=form_data.get("port_last", "10100"),
        prepend_hop=form_data.get("prepend_hop", ""),
        cycle_first_hop=form_data.get("cycle_first_hop", ""),
        error=error,
        i=get_translations(ui.locale),
    )
    return runtime.build_page_response(
        title=t("batch_title", ui.locale),
        content=content,
        active_nav="nav_proxies",
        ui=ui,
    )


def _render_import_form_page(runtime, ui, *, form_data, error=""):
    content = render_template(
        "proxies/import_form.html",
        schemes=sorted(SUPPORTED_UPSTREAM_SCHEMES),
        default_scheme=form_data.get("default_scheme", "http"),
        default_username=form_data.get("default_username", ""),
        default_password=form_data.get("default_password", ""),
        proxy_list_text=form_data.get("proxy_list_text", ""),
        probe_before_import=form_data.get("probe_before_import", False),
        error=error,
        i=get_translations(ui.locale),
    )
    return runtime.build_page_response(
        title=t("import_title", ui.locale),
        content=content,
        active_nav="nav_proxies",
        ui=ui,
        extra_scripts=admin_resources.load_template_source("proxies/import_scripts.js"),
    )


def _replace_auto_entries(existing_entries, new_entries):
    merged = [entry for entry in existing_entries if entry.source_tag != "auto"]
    existing_keys = {entry.key for entry in merged}
    imported_count = 0
    for entry in new_entries:
        normalized = UpstreamEntry(
            key=entry.key,
            label=entry.label,
            hops=entry.hops,
            source_tag="auto",
            in_random_pool=True,
            tags=entry.tags,
        )
        if normalized.key in existing_keys:
            continue
        merged.append(normalized)
        existing_keys.add(normalized.key)
        imported_count += 1
    return merged, imported_count


def _append_manual_entries(existing_entries, new_entries):
    merged = list(existing_entries)
    existing_keys = {entry.key for entry in merged}
    imported_count = 0
    for entry in new_entries:
        normalized = UpstreamEntry(
            key=entry.key,
            label=entry.label,
            hops=entry.hops,
            source_tag="manual",
            in_random_pool=True,
            tags=entry.tags,
        )
        if normalized.key in existing_keys:
            continue
        merged.append(normalized)
        existing_keys.add(normalized.key)
        imported_count += 1
    return merged, imported_count

def _build_import_form_data():
    return {
        "default_scheme": request.form.get("default_scheme", "http").strip().lower(),
        "default_username": request.form.get("default_username", ""),
        "default_password": request.form.get("default_password", ""),
        "proxy_list_text": request.form.get("proxy_list_text", ""),
        "probe_before_import": request.form.get("probe_before_import", "0") == "1",
    }


def _parse_import_entries(form_data, locale):
    default_scheme = form_data["default_scheme"] or "http"
    if default_scheme not in SUPPORTED_UPSTREAM_SCHEMES:
        raise ValueError(t("import_unsupported_scheme", locale, scheme=default_scheme))

    parsed_entries = []
    lines = form_data["proxy_list_text"].splitlines()
    for line_number, line in enumerate(lines, start=1):
        try:
            entry = parse_upstream_line(
                line,
                line_number - 1,
                default_scheme,
                form_data["default_username"],
                form_data["default_password"],
            )
        except ValueError as exc:
            raise ValueError(
                t(
                    "import_line_error",
                    locale,
                    line=line_number,
                    error=str(exc),
                )
            ) from exc
        if entry is not None:
            parsed_entries.append((line_number, entry))

    if not parsed_entries:
        raise ValueError(t("import_no_entries", locale))
    return parsed_entries


def _build_probe_config(app_config):
    from proxy_server import ProxyConfig

    config = ProxyConfig.from_app_config(app_config, strict=False)
    config.connect_timeout = min(config.connect_timeout, IMPORT_CHECK_TIMEOUT_CAP)
    config.connect_retries = 1
    return config


def _probe_import_entry(config, entry):
    from proxy_server import open_upstream_tunnel

    sock = None
    try:
        sock = open_upstream_tunnel(
            config,
            entry,
            IMPORT_CHECK_TARGET_HOST,
            IMPORT_CHECK_TARGET_PORT,
        )
        return True, ""
    except Exception as exc:  # pragma: no cover - message path asserted in higher-level tests
        return False, str(exc)
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _get_import_check_job(job_id):
    return _get_job(
        job_id,
        job_store=_IMPORT_CHECK_JOBS,
        jobs_lock=_IMPORT_CHECK_JOBS_LOCK,
        ttl_seconds=IMPORT_CHECK_JOB_TTL_SECONDS,
    )


def _run_import_check_job(runtime, job):
    try:
        config = _build_probe_config(runtime.load_app_config())
        with job.lock:
            job.status = "running"
        for index, (_line_number, entry) in enumerate(job.parsed_entries):
            started_at = time.monotonic()
            ok, error_message = _probe_import_entry(config, entry)
            duration_ms = int((time.monotonic() - started_at) * 1000)
            with job.lock:
                job.completed += 1
                job.results[index]["status"] = "ok" if ok else "error"
                job.results[index]["message"] = (
                    t("import_check_ok", job.locale)
                    if ok
                    else t("import_check_failed", job.locale, error=error_message)
                )
                job.results[index]["duration_ms"] = duration_ms
                if ok:
                    job.successful_entries.append(entry)
        with job.lock:
            job.status = "completed"
    except Exception as exc:  # pragma: no cover - guarded by integration tests
        with job.lock:
            job.status = "failed"
            job.error = str(exc)


def _create_import_check_job(runtime, locale, parsed_entries):
    job = _ImportCheckJob(
        job_id=uuid.uuid4().hex,
        locale=locale,
        parsed_entries=list(parsed_entries),
        results=[
            {
                "line": line_number,
                "key": entry.key,
                "display": entry.display,
                "status": "pending",
                "message": "",
                "duration_ms": None,
            }
            for line_number, entry in parsed_entries
        ],
    )
    return _launch_background_job(
        job,
        job_store=_IMPORT_CHECK_JOBS,
        jobs_lock=_IMPORT_CHECK_JOBS_LOCK,
        ttl_seconds=IMPORT_CHECK_JOB_TTL_SECONDS,
        target=_run_import_check_job,
        args=(runtime, job),
    )


def _get_country_detect_job(job_id):
    return _get_job(
        job_id,
        job_store=_COUNTRY_DETECT_JOBS,
        jobs_lock=_COUNTRY_DETECT_JOBS_LOCK,
        ttl_seconds=COUNTRY_DETECT_JOB_TTL_SECONDS,
    )


def _country_result_message(locale, country, changed):
    if changed:
        return t("proxies_country_detect_detected", locale, country=country)
    return t("proxies_country_detect_unchanged", locale, country=country)


def _resolve_country_detection_targets(entries, selected_keys, detect_missing_only):
    if selected_keys:
        selected_lookup = set(selected_keys)
        return [entry for entry in entries if entry.key in selected_lookup]
    if detect_missing_only:
        return [entry for entry in entries if not entry_country_tag(entry)]
    return []


def _persist_country_update(runtime, entry_key, country):
    storage = runtime.get_storage()
    latest_entries = list(runtime.load_entries(storage))
    merged_entries, changed = merge_country_tag_updates(latest_entries, {entry_key: country})
    if changed:
        runtime.save_entries(merged_entries, storage)


def _country_detect_worker_count(app_config, target_count):
    if target_count <= 0:
        return 1
    return min(app_config.country_detect_max_workers, target_count)


def _run_country_detect_job(runtime, job):
    try:
        app_config = runtime.load_app_config()
        max_workers = _country_detect_worker_count(app_config, len(job.target_entries))
        with job.lock:
            job.status = "running"
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(resolve_entry_country_tag, app_config, entry): (index, entry)
                for index, entry in enumerate(job.target_entries)
            }
            for future in as_completed(future_map):
                index, entry = future_map[future]
                previous_country = entry_country_tag(entry)
                try:
                    country = future.result()
                except Exception as exc:  # pragma: no cover - integration path exercised in tests
                    with job.lock:
                        job.completed += 1
                        job.results[index]["status"] = "error"
                        job.results[index]["message"] = t(
                            "proxies_country_detect_failed_item",
                            job.locale,
                            error=str(exc),
                        )
                    continue

                _persist_country_update(runtime, entry.key, country)
                changed = country != previous_country
                with job.lock:
                    job.completed += 1
                    job.success_count += 1
                    if changed:
                        job.updated_count += 1
                    job.results[index]["status"] = "ok"
                    job.results[index]["country"] = country
                    job.results[index]["message"] = _country_result_message(
                        job.locale,
                        country,
                        changed,
                    )

        with job.lock:
            job.status = "completed"
    except Exception as exc:  # pragma: no cover - guarded by integration tests
        with job.lock:
            job.status = "failed"
            job.error = str(exc)


def _create_country_detect_job(runtime, locale, target_entries):
    job = _CountryDetectJob(
        job_id=uuid.uuid4().hex,
        locale=locale,
        target_entries=list(target_entries),
        results=[
            {
                "key": entry.key,
                "label": entry.label,
                "status": "pending",
                "country": entry_country_tag(entry),
                "message": "",
            }
            for entry in target_entries
        ],
    )
    return _launch_background_job(
        job,
        job_store=_COUNTRY_DETECT_JOBS,
        jobs_lock=_COUNTRY_DETECT_JOBS_LOCK,
        ttl_seconds=COUNTRY_DETECT_JOB_TTL_SECONDS,
        target=_run_country_detect_job,
        args=(runtime, job),
    )


def register_proxy_routes(blueprint, runtime):
    """Register proxy CRUD, import, and batch routes."""

    @blueprint.get("/dashboard/proxies")
    def proxy_list():
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        state = _build_proxy_list_state(
            ui.locale,
            runtime.load_entries(runtime.get_storage()),
            request.args.get("source", "all"),
            request.args.get("country", ""),
            request.args.get("page", "1"),
            runtime.proxies_per_page,
        )
        content = render_template("proxies/list.html", **state)
        return runtime.build_page_response(
            title=t("proxies_title", ui.locale),
            content=content,
            active_nav="nav_proxies",
            ui=ui,
            extra_scripts=admin_resources.load_template_source("proxies/scripts.js"),
        )

    @blueprint.post("/dashboard/proxies/tags/country/detect")
    def proxy_country_detect_start():
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        storage = runtime.get_storage()
        entries = list(runtime.load_entries(storage))
        selected_keys = [key.strip() for key in request.form.getlist("keys") if key.strip()]
        detect_missing_only = request.form.get("detect_missing_only", "0") == "1"
        target_entries = _resolve_country_detection_targets(
            entries,
            selected_keys,
            detect_missing_only,
        )
        if not target_entries:
            error_message = (
                t("proxies_country_detect_no_missing", ui.locale)
                if detect_missing_only
                else t("proxies_no_selection", ui.locale)
            )
            return _json_response({"ok": False, "error": error_message}, status=400)

        job = _create_country_detect_job(runtime, ui.locale, target_entries)
        return _json_response(
            {
                "ok": True,
                "message": t(
                    "proxies_country_detect_started",
                    ui.locale,
                    count=len(target_entries),
                ),
                "job": job.snapshot(),
            }
        )

    @blueprint.get("/dashboard/proxies/tags/country/detect/<job_id>")
    def proxy_country_detect_status(job_id):
        guard = runtime.require_admin()
        if guard is not None:
            return _json_response({"ok": False, "error": "unauthorized"}, status=401)

        job = _get_country_detect_job(job_id)
        if job is None:
            return _json_response(
                {
                    "ok": False,
                    "error": t(
                        "proxies_country_detect_not_found",
                        runtime.resolve_ui_state().locale,
                    ),
                },
                status=404,
            )
        return _json_response({"ok": True, "job": job.snapshot()})

    @blueprint.get("/dashboard/proxies/add")
    def proxy_add_form():
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        return _render_proxy_form_page(
            runtime,
            ui,
            title=t("proxy_form_add_title", ui.locale),
            action_url="/dashboard/proxies/add",
            submit_label=t("proxy_form_add_btn", ui.locale),
            scheme="http",
            host="",
            port="",
            username="",
            password="",
            prepend_hop="",
            in_random_pool=True,
        )

    @blueprint.get("/dashboard/proxies/import")
    def proxy_import_form():
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        return _render_import_form_page(
            runtime,
            ui,
            form_data={
                "default_scheme": "http",
                "default_username": "",
                "default_password": "",
                "proxy_list_text": "",
                "probe_before_import": False,
            },
        )

    @blueprint.post("/dashboard/proxies/add")
    def proxy_add_submit():
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        scheme = request.form.get("scheme", "http")
        host = request.form.get("host", "").strip()
        port_str = request.form.get("port", "").strip()
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        prepend_hop_raw = request.form.get("prepend_hop", "").strip()
        in_pool = request.form.get("in_random_pool", "1") == "1"

        def _render_error(error_msg):
            return _render_proxy_form_page(
                runtime,
                ui,
                title=t("proxy_form_add_title", ui.locale),
                action_url="/dashboard/proxies/add",
                submit_label=t("proxy_form_add_btn", ui.locale),
                scheme=scheme,
                host=host,
                port=port_str,
                username=username,
                password=password,
                prepend_hop=prepend_hop_raw,
                in_random_pool=in_pool,
                error=error_msg,
            )

        if not host or not port_str:
            return _render_error(t("proxy_form_host_port_required", ui.locale))

        try:
            port = int(port_str)
            if port <= 0 or port > 65535:
                raise ValueError("out of range")
        except ValueError:
            return _render_error(t("proxy_form_port_invalid", ui.locale))

        hops = []
        if prepend_hop_raw:
            try:
                for hop_raw in [item.strip() for item in prepend_hop_raw.split(",") if item.strip()]:
                    hops.append(parse_upstream_hop(hop_raw, scheme, "", ""))
            except ValueError as exc:
                return _render_error(str(exc))

        hops.append(
            UpstreamHop(
                scheme=scheme,
                host=host,
                port=port,
                username=username,
                password=password,
            )
        )
        hops = tuple(hops)
        key = compute_entry_key(hops)
        label = " -> ".join("%s:%s" % (hop.host, hop.port) for hop in hops)

        storage = runtime.get_storage()
        entries = list(runtime.load_entries(storage))
        if any(entry.key == key for entry in entries):
            return _render_error(t("proxy_form_duplicate", ui.locale))

        entries.append(
            UpstreamEntry(
                key=key,
                label=label,
                hops=hops,
                source_tag="manual",
                in_random_pool=in_pool,
            )
        )
        runtime.save_entries(entries, storage)
        runtime.trigger_reload()
        return runtime.redirect(
            build_redirect_location("/dashboard/proxies", msg=t("proxies_added", ui.locale)),
            ui=ui,
        )

    @blueprint.get("/dashboard/proxies/<key>/edit")
    def proxy_edit_form(key):
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        storage = runtime.get_storage()
        entries = runtime.load_entries(storage)
        entry = next((candidate for candidate in entries if candidate.key == key), None)
        if entry is None:
            return runtime.redirect(
                build_redirect_location(
                    "/dashboard/proxies",
                    error=t("proxies_not_found", ui.locale),
                ),
                ui=ui,
            )

        hop = entry.last_hop
        return _render_proxy_form_page(
            runtime,
            ui,
            title=t("proxy_form_edit_title", ui.locale),
            action_url="/dashboard/proxies/%s/edit" % key,
            submit_label=t("proxy_form_save_btn", ui.locale),
            scheme=hop.scheme,
            host=hop.host,
            port=str(hop.port),
            username=hop.username,
            password=hop.password,
            prepend_hop=_prepend_hop_value(entry.hops),
            in_random_pool=entry.in_random_pool,
            proxy_summary=_build_proxy_summary(entry),
        )

    @blueprint.post("/dashboard/proxies/<key>/edit")
    def proxy_edit_submit(key):
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        storage = runtime.get_storage()
        entries = list(runtime.load_entries(storage))
        current_entry = next((entry for entry in entries if entry.key == key), None)
        if current_entry is None:
            return runtime.redirect(
                build_redirect_location(
                    "/dashboard/proxies",
                    error=t("proxies_not_found", ui.locale),
                ),
                ui=ui,
            )

        scheme = request.form.get("scheme", "http")
        host = request.form.get("host", "").strip()
        port_str = request.form.get("port", "").strip()
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        prepend_hop_raw = request.form.get("prepend_hop", "").strip()
        in_pool = request.form.get("in_random_pool", "1") == "1"

        def _render_error(error_msg):
            return _render_proxy_form_page(
                runtime,
                ui,
                title=t("proxy_form_edit_title", ui.locale),
                action_url="/dashboard/proxies/%s/edit" % key,
                submit_label=t("proxy_form_save_btn", ui.locale),
                scheme=scheme,
                host=host,
                port=port_str,
                username=username,
                password=password,
                prepend_hop=prepend_hop_raw,
                in_random_pool=in_pool,
                proxy_summary=_build_proxy_summary(current_entry),
                error=error_msg,
            )

        if not host or not port_str:
            return _render_error(t("proxy_form_host_port_required", ui.locale))

        try:
            port = int(port_str)
            if port <= 0 or port > 65535:
                raise ValueError("out of range")
        except ValueError:
            return _render_error(t("proxy_form_port_invalid", ui.locale))

        hops = []
        if prepend_hop_raw:
            try:
                for hop_raw in [item.strip() for item in prepend_hop_raw.split(",") if item.strip()]:
                    hops.append(parse_upstream_hop(hop_raw, scheme, "", ""))
            except ValueError as exc:
                return _render_error(str(exc))

        hops.append(
            UpstreamHop(
                scheme=scheme,
                host=host,
                port=port,
                username=username,
                password=password,
            )
        )
        hops = tuple(hops)
        new_key = compute_entry_key(hops)
        label = " -> ".join("%s:%s" % (hop.host, hop.port) for hop in hops)

        if any(entry.key == new_key and entry.key != key for entry in entries):
            return _render_error(t("proxy_form_duplicate", ui.locale))

        found = False
        for idx, entry in enumerate(entries):
            if entry.key == key:
                next_entry = entry
                if entry.hops != hops:
                    next_entry = without_entry_tags(entry, COUNTRY_TAG)
                entries[idx] = UpstreamEntry(
                    key=new_key,
                    label=label,
                    hops=hops,
                    source_tag=entry.source_tag,
                    in_random_pool=in_pool,
                    tags=next_entry.tags,
                )
                found = True
                break

        if not found:
            return runtime.redirect(
                build_redirect_location(
                    "/dashboard/proxies",
                    error=t("proxies_not_found", ui.locale),
                ),
                ui=ui,
            )

        runtime.save_entries(entries, storage)
        runtime.trigger_reload()
        return runtime.redirect(
            build_redirect_location("/dashboard/proxies", msg=t("proxies_updated", ui.locale)),
            ui=ui,
        )

    @blueprint.get("/dashboard/proxies/batch")
    def batch_form():
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        storage = runtime.get_storage()
        params = load_batch_params(storage)
        return _render_batch_form_page(runtime, ui, form_data=params)

    @blueprint.post("/dashboard/proxies/import")
    def proxy_import_submit():
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        form_data = _build_import_form_data()

        def _render_error(error_msg):
            return _render_import_form_page(
                runtime,
                ui,
                form_data=form_data,
                error=error_msg,
            )

        try:
            parsed_entries = _parse_import_entries(form_data, ui.locale)
        except ValueError as exc:
            return _render_error(str(exc))

        storage = runtime.get_storage()
        existing = list(runtime.load_entries(storage))
        merged, imported_count = _append_manual_entries(
            existing,
            [entry for _line_number, entry in parsed_entries],
        )
        if imported_count == 0:
            return _render_error(t("import_no_new_entries", ui.locale))
        runtime.save_entries(merged, storage)
        runtime.trigger_reload()
        return runtime.redirect(
            build_redirect_location(
                "/dashboard/proxies",
                msg=t("import_completed", ui.locale, count=imported_count),
            ),
            ui=ui,
        )

    @blueprint.post("/dashboard/proxies/import/check")
    def proxy_import_check_start():
        guard = runtime.require_admin()
        if guard is not None:
            return _json_response({"ok": False, "error": "unauthorized"}, status=401)

        ui = runtime.resolve_ui_state()
        form_data = _build_import_form_data()
        try:
            parsed_entries = _parse_import_entries(form_data, ui.locale)
        except ValueError as exc:
            return _json_response({"ok": False, "error": str(exc)}, status=400)

        job = _create_import_check_job(runtime, ui.locale, parsed_entries)
        return _json_response({"ok": True, "job": job.snapshot()})

    @blueprint.get("/dashboard/proxies/import/check/<job_id>")
    def proxy_import_check_status(job_id):
        guard = runtime.require_admin()
        if guard is not None:
            return _json_response({"ok": False, "error": "unauthorized"}, status=401)

        job = _get_import_check_job(job_id)
        if job is None:
            return _json_response(
                {"ok": False, "error": t("import_check_not_found", runtime.resolve_ui_state().locale)},
                status=404,
            )
        return _json_response({"ok": True, "job": job.snapshot()})

    @blueprint.post("/dashboard/proxies/import/commit")
    def proxy_import_commit():
        guard = runtime.require_admin()
        if guard is not None:
            return _json_response({"ok": False, "error": "unauthorized"}, status=401)

        ui = runtime.resolve_ui_state()
        job_id = request.form.get("job_id", "").strip()
        if not job_id:
            return _json_response(
                {"ok": False, "error": t("import_check_not_found", ui.locale)},
                status=400,
            )
        job = _get_import_check_job(job_id)
        if job is None:
            return _json_response(
                {"ok": False, "error": t("import_check_not_found", ui.locale)},
                status=404,
            )

        snapshot = job.snapshot()
        if snapshot["status"] not in {"completed", "failed"}:
            return _json_response(
                {"ok": False, "error": t("import_check_still_running", ui.locale)},
                status=409,
            )

        storage = runtime.get_storage()
        existing = list(runtime.load_entries(storage))
        merged, imported_count = _append_manual_entries(existing, job.successful_entries)
        if imported_count == 0:
            return _json_response(
                {"ok": False, "error": t("import_no_new_entries", ui.locale)},
                status=400,
            )
        runtime.save_entries(merged, storage)
        runtime.trigger_reload()
        _remove_job(
            job_id,
            job_store=_IMPORT_CHECK_JOBS,
            jobs_lock=_IMPORT_CHECK_JOBS_LOCK,
        )
        return _json_response(
            {
                "ok": True,
                "message": t("import_completed", ui.locale, count=imported_count),
                "redirect_url": build_redirect_location(
                    "/dashboard/proxies",
                    msg=t("import_completed", ui.locale, count=imported_count),
                ),
            }
        )

    @blueprint.post("/dashboard/proxies/<key>/delete")
    def proxy_delete(key):
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        storage = runtime.get_storage()
        entries = list(runtime.load_entries(storage))
        original_len = len(entries)
        entries = [entry for entry in entries if entry.key != key]
        if len(entries) == original_len:
            return runtime.redirect(
                build_redirect_location(
                    "/dashboard/proxies",
                    error=t("proxies_not_found", ui.locale),
                ),
                ui=ui,
            )

        runtime.save_entries(entries, storage)
        runtime.trigger_reload()
        return runtime.redirect(
            build_redirect_location("/dashboard/proxies", msg=t("proxies_deleted", ui.locale)),
            ui=ui,
        )

    @blueprint.post("/dashboard/proxies/<key>/toggle-pool")
    def proxy_toggle_pool(key):
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        storage = runtime.get_storage()
        entries = list(runtime.load_entries(storage))
        found = False
        for idx, entry in enumerate(entries):
            if entry.key == key:
                entries[idx] = UpstreamEntry(
                    key=entry.key,
                    label=entry.label,
                    hops=entry.hops,
                    source_tag=entry.source_tag,
                    in_random_pool=not entry.in_random_pool,
                    tags=entry.tags,
                )
                found = True
                break

        if not found:
            if _is_ajax_request():
                return _json_response(
                    {"ok": False, "error": t("proxies_not_found", ui.locale)},
                    status=404,
                )
            return runtime.redirect(
                build_redirect_location(
                    "/dashboard/proxies",
                    error=t("proxies_not_found", ui.locale),
                ),
                ui=ui,
            )

        runtime.save_entries(entries, storage)
        runtime.trigger_reload()
        if _is_ajax_request():
            updated_entry = next(entry for entry in entries if entry.key == key)
            state_label = t(
                "proxies_pool_on_short" if updated_entry.in_random_pool else "proxies_pool_off_short",
                ui.locale,
            )
            return _json_response(
                {
                    "ok": True,
                    "message": t("proxies_pool_updated", ui.locale, state=state_label),
                    "entry": {
                        "key": updated_entry.key,
                        "in_random_pool": updated_entry.in_random_pool,
                    },
                }
            )
        return runtime.redirect(build_redirect_location("/dashboard/proxies"), ui=ui)

    @blueprint.post("/dashboard/proxies/batch/delete")
    def batch_delete():
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        keys = set(request.form.getlist("keys"))
        if not keys:
            return runtime.redirect(
                build_redirect_location(
                    "/dashboard/proxies",
                    error=t("proxies_no_selection", ui.locale),
                ),
                ui=ui,
            )

        storage = runtime.get_storage()
        entries = list(runtime.load_entries(storage))
        original_len = len(entries)
        entries = [entry for entry in entries if entry.key not in keys]
        removed = original_len - len(entries)
        runtime.save_entries(entries, storage)
        runtime.trigger_reload()
        return runtime.redirect(
            build_redirect_location(
                "/dashboard/proxies",
                msg=t("proxies_deleted_count", ui.locale, count=removed),
            ),
            ui=ui,
        )

    @blueprint.post("/dashboard/proxies/batch/toggle-pool")
    def batch_toggle_pool():
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        keys = set(request.form.getlist("keys"))
        pool_state = request.form.get("pool_state", "on")
        new_pool_value = pool_state == "on"
        if not keys:
            return runtime.redirect(
                build_redirect_location(
                    "/dashboard/proxies",
                    error=t("proxies_no_selection", ui.locale),
                ),
                ui=ui,
            )

        storage = runtime.get_storage()
        entries = list(runtime.load_entries(storage))
        changed = 0
        for idx, entry in enumerate(entries):
            if entry.key in keys and entry.in_random_pool != new_pool_value:
                entries[idx] = UpstreamEntry(
                    key=entry.key,
                    label=entry.label,
                    hops=entry.hops,
                    source_tag=entry.source_tag,
                    in_random_pool=new_pool_value,
                    tags=entry.tags,
                )
                changed += 1

        runtime.save_entries(entries, storage)
        runtime.trigger_reload()
        return runtime.redirect(
            build_redirect_location(
                "/dashboard/proxies",
                msg=t("proxies_pool_state", ui.locale, state=pool_state.upper(), count=changed),
            ),
            ui=ui,
        )

    @blueprint.post("/dashboard/proxies/batch/generate")
    def batch_generate():
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        form_data = {
            "scheme": request.form.get("scheme", "http"),
            "host": request.form.get("host", "").strip(),
            "username": request.form.get("username", ""),
            "password": request.form.get("password", ""),
            "port_first": request.form.get("port_first", "").strip(),
            "port_last": request.form.get("port_last", "").strip(),
            "prepend_hop": request.form.get("prepend_hop", "").strip(),
            "cycle_first_hop": request.form.get("cycle_first_hop", "").strip(),
        }

        def _render_error(error_msg):
            return _render_batch_form_page(
                runtime,
                ui,
                form_data=form_data,
                error=error_msg,
            )

        if not form_data["host"]:
            return _render_error(t("batch_host_required", ui.locale))

        try:
            port_first = int(form_data["port_first"])
        except (ValueError, TypeError):
            return _render_error(t("batch_port_first_int", ui.locale))

        try:
            port_last = int(form_data["port_last"])
        except (ValueError, TypeError):
            return _render_error(t("batch_port_last_int", ui.locale))

        if port_first > port_last:
            return _render_error(t("batch_port_order", ui.locale))

        if port_last - port_first + 1 > 1000:
            return _render_error(t("batch_range_limit", ui.locale))

        scheme = form_data["scheme"]
        if scheme not in SUPPORTED_UPSTREAM_SCHEMES:
            return _render_error(t("batch_unsupported_scheme", ui.locale, scheme=scheme))

        storage = runtime.get_storage()
        save_batch_params(storage, form_data)

        prepend_hops = [item.strip() for item in form_data["prepend_hop"].split(",") if item.strip()]
        cycle_hops = [item.strip() for item in form_data["cycle_first_hop"].split(",") if item.strip()]

        new_entries = []
        for index, port in enumerate(range(port_first, port_last + 1)):
            hops = []
            if cycle_hops:
                hop_raw = cycle_hops[index % len(cycle_hops)]
                hops.append(parse_upstream_hop(hop_raw, scheme, "", ""))

            for hop_raw in prepend_hops:
                hops.append(parse_upstream_hop(hop_raw, scheme, "", ""))

            hops.append(
                UpstreamHop(
                    scheme=scheme,
                    host=form_data["host"],
                    port=port,
                    username=form_data["username"],
                    password=form_data["password"],
                )
            )

            hops_tuple = tuple(hops)
            key = compute_entry_key(hops_tuple)
            label = " -> ".join("%s:%s" % (hop.host, hop.port) for hop in hops)
            new_entries.append(
                UpstreamEntry(
                    key=key,
                    label=label,
                    hops=hops_tuple,
                    source_tag="auto",
                    in_random_pool=True,
                )
            )

        existing = list(runtime.load_entries(storage))
        merged, _ = _replace_auto_entries(existing, new_entries)

        runtime.save_entries(merged, storage)
        runtime.trigger_reload()
        return runtime.redirect(
            build_redirect_location(
                "/dashboard/proxies",
                msg=t("batch_generated", ui.locale, count=len(new_entries), first=port_first, last=port_last),
            ),
            ui=ui,
        )

    @blueprint.post("/dashboard/proxies/batch/clear")
    def batch_clear():
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        storage = runtime.get_storage()
        entries = list(runtime.load_entries(storage))
        auto_count = sum(1 for entry in entries if entry.source_tag == "auto")
        remaining = [entry for entry in entries if entry.source_tag != "auto"]
        runtime.save_entries(remaining, storage)
        runtime.trigger_reload()
        return runtime.redirect(
            build_redirect_location(
                "/dashboard/proxies",
                msg=t("batch_cleared", ui.locale, count=auto_count),
            ),
            ui=ui,
        )
