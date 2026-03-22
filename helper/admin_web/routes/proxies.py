"""Proxy-management routes for the admin Flask app."""

import math
from urllib.parse import quote as _url_quote

from flask import render_template, request

from i18n import get_translations, t
from persistence import load_batch_params, save_batch_params
from upstream_pool import (
    SUPPORTED_UPSTREAM_SCHEMES,
    UpstreamEntry,
    UpstreamHop,
    compute_entry_key,
    parse_upstream_hop,
)

from .. import resources as admin_resources
from ..app_runtime import build_redirect_location


def _format_hop_uri(hop):
    auth = ""
    if hop.username or hop.password:
        auth = "%s:%s@" % (_url_quote(hop.username), _url_quote(hop.password))
    return "%s://%s%s:%s" % (hop.scheme, auth, hop.host, hop.port)


def _prepend_hop_value(hops):
    if len(hops) <= 1:
        return ""
    return ", ".join(_format_hop_uri(hop) for hop in hops[:-1])


def _build_proxy_list_state(locale, entries, source_filter, page_value, proxies_per_page):
    manual_count = sum(1 for entry in entries if entry.source_tag == "manual")
    auto_count = len(entries) - manual_count
    active_filter = (source_filter or "all").strip().lower()
    if active_filter not in {"all", "manual", "auto"}:
        active_filter = "all"

    if active_filter == "all":
        filtered_entries = entries
    else:
        filtered_entries = [entry for entry in entries if entry.source_tag == active_filter]
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

    def _page_url(page_number, source=None):
        page_source = active_filter if source is None else source
        params = []
        if page_source != "all":
            params.append(("source", page_source))
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
            error=error,
        ),
    )
    return runtime.build_page_response(
        title=title,
        content=content,
        active_nav="nav_proxies",
        ui=ui,
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


def register_proxy_routes(blueprint, runtime):
    """Register proxy CRUD and batch routes."""

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
        )

    @blueprint.post("/dashboard/proxies/<key>/edit")
    def proxy_edit_submit(key):
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

        storage = runtime.get_storage()
        entries = list(runtime.load_entries(storage))
        if any(entry.key == new_key and entry.key != key for entry in entries):
            return _render_error(t("proxy_form_duplicate", ui.locale))

        found = False
        for idx, entry in enumerate(entries):
            if entry.key == key:
                entries[idx] = UpstreamEntry(
                    key=new_key,
                    label=label,
                    hops=hops,
                    source_tag=entry.source_tag,
                    in_random_pool=in_pool,
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
        merged = [entry for entry in existing if entry.source_tag != "auto"]
        existing_keys = {entry.key for entry in merged}
        for entry in new_entries:
            if entry.key not in existing_keys:
                merged.append(entry)
                existing_keys.add(entry.key)

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
