"""Shared admin layout rendering."""

from i18n import DEFAULT_LOCALE, t

from ..i18n_utils import toggle_locale_url
from ..resources import load_static_source
from ..templating import render_template

_NAV_ITEMS = [
    ("nav_dashboard", "/dashboard", "ti-layout-dashboard"),
    ("nav_configuration", "/dashboard/config", "ti-settings"),
    ("nav_proxies", "/dashboard/proxies", "ti-server-2"),
    ("nav_compat", "/dashboard/compat", "ti-plug-connected"),
    ("nav_logs", "/dashboard/logs", "ti-list-details"),
]


def render_tabler_page(
    title,
    content,
    active_nav="",
    extra_head="",
    extra_scripts="",
    locale=None,
    theme=None,
    request_path="",
    request_query=(),
):
    """Wrap content in the shared admin layout."""
    if locale is None:
        locale = DEFAULT_LOCALE
    if theme is None:
        theme = "light"
    translate = lambda key, **kw: t(key, locale, **kw)  # noqa: E731

    sidebar = ""
    topbar = ""
    main_open = ""
    main_close = ""

    if active_nav is not None:
        nav_links = []
        for key, href, icon in _NAV_ITEMS:
            label = translate(key)
            active_cls = ' class="active"' if key == active_nav else ""
            nav_links.append(
                '<li><a%s href="%s"><i class="ti %s"></i><span>%s</span></a></li>'
                % (active_cls, href, icon, label)
            )

        lang_toggle_url = toggle_locale_url(request_path, request_query, locale)
        lang_toggle_label = (
            translate("lang_switch_zh") if locale == "en" else translate("lang_switch_en")
        )
        theme_icon = "ti-moon" if theme == "light" else "ti-sun"
        close_sidebar_onclick = (
            "document.getElementById('sidebar').classList.remove('open');"
            "this.classList.remove('open')"
        )
        toggle_sidebar_onclick = (
            "document.getElementById('sidebar').classList.toggle('open');"
            "document.getElementById('sidebar-overlay').classList.toggle('open')"
        )

        sidebar = """\
<nav class="pp-sidebar" id="sidebar">
  <a class="pp-sidebar-brand" href="/dashboard">
    <i class="ti ti-network"></i> ProxyPool
  </a>
  <ul class="pp-sidebar-nav">%s</ul>
  <div class="pp-sidebar-footer">
    <a class="pp-sidebar-logout" href="/logout">
      <i class="ti ti-logout"></i><span>%s</span>
    </a>
  </div>
</nav>
<div class="pp-sidebar-overlay" id="sidebar-overlay" onclick="%s"></div>
""" % ("".join(nav_links), translate("btn_logout"), close_sidebar_onclick)

        topbar = """\
<div class="pp-topbar">
  <button class="pp-sidebar-toggle" onclick="%s">
    <i class="ti ti-menu-2"></i>
  </button>
  <div style="display:flex;align-items:center;gap:0.5rem;margin-left:auto">
    <a href="%s" class="pp-lang-btn" title="%s">
      <i class="ti ti-language"></i><span>%s</span>
    </a>
    <button class="pp-theme-btn" onclick="toggleTheme()" title="%s">
      <i class="ti %s"></i>
    </button>
  </div>
</div>""" % (
            toggle_sidebar_onclick,
            lang_toggle_url,
            lang_toggle_label,
            lang_toggle_label,
            translate("theme_toggle"),
            theme_icon,
        )
        main_open = '<div class="pp-main">'
        main_close = "</div>"

    html_lang = "zh" if locale == "zh" else "en"
    theme_attr = ' data-theme="%s" data-bs-theme="%s"' % (theme, theme)
    theme_css = "<style>\n%s\n</style>" % load_static_source("css/admin.css")
    shell_js = load_static_source("js/admin-shell.js")
    return render_template(
        "base.html",
        html_lang=html_lang,
        theme_attr=theme_attr,
        title=title,
        theme_css=theme_css,
        extra_head=extra_head,
        sidebar=sidebar,
        main_open=main_open,
        topbar=topbar,
        content=content,
        main_close=main_close,
        shell_js=shell_js,
        extra_scripts=extra_scripts,
    )
