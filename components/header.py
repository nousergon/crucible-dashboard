"""Shared header and footer for the Nous Ergon public site."""

import base64
import os

import streamlit as st

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")

_NAV_LINKS = [
    ("Home", "/", True),
    ("About", "/About", True),
    ("Architecture", "/Architecture", True),
    ("Stack", "/Stack", True),
    ("Retros", "/Retros", True),
    ("Blog", "/blog", True),
    ("GitHub", "https://github.com/nousergon/nousergon-docs", False),
    ("Console", "https://console.nousergon.ai", False),
]


def _build_nav_html(current_page: str) -> str:
    """Build the nav bar HTML with the current page highlighted."""
    parts = []
    for label, href, is_internal in _NAV_LINKS:
        target = "_self" if is_internal else "_blank"
        if label == current_page:
            style = "color: #1a73e8; text-decoration: none; margin: 0 16px; font-weight: 600;"
        else:
            style = "color: #ccc; text-decoration: none; margin: 0 16px;"
        parts.append(f'<a href="{href}" target="{target}" style="{style}">{label}</a>')
    return "\n".join(parts)


def render_header(current_page: str = "Home"):
    """Render the logo and navigation bar."""
    nav_html = _build_nav_html(current_page)
    logo_path = os.path.join(_ASSETS_DIR, "NousErgonLogo_260319.png")

    if os.path.exists(logo_path):
        with open(logo_path, "rb") as f:
            logo_b64 = base64.b64encode(f.read()).decode()
        st.markdown(
            f"""
            <div style="text-align: center; padding: 20px 0 0 0;">
                <img src="data:image/png;base64,{logo_b64}"
                     alt="Nous Ergon: Alpha Engine"
                     style="max-width: 600px; width: 90%; margin-bottom: 8px;" />
                <div style="margin-top: 14px; font-size: 13px; letter-spacing: 1px;">
                    {nav_html}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"""
            <div style="text-align: center; padding: 20px 0 0 0;">
                <h1 style="margin-bottom: 0; font-size: 2.5em; letter-spacing: 2px;">
                    Nous Ergon: Alpha Engine
                </h1>
                <p style="color: #888; font-size: 14px; margin-top: 4px; font-style: italic;">
                    &nu;&omicron;&upsilon;&sigmaf; &epsilon;&rho;&gamma;&omicron;&nu;
                    <span style="color:#666; font-size:12px;">(noose air-gone)</span>
                </p>
                <p style="color: #aaa; font-size: 14px; margin-top: 6px;">
                    Intelligence at work
                </p>
                <p style="color: #999; font-size: 13px; margin-top: 8px;">
                    AI-driven autonomous trading system
                </p>
                <div style="margin-top: 14px; font-size: 13px; letter-spacing: 1px;">
                    {nav_html}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_footer():
    """Render the shared page footer.

    Footer CTAs intentionally narrower than the header nav — three
    secondary destinations chosen to be the "if I'm scrolled to the
    bottom and want one more thing" links: the visual deep-dive
    (Architecture), the source code (GitHub org), and the long-form
    writing (Blog). Header carries the full primary nav.
    """
    st.divider()
    st.markdown(
        """
        <div style="text-align: center; padding: 8px 0 20px 0;">
            <p style="color: #aaa; font-size: 13px; margin-bottom: 6px;">
                <a href="/Architecture"
                   target="_self"
                   style="color: #1a73e8; text-decoration: none; margin: 0 12px;">
                    Architecture
                </a>
                <a href="https://github.com/nousergon/nousergon-docs"
                   target="_blank"
                   style="color: #1a73e8; text-decoration: none; margin: 0 12px;">
                    GitHub
                </a>
                <a href="/blog"
                   target="_self"
                   style="color: #1a73e8; text-decoration: none; margin: 0 12px;">
                    Blog
                </a>
            </p>
            <p style="color: #666; font-size: 12px;">
                Paper trading account &mdash; not financial advice
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )
