"""Run page layout classes (summary card, tab bar, badges, glossary, wide-table scroll).

Pure CSS written in the colour tokens of ``site_style`` (light/dark, toggle), which the
page also includes through ``site_style.themed()``. No scripts, fonts or network.
"""

BASE = (
        'html{-webkit-text-size-adjust:100%}*,*::before,*::after{box-sizing:border-box}'
        'body{max-width:1150px;margin:0 auto;padding:24px 16px 60px;font:16px/1.55 system-ui,-apple-system,'
        '"Segoe UI",sans-serif;color:var(--fg);background:var(--bg);overflow-wrap:break-word}'
        'a{color:var(--accent)}small,.muted{color:var(--muted)}code,.id{font-family:ui-monospace,SFMono-Regular,'
        'Menlo,monospace;font-size:.92em}h1{margin:.2em 0 .1em;line-height:1.2}h2{margin-top:1.8em}'
        'table{border-collapse:collapse;width:100%;background:var(--card)}td,th{text-align:left;padding:8px 9px;'
        'border-bottom:1px solid var(--line);vertical-align:top}th{font-size:13px;color:var(--muted)}'
        '.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}.scroll table{min-width:640px}'
        '.crumbs{font-size:14px;margin:0 0 10px}'
        '.card{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));background:var(--card);'
        'border:1px solid var(--line);border-radius:10px;overflow:hidden;margin:16px 0}'
        '.tile{background:var(--card);padding:12px 14px;box-shadow:0 0 0 .5px var(--line);overflow-wrap:break-word}.tile .k{font-size:12px;color:var(--muted);'
        'text-transform:uppercase;letter-spacing:.04em}.tile .v{font-size:19px;font-weight:600;margin-top:2px;line-height:1.25}'
        '.tile .s{font-size:13px;color:var(--muted)}'
        '.tabs{display:flex;flex-wrap:wrap;gap:6px;border-bottom:2px solid var(--line);margin:18px 0 0;padding:0}'
        '.tabs a,.tabs span{display:inline-block;padding:9px 14px;border:1px solid var(--line);border-bottom:none;'
        'border-radius:8px 8px 0 0;background:var(--card);text-decoration:none;font-weight:600;margin-bottom:-2px}'
        '.tabs .current{border-bottom:2px solid var(--card);color:var(--fg)}.tabs .off{color:var(--muted);'
        'font-weight:400}'
        '.tabhelp{margin:0;padding:10px 0;list-style:none;font-size:14px}.tabhelp li{margin:3px 0}'
        '.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;'
        'border:1px solid currentColor;white-space:nowrap}.b-good{color:var(--good);background:var(--good-bg)}'
        '.b-warn{color:var(--caution);background:var(--caution-bg)}.b-bad{color:var(--bad);background:var(--bad-bg)}'
        '.b-info{color:var(--info);background:var(--info-bg)}.b-muted{color:var(--muted)}'
        'dl.gloss{display:grid;grid-template-columns:minmax(120px,max-content) 1fr;gap:4px 14px;font-size:14px}'
        'dl.gloss dt{font-family:ui-monospace,monospace}dl.gloss dd{margin:0}'
        '@media (max-width:640px){body{padding:16px 16px 48px;font-size:15px}.tabs a,.tabs span{padding:8px 10px}'
        'dl.gloss{grid-template-columns:1fr}dl.gloss dd{margin-bottom:6px}}')


def badge_class(value):
    """Badge colour class for a status value (operational outcome, validity, judge state)."""
    value = str(value or '')
    if value in ('accepted', 'genuine', 'completed', 'harness_completed'):
        return 'b-good'
    if value in ('rejected', 'fraudulent', 'judge_error') or value.startswith('error'):
        return 'b-bad'
    if value in ('accepted_after_close', 'unresolved', 'partial', 'problem_closed', 'pending') \
            or value.startswith('budget') or value.startswith('stopped'):
        return 'b-warn'
    return 'b-muted'
