"""Small HTML helpers shared by the Streamlit pages. All text is escaped before it is put in markup."""
import html

import streamlit as st


def esc(value):
    return html.escape("" if value is None else str(value))


def inr(value):
    return "-" if value is None else (f"Rs. {value:,.2f}" if isinstance(value, (int, float)) else str(value))


def show_value(value, unit):
    return inr(value) if unit == "INR" else ("-" if value is None else str(value))


def md(markup):
    st.markdown(markup, unsafe_allow_html=True)


def pill(text, kind):
    return f'<span class="pill {kind}">{esc(text)}</span>'


def table(header, rows, num_cols=()):
    """rows are lists of already-escaped HTML cells."""
    head = "".join(f'<th class="{"num" if i in num_cols else ""}">{h}</th>' for i, h in enumerate(header)) if header else ""
    body = "".join("<tr>" + "".join(f'<td class="{"num" if i in num_cols else ""}">{c}</td>' for i, c in enumerate(r)) + "</tr>"
                   for r in rows)
    return f'<div class="table-wrap"><table>{"<tr>" + head + "</tr>" if head else ""}{body}</table></div>'
