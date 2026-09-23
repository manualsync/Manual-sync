"""ERYLOS visual foundation: design tokens, brand marks, icons, and the
stylesheet layer applied on top of the application's own CSS.

Presentation only. Nothing here reads or writes project data. app.py builds
its CSS as FONT_IMPORT + its own rules + FOUNDATION_CSS, and both launch
paths (app.py and the desktop launcher) pass that same CSS, so the two can
never look different.

Direction: graphite and electric violet. The violet is ERYLOS's identity,
not its surface: it marks the logo, primary actions, the active/selected
state, links and focus. Everything else is white, very light grey,
graphite, near-black text and secondary greys.
"""
from __future__ import annotations

import base64

# ---------------------------------------------------------------- tokens
# Every pair a person has to read is listed in CONTRAST_REQUIREMENTS below
# and checked by regressione_visual_foundation_cp4 (WCAG 2.2 AA).
TOKENS = {
    "primary": "#6A48F2",          # primary actions, links, focus
    "primary-hover": "#5634E0",
    "primary-active": "#4A2BC7",
    "primary-soft": "#F0EDFE",     # selected / current backgrounds
    "brand": "#7C5CFF",            # logo and graphics only -- never body text
    "brand-bright": "#9B7CFF",     # the violet on graphite
    "graphite": "#15161C",         # sidebar
    "graphite-hover": "#1E1F27",
    "graphite-active": "#2A2540",
    "text": "#111318",
    "text-secondary": "#555B6E",
    "text-muted": "#6B7185",       # captions; >= 4.5 on white and on the background
    "background": "#F7F8FA",
    "surface": "#FFFFFF",
    "surface-subtle": "#FBFBFC",
    "border": "#E4E6EC",           # decorative separation between surfaces
    "border-strong": "#CFD3DC",
    "border-input": "#858A9B",     # a control's boundary: >= 3:1 (WCAG 1.4.11)
    "accent": "#00807A",           # functional teal: icons and small signals
    "accent-strong": "#006B66",    # the teal when it has to be read as text
    "accent-soft": "#E6F4F3",
    "success": "#0F7A55", "success-soft": "#E9F7F1", "success-line": "#B3E2CD",
    "warning": "#A35A00", "warning-soft": "#FFF4E0", "warning-line": "#F1D3A1",
    "error": "#C0262D", "error-soft": "#FDECEC", "error-line": "#F5C2C4",
    "info": "#3558D6", "info-soft": "#EDF1FE", "info-line": "#C3CFF7",
    "focus": "#6A48F2",
    "sidebar-text": "#C9CCD6",
    "sidebar-muted": "#8E93A6",
    "sidebar-active-text": "#DCD5FF",
    "disabled-text": "#6E7384",
    "disabled-bg": "#F2F3F6",
}

# (foreground, background, minimum ratio, what it is)
CONTRAST_REQUIREMENTS = [
    ("#FFFFFF", "primary", 4.5, "testo dei pulsanti primari"),
    ("#FFFFFF", "primary-hover", 4.5, "pulsante primario al passaggio del mouse"),
    ("#FFFFFF", "primary-active", 4.5, "pulsante primario premuto"),
    ("primary", "surface", 4.5, "link e azioni su bianco"),
    ("primary-hover", "primary-soft", 4.5, "elemento selezionato"),
    ("text", "background", 4.5, "testo principale sullo sfondo"),
    ("text", "surface", 4.5, "testo principale su bianco"),
    ("text-secondary", "background", 4.5, "testo secondario sullo sfondo"),
    ("text-secondary", "surface", 4.5, "testo secondario su bianco"),
    ("text-muted", "background", 4.5, "didascalie sullo sfondo"),
    ("text-muted", "surface", 4.5, "didascalie su bianco"),
    ("sidebar-text", "graphite", 4.5, "voci della sidebar"),
    ("sidebar-muted", "graphite", 4.5, "titoli di gruppo della sidebar"),
    ("sidebar-active-text", "graphite-active", 4.5, "voce attiva della sidebar"),
    ("#FFFFFF", "graphite-hover", 4.5, "voce della sidebar al passaggio del mouse"),
    ("success", "success-soft", 4.5, "avviso Completato"),
    ("warning", "warning-soft", 4.5, "avviso Attenzione"),
    ("error", "error-soft", 4.5, "avviso Bloccato / Errore"),
    ("info", "info-soft", 4.5, "avviso Informazione"),
    ("error", "surface", 4.5, "azione distruttiva"),
    ("accent-strong", "accent-soft", 4.5, "accento letto come testo"),
    ("border-input", "surface", 3.0, "bordo dei campi su bianco"),
    ("border-input", "background", 3.0, "bordo dei campi sullo sfondo"),
    ("focus", "surface", 3.0, "anello di focus su bianco"),
    ("focus", "background", 3.0, "anello di focus sullo sfondo"),
    ("brand-bright", "graphite-active", 3.0, "indicatore della voce attiva"),
    ("disabled-text", "disabled-bg", 3.0, "controllo disattivato, ancora leggibile"),
]


def _hex(value: str) -> str:
    return TOKENS.get(value, value)


