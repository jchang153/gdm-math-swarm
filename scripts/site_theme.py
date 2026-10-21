"""Shared light/dark, phone-friendly page styles for the derived run pages (report, transcripts).

Pure CSS: colour tokens on :root, redefined for dark mode, and a small set of layout
classes (summary card, tab bar, badges, wide-table scroll). No scripts, fonts or network.
"""

TOKENS = (':root{color-scheme:light dark;--bg:#fafbfc;--panel:#ffffff;--text:#18212b;--muted:#596579;'
          '--line:#dde3ea;--link:#155eaa;--code:#f1f4f7;--accent:#155eaa;--good:#1f7a3a;--warn:#9a5b00;'
          '--bad:#b42318;--info:#4b5cc4;--mark:#ffe58a;--mark-text:#18212b;--good-bg:#e6f4ea;--warn-bg:#fdf1dc;'
          '--bad-bg:#fde8e6;--info-bg:#eceffd}'
          '@media (prefers-color-scheme:dark){:root{--bg:#10151b;--panel:#171e26;--text:#e4e9ef;--muted:#9aa6b4;'
          '--line:#2b3541;--link:#7fb6f0;--code:#1d2630;--accent:#7fb6f0;--good:#6fcf8b;--warn:#f0b357;'
          '--bad:#f08679;--info:#a3b0ff;--mark:#6b5a12;--mark-text:#fff6d5;--good-bg:#14301d;--warn-bg:#3a2a10;'
          '--bad-bg:#3d1714;--info-bg:#1e2547}}')

BASE = (TOKENS +
        'html{-webkit-text-size-adjust:100%}*,*::before,*::after{box-sizing:border-box}'
        'body{max-width:1150px;margin:0 auto;padding:24px 16px 60px;font:16px/1.55 system-ui,-apple-system,'
        '"Segoe UI",sans-serif;color:var(--text);background:var(--bg);overflow-wrap:break-word}'
        'a{color:var(--link)}small,.muted{color:var(--muted)}code,.id{font-family:ui-monospace,SFMono-Regular,'
        'Menlo,monospace;font-size:.92em}h1{margin:.2em 0 .1em;line-height:1.2}h2{margin-top:1.8em}'
        'table{border-collapse:collapse;width:100%;background:var(--panel)}td,th{text-align:left;padding:8px 9px;'
        'border-bottom:1px solid var(--line);vertical-align:top}th{font-size:13px;color:var(--muted)}'
        '.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}.scroll table{min-width:640px}'
        '.crumbs{font-size:14px;margin:0 0 10px}'
        '.card{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:1px;background:var(--line);'
        'border:1px solid var(--line);border-radius:10px;overflow:hidden;margin:16px 0}'
        '.tile{background:var(--panel);padding:12px 14px}.tile .k{font-size:12px;color:var(--muted);'
        'text-transform:uppercase;letter-spacing:.04em}.tile .v{font-size:19px;font-weight:600;margin-top:2px;line-height:1.25}'
        '.tile .s{font-size:13px;color:var(--muted)}'
        '.tabs{display:flex;flex-wrap:wrap;gap:6px;border-bottom:2px solid var(--line);margin:18px 0 0;padding:0}'
        '.tabs a,.tabs span{display:inline-block;padding:9px 14px;border:1px solid var(--line);border-bottom:none;'
        'border-radius:8px 8px 0 0;background:var(--panel);text-decoration:none;font-weight:600;margin-bottom:-2px}'
        '.tabs .current{border-bottom:2px solid var(--panel);color:var(--text)}.tabs .off{color:var(--muted);'
        'font-weight:400}'
        '.tabhelp{margin:0;padding:10px 0;list-style:none;font-size:14px}.tabhelp li{margin:3px 0}'
        '.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;'
        'border:1px solid currentColor;white-space:nowrap}.b-good{color:var(--good);background:var(--good-bg)}'
        '.b-warn{color:var(--warn);background:var(--warn-bg)}.b-bad{color:var(--bad);background:var(--bad-bg)}'
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