def _luminance(value: str) -> float:
    h = _hex(value).lstrip("#")
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(foreground: str, background: str) -> float:
    """WCAG 2.2 contrast ratio between two token names or hex colours."""
    a, b = _luminance(foreground), _luminance(background)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


# ---------------------------------------------------------------- brand
# "Strati": three layers of structured content; the middle one is shifted
# -- the layer being reused and transformed. Master files in brand/.
_LAYERS = ('<polygon points="6,6 24,6 22,10.5 4,10.5" fill="{a}" stroke="{a}"/>'
           '<polygon points="10,14 28,14 26,18.5 8,18.5" fill="{b}" stroke="{b}"/>'
           '<polygon points="6,22 24,22 22,26.5 4,26.5" fill="{a}" stroke="{a}"/>')


def symbol_svg(size: int = 24, *, dark: bool = False) -> str:
    a, b = ("#F2F2F5", TOKENS["brand-bright"]) if dark else (TOKENS["graphite"], TOKENS["brand"])
    return (f'<svg class="ms-symbol" viewBox="0 0 32 32" width="{size}" height="{size}" aria-hidden="true" '
            f'focusable="false"><g stroke-linejoin="round" stroke-width="0.8">'
            + _LAYERS.format(a=a, b=b) + "</g></svg>")


def brand_html() -> str:
    """The header lock-up: symbol and wordmark. The name is not translated."""
    return f'<div class="ms-brandmark" role="img" aria-label="ERYLOS">{symbol_svg(24)}<span>ERYLOS</span></div>'


# ---------------------------------------------------------------- icons
# One family: 24-unit grid, 1.8 stroke, round caps and joins, no fills.
ICON_PATHS = {
    "home": "M3.5 11 12 4l8.5 7M5.5 9.5V20h13V9.5M10 20v-5h4v5",
    "content": "M7 3h7l5 5v13H7zM14 3v5h5M10 13h6M10 17h6",
    "changes": ("M6 3.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5zM18 15.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5z"
                "M6 8.5V13a4 4 0 0 0 4 4h5.5M18 15.5V11a4 4 0 0 0-4-4H8.5"),
    "translation": "M4 5h8M8 3v2M5.5 5c.8 3.4 3.2 6 6.5 7.5M10.5 5C9.7 8.4 7.3 11 4 12.5M13 21l4-9 4 9M14.5 17.5h5",
    "compliance": "M12 3l7 3v5.5c0 4.4-2.9 8-7 9.5-4.1-1.5-7-5.1-7-9.5V6zM9 12l2 2 4-4.5",
    "publishing": "M12 15V4M8 8l4-4 4 4M5 13.5V20h14v-6.5",
    "settings": "M4 7h9M17 7h3M4 17h3M11 17h9M15 5v4M9 15v4",
    "arrow": "M5 12h14M13 6l6 6-6 6",
    "chevron": "M6 9l6 6 6-6",
}


def icon_svg(name: str, size: int = 18) -> str:
    """An icon inline in HTML, drawn in the surrounding text colour."""
    return (f'<svg class="ms-icon" viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" '
            f'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" '
            f'aria-hidden="true" focusable="false"><path d="{ICON_PATHS[name]}"/></svg>')


def _mask(name: str) -> str:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="black" '
           f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="{ICON_PATHS[name]}"/></svg>')
    return "url(data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii") + ")"


# Sidebar entry -> icon. The entries are words only; the stylesheet draws
# each icon from the entry's elem_id, in the entry's own text colour.
NAV_ICONS = {
    "ms-nav-home": "home", "ms-nav-content": "content", "ms-nav-changes": "changes",
    "ms-nav-translation": "translation", "ms-nav-compliance": "compliance",
    "ms-nav-publishing": "publishing", "ms-nav-settings": "settings",
}

# ---------------------------------------------------------------- typography
# Plus Jakarta Sans for everything people read; JetBrains Mono only for
# identifiers, codes and technical values. Loaded from Google Fonts with a
# system fallback -- offline, the interface falls back to Segoe UI.
FONT_IMPORT = ("@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500"
               "&family=Plus+Jakarta+Sans:wght@400;500;600&display=swap');\n")
FONT_SANS = "'Plus Jakarta Sans','Segoe UI',system-ui,-apple-system,sans-serif"
FONT_MONO = "'JetBrains Mono',Consolas,'Cascadia Mono',ui-monospace,monospace"
TYPE_SCALE = {  # name: (size px, line height px, weight)
    "h1": (24, 32, 600), "h2": (18, 26, 600), "h3": (15, 22, 600),
    "body": (14, 21, 400), "secondary": (13, 19, 400), "caption": (12, 16, 500),
}
MIN_FONT_PX = 12
WEIGHTS = (400, 500, 600)


def _vars() -> str:
    return "".join(f"--e-{k}:{v};" for k, v in TOKENS.items())


def _chevron_rule() -> str:
    # Gradio draws the accordion state with a "▼" character; the same
    # family's chevron replaces it (Gradio still rotates it open/closed).
    m = _mask("chevron")
    return (".gradio-container .label-wrap .icon{font-size:0!important;width:16px;height:16px;display:inline-block;"
            f"background:currentColor;-webkit-mask:{m} center/16px 16px no-repeat;mask:{m} center/16px 16px no-repeat;"
            "color:var(--e-text-secondary)}\n")


def _nav_icon_rules() -> str:
    return "".join(f"#ms-sidebar #{eid}::before{{-webkit-mask-image:{_mask(name)};mask-image:{_mask(name)}}}\n"
                   for eid, name in NAV_ICONS.items())


T = TOKENS
FOUNDATION_CSS = (
    "\n/* ================================================================\n"
    "   ERYLOS visual foundation (Checkpoint 4) -- the last layer, so it wins.\n"
    "   ================================================================ */\n"
    f":root,.gradio-container{{{_vars()}"
    f"--font:{FONT_SANS}!important;--font-mono:{FONT_MONO}!important;"
    "--body-background-fill:var(--e-background)!important;--body-text-color:var(--e-text)!important;"
    "--body-text-color-subdued:var(--e-text-secondary)!important;--color-accent:var(--e-primary)!important;"
    "--color-accent-soft:var(--e-primary-soft)!important;--link-text-color:var(--e-primary-hover)!important;"
    "--block-background-fill:var(--e-surface)!important;--block-border-color:var(--e-border)!important;"
    "--block-radius:12px!important;--block-shadow:none!important;--block-label-text-color:var(--e-text-secondary)!important;"
    "--block-info-text-color:var(--e-text-muted)!important;--block-title-text-color:var(--e-text)!important;"
    "--input-background-fill:var(--e-surface)!important;--input-border-color:var(--e-border-input)!important;"
    # A placeholder is read too (a hint like "Nome e cognome"): muted, never below 4.5:1.
    "--input-placeholder-color:var(--e-text-muted)!important;"
    "--input-border-color-focus:var(--e-focus)!important;--input-radius:8px!important;"
    "--input-shadow-focus:0 0 0 3px rgba(106,72,242,.22)!important;--radius-sm:6px!important;--radius-md:8px!important;"
    "--radius-lg:12px!important;--checkbox-background-color-selected:var(--e-primary)!important;"
    "--checkbox-border-color-selected:var(--e-primary)!important;--table-border-color:var(--e-border)!important;"
    "--table-even-background-fill:var(--e-surface)!important;--table-odd-background-fill:var(--e-surface)!important;"
    "--button-primary-background-fill:var(--e-primary)!important;--button-primary-background-fill-hover:var(--e-primary-hover)!important;"
    "--button-primary-text-color:#FFFFFF!important;--button-primary-border-color:var(--e-primary)!important;"
    "--button-secondary-background-fill:var(--e-surface)!important;--button-secondary-background-fill-hover:#F2F3F6!important;"
    "--button-secondary-text-color:var(--e-text)!important;--button-secondary-border-color:var(--e-border-strong)!important;"
    "--button-cancel-background-fill:var(--e-surface)!important;--button-cancel-background-fill-hover:var(--e-error-soft)!important;"
    "--button-cancel-text-color:var(--e-error)!important;--button-cancel-border-color:var(--e-error-line)!important;"
    # The legacy tokens the application's own rules are written against.
    "--ui-bg:var(--e-background);--ui-surface:var(--e-surface);--ui-surface-2:var(--e-surface-subtle);"
    "--ui-ink:var(--e-text);--ui-muted:var(--e-text-secondary);--ui-line:var(--e-border);"
    "--ui-primary:var(--e-primary);--ui-primary-hover:var(--e-primary-hover);--ui-primary-soft:var(--e-primary-soft);"
    "--ui-brand:var(--e-primary);--ui-brand-soft:var(--e-primary-soft);"
    "--ui-green:var(--e-success);--ui-green-soft:var(--e-success-soft);--ui-red:var(--e-error);"
    "--ui-red-soft:var(--e-error-soft);--ui-amber:var(--e-warning);--ui-amber-soft:var(--e-warning-soft)}\n"
    """
html,body,.gradio-container{background:var(--e-background)!important;color:var(--e-text)!important;
  font-family:var(--font)!important;font-size:14px;line-height:21px}
.gradio-container *{font-family:inherit}
.gradio-container code,.gradio-container pre,.gradio-container kbd,.ms-code{font-family:var(--font-mono)!important}
.gradio-container h1,.ms-pagehead h1{font-size:24px!important;line-height:32px!important;font-weight:600!important;letter-spacing:-.01em!important;color:var(--e-text)!important}
.gradio-container h2{font-size:18px!important;line-height:26px!important;font-weight:600!important}
.gradio-container h3,.ms-section-title{font-size:15px!important;line-height:22px!important;font-weight:600!important;color:var(--e-text)!important}
.gradio-container p,.gradio-container li{font-size:14px;line-height:21px}
.gradio-container b,.gradio-container strong,.gradio-container th{font-weight:600}
.ms-section-sub,.ms-muted{font-size:13px!important;line-height:19px!important;color:var(--e-text-secondary)!important}
a{color:var(--e-primary-hover)}
.gradio-container ::placeholder{color:var(--e-text-muted)!important;opacity:1!important}
a:hover{color:var(--e-primary-active)}

/* ---------------------------------------------------------------- focus, states */
.gradio-container :focus-visible,#ms-sidebar :focus-visible{outline:2px solid var(--e-focus)!important;outline-offset:2px!important}
.gradio-container input:focus,.gradio-container textarea:focus{border-color:var(--e-focus)!important;box-shadow:0 0 0 3px rgba(106,72,242,.22)!important;outline:none!important}
.gradio-container button:disabled,.gradio-container button[disabled]{background:var(--e-disabled-bg)!important;color:var(--e-disabled-text)!important;
  border-color:var(--e-border)!important;box-shadow:none!important;cursor:not-allowed!important;opacity:1!important}
.gradio-container input[type=radio]:checked,.gradio-container input[type=checkbox]:checked{accent-color:var(--e-primary)}
.gradio-container ::selection{background:var(--e-primary-soft);color:var(--e-text)}

/* Gradio's run-time readouts ("processing | 0.4s", queue position, ETA)
   are engineering noise: hidden. A long step still shows its spinner. */
[data-testid="status-tracker"] .progress-text,[data-testid="status-tracker"] .meta-text,
[data-testid="status-tracker"] .meta-text-center,[data-testid="status-tracker"] .eta-bar,
[data-testid="status-tracker"] .progress-level,[data-testid="status-tracker"] .timer{display:none!important}
[data-testid="status-tracker"].wrap.default{background:rgba(255,255,255,.55)!important;border:0!important}

/* ---------------------------------------------------------------- layout */
/* Gradio centres the app in a 1280px column and then shifts it right by the
   sidebar's overlap + 30px. A full-width frame instead: the header meets the
   sidebar, and the content column (max 1440px, centred) is the product's. */
.gradio-container>.main.app{max-width:none!important;margin:0!important;padding:0!important}
#workspace-tabs{max-width:1440px!important;margin:0 auto!important;padding:0 32px 40px 2px!important;width:100%!important}
/* The Gradio tab strip is replaced by the sidebar; it no longer reserves
   its 32px band either (sub-tabs inside a page are not affected). */
#workspace-tabs>.tab-wrapper{display:none!important}
/* An HTML slot with nothing in it (a notice not raised yet) takes no room. */
#workspace-tabs .block:has(>.html-container>.prose:empty){display:none!important}
.gradio-container .main,.gradio-container main{min-width:0}
.gradio-container .block{min-width:0;border-radius:12px}
.ms-pagebar{margin-top:0!important}
.ms-pagehead{padding:8px 0 12px!important;margin-bottom:16px!important;border-bottom:1px solid var(--e-border)!important}
.ms-pagehead p{font-size:13px!important;line-height:19px!important;color:var(--e-text-secondary)!important;margin-top:4px!important}
.ms-page-kicker{font-size:12px!important;line-height:16px!important;font-weight:600!important;letter-spacing:.06em!important;color:var(--e-text-muted)!important}

/* ---------------------------------------------------------------- header: one 56px bar */
.ms-ctxbar{position:sticky!important;top:0!important;z-index:30!important;min-height:56px!important;height:56px;margin:0 0 8px -30px!important;
  width:auto!important;padding:0 32px!important;background:var(--e-surface)!important;border:0!important;border-bottom:1px solid var(--e-border)!important;
  border-radius:0!important;box-shadow:none!important;align-items:center!important;gap:16px!important;flex-wrap:nowrap!important}
.ms-ctxbar>.form,.ms-ctxbar .form{display:flex!important;flex-direction:row!important;flex-wrap:nowrap!important;gap:16px!important;
  align-items:center!important;background:transparent!important;border:0!important;box-shadow:none!important;min-width:0!important;
  padding:0!important;overflow:visible!important}
.ms-ctxbar .block,.ms-ctxbar fieldset.block{background:transparent!important;border:0!important;box-shadow:none!important;padding:0!important;
  position:relative!important;min-width:0!important;width:auto!important}
.ms-ctxbar .container,.ms-ctxbar label.container{display:flex!important;flex-direction:row!important;align-items:center!important;gap:8px!important;margin:0!important}
.ms-ctxbar [data-testid="block-info"]{font-size:12px!important;line-height:16px!important;font-weight:500!important;color:var(--e-text-muted)!important;
  margin:0!important;white-space:nowrap!important;padding:0!important}
/* The explanation under each header label becomes a tooltip. */
.ms-ctxbar [data-testid="block-info"].has-info+div{display:none!important;position:absolute!important;top:calc(100% + 8px)!important;left:0!important;z-index:50!important;
  width:max-content!important;max-width:280px!important;padding:8px 10px!important;border-radius:6px!important;background:var(--e-graphite)!important;
  box-shadow:0 8px 24px rgba(17,19,24,.18)!important}
.ms-ctxbar [data-testid="block-info"].has-info+div p{color:#FFFFFF!important;font-size:12px!important;line-height:16px!important;margin:0!important}
.ms-ctxbar .block:hover [data-testid="block-info"]+div,.ms-ctxbar .block:focus-within [data-testid="block-info"]+div{display:block!important}
/* .full is Gradio's loading overlay, which also carries .wrap: not a field. */
.ms-ctxbar .container>.wrap,.ms-ctxbar .wrap-inner,.ms-ctxbar textarea{min-height:34px!important;height:34px!important}
.ms-ctxbar .container>.wrap{flex:1 1 auto!important;min-width:0!important;border:1px solid var(--e-border-input)!important;border-radius:8px!important;
  background:var(--e-surface)!important;padding:0 8px!important}
.ms-ctxbar .container>.input-container{flex:1 1 auto!important;min-width:0!important}
.ms-ctxbar .container>.wrap:focus-within{border-color:var(--e-focus)!important;box-shadow:0 0 0 3px rgba(106,72,242,.22)!important}
.ms-ctxbar input[role=listbox]{font-size:13px!important;text-overflow:ellipsis!important;padding-right:20px!important}
.ms-ctxbar .wrap-inner{padding:0!important}
.ms-ctxbar textarea{border:1px solid var(--e-border-input)!important;border-radius:8px!important;padding:6px 10px!important;font-size:13px!important;
  line-height:20px!important;resize:none!important;overflow:hidden!important}
#ms-project-picker{flex:1 1 320px!important;max-width:420px!important}
#ms-publication-picker{flex:1 1 260px!important;max-width:340px!important}
#ms-operator{flex:0 1 230px!important;max-width:260px!important;margin-left:auto!important}
#ms-brand-head{flex:0 0 auto!important;width:auto!important;min-width:0!important;border:0!important;background:transparent!important;padding:0!important}
.ms-brandmark{display:flex;align-items:center;gap:10px;height:34px;color:var(--e-text)}
.ms-brandmark span{font-size:16px;font-weight:600;letter-spacing:.12em;line-height:1}
/* Interface language: a two-segment switch, IT | EN. The full language name
   stays in the markup for screen readers. */
#ms-ui-lang{flex:0 0 auto!important;display:flex!important;align-items:center!important;gap:8px!important}
#ms-ui-lang>[data-testid="block-info"]{position:absolute!important;width:1px!important;height:1px!important;overflow:hidden!important;clip:rect(0 0 0 0)!important}
#ms-ui-lang .wrap:not(.full){display:flex!important;gap:0!important;border:1px solid var(--e-border-input)!important;border-radius:8px!important;overflow:hidden!important;
  height:34px!important;min-height:34px!important;padding:0!important;background:var(--e-surface)!important}
#ms-ui-lang label{margin:0!important;border:0!important;border-radius:0!important;background:transparent!important;padding:0 12px!important;
  height:32px!important;display:flex!important;align-items:center!important;box-shadow:none!important;cursor:pointer!important}
#ms-ui-lang label input{position:absolute!important;opacity:0!important;pointer-events:none!important}
#ms-ui-lang label:focus-within{outline:2px solid var(--e-focus)!important;outline-offset:-2px!important}
#ms-ui-lang label span{font-size:0!important;margin:0!important}
#ms-ui-lang label[data-testid="Italiano-radio-label"] span::after{content:"IT";font-size:13px;font-weight:600}
#ms-ui-lang label[data-testid="English-radio-label"] span::after{content:"EN";font-size:13px;font-weight:600}
#ms-ui-lang label.selected{background:var(--e-primary-soft)!important;color:var(--e-primary-hover)!important}
#ms-ui-lang label:not(.selected){color:var(--e-text-secondary)!important}

/* ---------------------------------------------------------------- sidebar: graphite, compact */
#ms-sidebar{background:var(--e-graphite)!important;border-right:0!important;padding:0!important;overflow:visible!important}
/* The open/close tab lives just OUTSIDE a closed sidebar: with overflow
   hidden (the old rule) it was clipped, so a closed sidebar could not be
   reopened. The entries scroll inside instead. */
#ms-sidebar .sidebar-content{overflow-y:auto!important;overflow-x:hidden!important;height:100%!important;box-sizing:border-box!important}
/* Desktop: a 224px rail. Up to 768px Gradio turns the sidebar into an overlay
   opened from its tab; that behaviour is kept (see the responsive block). */
@media (min-width:769px){#ms-sidebar{width:224px!important;min-width:224px!important}}
#ms-sidebar .sidebar-content,#ms-sidebar .column{background:transparent!important;gap:2px!important}
#ms-sidebar .sidebar-content{padding:64px 12px 16px!important}
#ms-sidebar .block{background:transparent!important;border:0!important;padding:0!important}
/* The collapse control is a button too: it must not take the entries' in-flow
   shape, or it leaves the viewport (position/padding restored). */
#ms-sidebar .toggle-button{position:absolute!important;padding:0!important;min-height:0!important;min-width:0!important;
  justify-content:center!important;background:var(--e-graphite)!important;color:var(--e-sidebar-text)!important;border-color:#2B2C35!important}
#ms-sidebar .toggle-button::before{content:none!important}
#ms-sidebar.open .toggle-button{left:auto!important;right:12px!important;top:12px!important;width:32px!important;height:32px!important;
  border:0!important;border-radius:8px!important;background:transparent!important}
#ms-sidebar.open .toggle-button:hover{background:var(--e-graphite-hover)!important}
/* Closed: a small graphite tab at the top-left, and the header and page make
   room for it (Gradio no longer pushes the content right). */
#ms-sidebar:not(.open) .toggle-button{top:12px!important;width:32px!important;height:32px!important;
  border:0!important;border-radius:0 8px 8px 0!important;background:var(--e-graphite)!important}
.sidebar-parent:not(:has(#ms-sidebar.open)) .ms-ctxbar{margin-left:0!important;padding-left:48px!important}
.sidebar-parent:not(:has(#ms-sidebar.open)) #workspace-tabs{padding-left:32px!important}
#ms-sidebar .toggle-button .chevron-left,#ms-sidebar .toggle-button .chevron-right{border-color:var(--e-sidebar-text)!important}
#ms-sidebar .ms-side-section{font-size:12px!important;line-height:16px!important;font-weight:600!important;letter-spacing:.06em!important;
  color:var(--e-sidebar-muted)!important;padding:16px 12px 6px!important;text-transform:uppercase}
#ms-sidebar button{position:relative!important;height:36px!important;min-height:36px!important;padding:0 10px 0 40px!important;border-radius:8px!important;
  border:0!important;background:transparent!important;color:var(--e-sidebar-text)!important;font-size:14px!important;font-weight:500!important;
  box-shadow:none!important;justify-content:flex-start!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important}
#ms-sidebar button.secondary{background:transparent!important;color:var(--e-sidebar-text)!important}
#ms-sidebar button:hover,#ms-sidebar button.secondary:hover{background:var(--e-graphite-hover)!important;color:#FFFFFF!important}
#ms-sidebar button.primary{background:var(--e-graphite-active)!important;color:var(--e-sidebar-active-text)!important;font-weight:600!important;
  box-shadow:inset 3px 0 0 var(--e-brand-bright)!important}
#ms-sidebar button[id^="ms-nav-"]::before{content:"";position:absolute;left:12px;top:50%;width:18px;height:18px;margin-top:-9px;background:currentColor;
  -webkit-mask-repeat:no-repeat;mask-repeat:no-repeat;-webkit-mask-size:18px 18px;mask-size:18px 18px;-webkit-mask-position:center;mask-position:center}
/* The project is already in the header: the sidebar copy is not shown. */
#ms-sidebar .block:has(.ms-sidebar-context){display:none!important}
"""
    + _nav_icon_rules()
    + """
/* ---------------------------------------------------------------- buttons */
.gradio-container button.primary,.gradio-container button.secondary,.gradio-container button.stop{
  min-height:36px!important;height:auto!important;padding:0 16px!important;border-radius:8px!important;font-size:14px!important;
  line-height:20px!important;font-weight:600!important;box-shadow:none!important;border-width:1px!important;border-style:solid!important;
  min-width:88px!important;max-width:100%!important;transition:background-color .12s,border-color .12s,color .12s}
.gradio-container button.sm{min-height:32px!important;padding:0 12px!important;font-size:13px!important}
.gradio-container button.primary{background:var(--e-primary)!important;border-color:var(--e-primary)!important;color:#FFFFFF!important}
.gradio-container button.primary:hover{background:var(--e-primary-hover)!important;border-color:var(--e-primary-hover)!important}
.gradio-container button.primary:active{background:var(--e-primary-active)!important}
.gradio-container button.secondary{background:var(--e-surface)!important;border-color:var(--e-border-strong)!important;color:var(--e-text)!important}
.gradio-container button.secondary:hover{background:#F2F3F6!important}
.gradio-container button.stop,.gradio-container button.ms-danger{background:var(--e-surface)!important;border-color:var(--e-error-line)!important;color:var(--e-error)!important}
.gradio-container button.stop:hover,.gradio-container button.ms-danger:hover{background:var(--e-error-soft)!important}
/* A page's own primary action is a step larger. */
.gradio-container #workspace-tabs button.primary.lg{min-height:40px!important}
/* No more 1,000-pixel buttons: an action is as wide as its words, and
   pairs of actions sit side by side instead of splitting the row. */
#workspace-tabs .column>button.primary,#workspace-tabs .column>button.secondary,#workspace-tabs .column>button.stop,
#workspace-tabs .form>button{width:auto!important;align-self:flex-start!important;flex:0 0 auto!important}
#workspace-tabs .row>button.primary,#workspace-tabs .row>button.secondary,#workspace-tabs .row>button.stop{flex:0 0 auto!important;width:auto!important}
#workspace-tabs .row:has(>button){justify-content:flex-start!important;gap:8px!important;flex-wrap:wrap!important}
/* Destructive actions keep their distance from the ordinary ones. */
#workspace-tabs .row>button.ms-danger,#workspace-tabs .row>button.stop{margin-left:16px!important}

/* Sub-navigation of a two-page section. */
.ms-pane-left,.ms-pane-right{flex-wrap:nowrap!important}
.ms-subnav{gap:4px!important;margin:-4px 0 16px!important;padding:4px!important;width:max-content!important;max-width:100%!important;
  background:#EEEFF3!important;border-radius:10px!important;flex-wrap:wrap!important}
.ms-subnav>button.secondary,.ms-subnav>button.secondary:hover{background:transparent!important;border-color:transparent!important;
  color:var(--e-text-secondary)!important;min-width:0!important;margin:0!important}
.ms-subnav>button.secondary:hover{color:var(--e-text)!important;background:rgba(255,255,255,.7)!important}
.ms-subnav>button.is-current,.ms-subnav>button.is-current:disabled{background:var(--e-surface)!important;color:var(--e-text)!important;
  border-color:var(--e-border)!important;cursor:default!important;box-shadow:0 1px 2px rgba(17,19,24,.06)!important}

/* ---------------------------------------------------------------- inputs */
.gradio-container textarea,.gradio-container input[type=text],.gradio-container input[type=number],.gradio-container input:not([type]){
  font-size:14px!important;color:var(--e-text)!important}
.gradio-container label>span,.gradio-container [data-testid="block-info"]{font-size:13px!important;font-weight:500!important;color:var(--e-text-secondary)!important}

/* ---------------------------------------------------------------- cards */
.ms-panel,.ms-settings-card,.ms-create-project-panel,.ms-card,.ms-quick-card,.ms-project-card{
  background:var(--e-surface)!important;border:1px solid var(--e-border)!important;border-radius:12px!important;box-shadow:none!important}
.ms-panel,.ms-settings-card,.ms-create-project-panel{padding:20px 24px!important}
.ms-card{padding:16px 20px!important}
.ms-project-card{padding:20px 24px!important;border-left:1px solid var(--e-border)!important}
.ms-eyebrow{background:var(--e-primary-soft)!important;color:var(--e-primary-hover)!important;border:0!important;font-size:12px!important;
  font-weight:600!important;padding:2px 10px!important;letter-spacing:.04em!important}
.ms-project-card h2{font-size:18px!important;line-height:26px!important;margin:8px 0 2px!important}
.ms-kpi{background:var(--e-surface-subtle)!important;border:1px solid var(--e-border)!important;border-radius:10px!important;padding:12px 14px!important}
.ms-kpi-label{font-size:12px!important;font-weight:500!important;color:var(--e-text-secondary)!important}
.ms-kpi b{font-size:22px!important;font-weight:600!important;font-variant-numeric:tabular-nums}
.ms-kpi small{font-size:12px!important;color:var(--e-text-muted)!important}
.ms-next{background:var(--e-surface-subtle)!important;border:1px solid var(--e-border)!important;color:var(--e-text)!important;
  font-size:13px!important;border-radius:10px!important}
.ms-next b{font-weight:600!important}
.ms-next-icon{border:0!important;background:var(--e-accent-soft)!important;color:var(--e-accent)!important}
/* Quick actions: four equal cards on a desktop, two on narrower screens. */
.ms-quick-grid{display:grid!important;grid-template-columns:repeat(4,minmax(0,1fr))!important;gap:16px!important;align-items:stretch!important}
.ms-quick-head{padding:0!important;margin-top:8px!important}
.ms-quick-grid>.column{min-width:0!important;width:auto!important}
.ms-quick-card{padding:20px!important;min-height:0!important}
.ms-quick-card:hover{border-color:var(--e-border-strong)!important;box-shadow:0 2px 8px rgba(17,19,24,.05)!important}
.ms-quick-card.brand{background:var(--e-surface)!important;border-color:var(--e-border)!important}
.ms-quick-icon{width:36px!important;height:36px!important;border-radius:10px!important;background:var(--e-primary-soft)!important;
  color:var(--e-primary-hover)!important;margin-bottom:12px!important}
.ms-quick-card.brand .ms-quick-icon{background:var(--e-primary-soft)!important;color:var(--e-primary-hover)!important}
.ms-quick-title{font-size:15px!important;font-weight:600!important;color:var(--e-text)!important}
.ms-quick-desc{font-size:13px!important;line-height:19px!important;color:var(--e-text-secondary)!important;min-height:38px!important}
.ms-card-cta{margin-top:auto!important;min-height:32px!important;font-size:13px!important;align-self:flex-start!important;width:auto!important}
.ms-new-project-btn{max-width:none!important;min-width:0!important;width:auto!important;white-space:nowrap!important;margin-top:0!important;font-size:14px!important;padding:0 16px!important}
.ms-pagebar{align-items:flex-end!important}

/* ---------------------------------------------------------------- tables */
.gradio-container .table-wrap table,.gradio-container .table-wrap td,.gradio-container .table-wrap th,
.gradio-container .table-wrap td span,.gradio-container .table-wrap th span{font-family:var(--font)!important;font-size:13px!important;
  font-variant-numeric:tabular-nums}
.gradio-container .table-wrap thead th{position:sticky!important;top:0!important;z-index:2!important;background:#F4F5F8!important;
  color:var(--e-text-secondary)!important;font-weight:600!important;font-size:12px!important}
.gradio-container .table-wrap td{padding:9px 12px!important;min-height:40px;color:var(--e-text)!important;
  border-color:var(--e-border)!important;vertical-align:middle!important}
.gradio-container .table-wrap tr:hover td{background:#FAFAFC!important}
/* A table scrolls inside its own frame, never the page. */
.gradio-container .table-container,.gradio-container .table-wrap{max-width:100%!important;overflow-x:auto!important}
.gradio-container .block:has(.table-wrap){max-width:100%!important;min-width:0!important;overflow:hidden!important}
.gradio-container .cell-menu-button{opacity:.55}

/* ---------------------------------------------------------------- uploads */
/* Gradio's own drop zone: compact, in the product's words and language.
   The page's lang attribute follows the interface language (see the
   persistence script in app.py). */
.gradio-container button[aria-dropeffect].boundedheight{min-height:84px!important;height:84px!important;
  max-height:120px!important;border:1px dashed var(--e-border-input)!important;border-radius:10px!important;background:var(--e-surface-subtle)!important}
.gradio-container button[aria-dropeffect].boundedheight:hover{border-color:var(--e-primary)!important;background:var(--e-primary-soft)!important}
.gradio-container button[aria-dropeffect].boundedheight .wrap{font-size:0!important;line-height:0!important;flex-direction:row!important;gap:10px!important;
  color:var(--e-text-secondary)!important}
.gradio-container button[aria-dropeffect].boundedheight .wrap .or{display:none!important}
.gradio-container button[aria-dropeffect].boundedheight .icon-wrap{width:20px!important;height:20px!important;margin:0!important;color:var(--e-primary)!important}
.gradio-container button[aria-dropeffect].boundedheight .wrap::after{content:"Trascina qui il file oppure fai clic per sceglierlo";font-size:13px;line-height:19px;font-weight:500}
html:lang(en) .gradio-container button[aria-dropeffect].boundedheight .wrap::after{content:"Drop the file here or click to choose it"}
/* An empty result slot (a file not produced yet) is a single line, not a 240px box. */
.gradio-container .block:has(> .empty),.gradio-container .block:has(.empty.unpadded_box){min-height:0!important}
.gradio-container .empty.unpadded_box,.gradio-container .block .empty{min-height:44px!important;height:44px!important;opacity:.6}

/* ---------------------------------------------------------------- accordions */
.gradio-container .label-wrap{font-size:14px!important;font-weight:600!important;color:var(--e-text)!important}
"""
    + _chevron_rule()
    + """
/* ---------------------------------------------------------------- status lines, product card */
/* The notice/badge tones are the application's own rules, re-coloured to
   these tokens; only the pieces introduced here need rules. */
.ms-status-line{display:inline-flex;align-items:center;gap:8px}
.ms-status-line .ms-ux-icon{width:18px;height:18px}
.ms-tr-lang-status,.ms-ic-lang-head{display:flex;align-items:center;gap:6px}
.ms-tr-lang-status .ms-ux-icon,.ms-ic-lang-head .ms-ux-icon{width:16px;height:16px;flex:0 0 auto}
.ms-quick-icon svg,.ms-cp-empty-icon svg{display:block}
.ms-product-info{display:flex;flex-direction:column;gap:4px;margin-top:8px;font-size:13px;line-height:19px}
.ms-product-name{display:flex;align-items:center;gap:8px;font-size:15px;letter-spacing:.08em}
.ms-product-name b{font-weight:600}
.ms-product-build{color:var(--e-text-secondary)}
.ms-code{font-family:var(--font-mono)!important;font-size:12px;color:var(--e-text)}
.ms-context-line{padding:8px 12px;border-radius:8px;background:var(--e-surface);border:1px solid var(--e-border);font-size:13px;line-height:19px;color:var(--e-text-secondary)}
.ms-context-line b{color:var(--e-text)}

/* ---------------------------------------------------------------- responsive */
@media (max-width:1279px){
  .ms-quick-grid{grid-template-columns:repeat(2,minmax(0,1fr))!important}
  #workspace-tabs{padding:0 20px 32px!important}
  .ms-ctxbar{padding:0 20px!important;gap:12px!important}
  .ms-ctxbar>.form,.ms-ctxbar .form{gap:12px!important}
  #ms-project-picker{flex:1 1 260px!important;max-width:340px!important}
  #ms-publication-picker{flex:1 1 220px!important;max-width:280px!important}
}
/* Narrow screens: still ONE 56px bar. The field names become tooltips'
   companions for screen readers only (the values speak for themselves:
   the project, the manual, "Nome e cognome"), the wordmark leaves the
   symbol alone. */
@media (max-width:1023px){
  .ms-ctxbar [data-testid="block-info"]{position:absolute!important;width:1px!important;height:1px!important;
    overflow:hidden!important;clip:rect(0 0 0 0)!important;white-space:nowrap!important}
  .ms-brandmark span{display:none}
  #ms-project-picker{flex:1 1 200px!important;max-width:none!important}
  #ms-publication-picker{flex:1 1 180px!important;max-width:none!important}
  #ms-operator{flex:1 1 150px!important;max-width:220px!important;margin-left:0!important}
  #workspace-tabs{padding:0 16px 24px!important}
}
/* Tablet and below: Gradio's overlay sidebar, kept -- as a 280px panel over
   the page (not a full-screen sheet), opened from a graphite tab at the
   top-left that the header leaves room for. No -30px header offset here:
   Gradio does not push the content when the sidebar is an overlay. */
@media (max-width:768px){
  #ms-sidebar{width:280px!important;min-width:280px!important}
  #ms-sidebar:not(.right){left:-280px!important}
  .ms-ctxbar{margin-left:0!important;padding:0 12px 0 48px!important}
}
@media (max-width:767px){
  .ms-quick-grid{grid-template-columns:minmax(0,1fr)!important}
}
@media (prefers-reduced-motion:reduce){.gradio-container *{transition:none!important;animation-duration:.01ms!important}}
"""
)
