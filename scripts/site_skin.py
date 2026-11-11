"""Visual directions ("skins") for the user-facing pages: Explore, Experiments and About (owner, November 10, 2026:
"explore more stylistic options ... this should affect both explore and experiments page. Also include a separate
about page").

Each skin is one token set, light and dark, plus a little skin-specific CSS. ``skin_css(name)`` returns the full
stylesheet (tokens and the shared ``sk-`` components used by scripts/explore_page.py); ``skin_css(name,
scope='tokens')`` returns only the tokens, the matching site tokens of ``site_style`` (``--bg``, ``--fg``,
``--accent`` ...) and the body and heading fonts, so an existing page such as the catalogue can take on a skin
without changing its own CSS. Put the skin CSS after ``site_style``'s (``site_style.themed`` already inserts the
theme first, so page styles win). Light and dark follow the same rules as site_style: the visitor's system setting,
overridden by ``data-theme`` on <html> from the corner toggle.

System font stacks only; no external resources. Text colours meet WCAG AA (4.5:1) on their backgrounds.
"""
import html

DEFAULT = 'editorial'

# Token names: bg, ink (text), soft (secondary text), muted (captions), card, line, accent, on-accent (text on the
# accent), hero/hero-ink (hero block), block/block-ink (colour block), c1..c5 (data series: c1 = no oversight, c5 =
# most oversight), bad, good, plus fonts, radius and shadow.
_SITE_MAP = {'bg': 'bg', 'fg': 'ink', 'muted': 'muted', 'soft': 'soft', 'card': 'card', 'line': 'line',
             'accent': 'accent', 'chip': 'chip', 'pre': 'chip', 'shadow': 'shadow-c', 'bad': 'bad', 'good': 'good'}

SKINS = {
    'editorial': {
        'label': 'Editorial',
        'blurb': 'Bold magazine: paper and ink, huge serif headlines, colour blocks and hard rules.',
        'light': {
            'bg': '#f6f1e7', 'ink': '#141414', 'soft': '#36332e', 'muted': '#5e594f', 'card': '#fffdf8',
            'line': '#d8cfbd', 'chip': '#ece4d4', 'accent': '#b8301b', 'on-accent': '#ffffff',
            'hero': '#141414', 'hero-ink': '#f6f1e7', 'hero-soft': '#cfc7b8', 'block': '#1d3b8f',
            'block-ink': '#ffffff', 'c1': '#c8321e', 'c2': '#a86a00', 'c3': '#7a4fc0', 'c4': '#1f6fb2',
            'c5': '#138a5e', 'bad': '#b8301b', 'good': '#137a52', 'shadow-c': 'rgba(20,20,20,.14)',
            'grid': 'rgba(20,20,20,.07)'},
        'dark': {
            'bg': '#121110', 'ink': '#f2ece0', 'soft': '#d6cfc1', 'muted': '#a8a093', 'card': '#1c1a17',
            'line': '#3a352d', 'chip': '#2a2621', 'accent': '#ff6b4f', 'on-accent': '#141414',
            'hero': '#f2ece0', 'hero-ink': '#141414', 'hero-soft': '#4a453d', 'block': '#2c4aa8',
            'block-ink': '#ffffff', 'c1': '#ff6b4f', 'c2': '#f2b43c', 'c3': '#b394f2', 'c4': '#5aa9ec',
            'c5': '#3ccf95', 'bad': '#ff7a62', 'good': '#4fd39d', 'shadow-c': 'rgba(0,0,0,.5)',
            'grid': 'rgba(242,236,224,.08)'},
        'fonts': {'display': '"Iowan Old Style","Palatino Linotype","Book Antiqua",Palatino,Georgia,"Times New Roman",serif',
                  'body': 'system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif',
                  'mono': 'ui-monospace,SFMono-Regular,Menlo,Consolas,monospace'},
        'radius': '2px', 'radius-lg': '4px',
        'css': (
            '.sk-hero{background:var(--k-hero);color:var(--k-hero-ink);border-bottom:6px solid var(--k-accent)}'
            '.sk-hero .sk-lead{color:var(--k-hero-soft)}.sk-hero .sk-kicker{color:var(--k-accent)}'
            '.sk-hero .sk-stat dd{color:var(--k-hero-ink)}.sk-hero .sk-stat dt{color:var(--k-hero-soft)}'
            '.sk-h1{font-family:var(--k-display);font-weight:700;letter-spacing:-.02em}'
            '.sk-h2{font-family:var(--k-display);font-weight:700}'
            '.sk-chapter{border-top:3px solid var(--k-ink)}'
            '.sk-chapter-n{font-family:var(--k-display);font-style:italic;color:var(--k-accent)}'
            '.sk-take{border-left:6px solid var(--k-accent);padding-left:16px}'
            '.sk-num dd{font-family:var(--k-display)}'
            '.sk-band{background:var(--k-block);color:var(--k-block-ink)}'
            '.sk-band .sk-h2,.sk-band p{color:var(--k-block-ink)}'
            '.sk-band a.sk-btn:not(.ghost){background:var(--k-block-ink);color:var(--k-block);border-color:var(--k-block-ink)}'
            '.sk-card{border:1px solid var(--k-ink);box-shadow:4px 4px 0 var(--k-ink)}'
            '.sk-fig-frame{border:1px solid var(--k-ink)}'
            '.sk-nav{border-bottom:1px solid var(--k-ink)}.sk-nav .sk-brand{font-family:var(--k-display);font-size:20px}'
            'body.sk .sk-nav a[aria-current]{box-shadow:inset 0 -3px 0 var(--k-accent)}'
        ),
    },
    'lab': {
        'label': 'Lab dashboard',
        'blurb': 'Dark control room: glowing data on a faint grid, monospace labels, neon series colours.',
        'light': {
            'bg': '#eef3f8', 'ink': '#0b1726', 'soft': '#283a50', 'muted': '#4a5e74', 'card': '#ffffff',
            'line': '#c9d5e3', 'chip': '#e1e9f2', 'accent': '#00739f', 'on-accent': '#ffffff',
            'hero': '#0b1726', 'hero-ink': '#e6f1fb', 'hero-soft': '#a9bfd6', 'block': '#0b1726',
            'block-ink': '#e6f1fb', 'c1': '#d42a4c', 'c2': '#b86e00', 'c3': '#7f45d1', 'c4': '#0a7fbd',
            'c5': '#0b8f6a', 'bad': '#c4213f', 'good': '#0b7a5b', 'shadow-c': 'rgba(11,23,38,.14)',
            'grid': 'rgba(11,23,38,.06)', 'glow': 'transparent'},
        'dark': {
            'bg': '#060a13', 'ink': '#dce8f5', 'soft': '#aabdd2', 'muted': '#8296ad', 'card': '#0c1322',
            'line': '#1d2b44', 'chip': '#121c30', 'accent': '#38e1ff', 'on-accent': '#03111a',
            'hero': '#08101e', 'hero-ink': '#e6f1fb', 'hero-soft': '#9fb4cc', 'block': '#0c1a2e',
            'block-ink': '#e6f1fb', 'c1': '#ff4d6d', 'c2': '#ffb02e', 'c3': '#b48cff', 'c4': '#38c6ff',
            'c5': '#3dffb0', 'bad': '#ff5c7a', 'good': '#3dffb0', 'shadow-c': 'rgba(0,0,0,.6)',
            'grid': 'rgba(56,225,255,.06)', 'glow': 'rgba(56,225,255,.35)'},
        'fonts': {'display': 'system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif',
                  'body': 'system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif',
                  'mono': 'ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace'},
        'radius': '8px', 'radius-lg': '14px',
        'css': (
            'body.sk{background-image:linear-gradient(var(--k-grid) 1px,transparent 1px),'
            'linear-gradient(90deg,var(--k-grid) 1px,transparent 1px);background-size:32px 32px}'
            '.sk-hero{background:radial-gradient(1200px 500px at 80% -10%,color-mix(in srgb,var(--k-accent) 22%,'
            'transparent),transparent 60%),var(--k-hero);color:var(--k-hero-ink);border-bottom:1px solid var(--k-line)}'
            '.sk-hero .sk-lead{color:var(--k-hero-soft)}.sk-hero .sk-stat dt{color:var(--k-hero-soft)}'
            '.sk-hero .sk-stat dd{color:var(--k-hero-ink);text-shadow:0 0 18px var(--k-glow)}'
            '.sk-hero .sk-kicker{color:var(--k-accent)}'
            '.sk-kicker,.sk-chapter-n,.sk-num dt,.sk-stat dt,.sk-fig figcaption,.sk-chart text{font-family:var(--k-mono)}'
            '.sk-kicker{text-transform:uppercase;letter-spacing:.14em}'
            '.sk-h1{font-weight:800;letter-spacing:-.035em}'
            '.sk-chapter-n{color:var(--k-accent);text-shadow:0 0 14px var(--k-glow)}'
            '.sk-num dd{color:var(--k-accent);text-shadow:0 0 16px var(--k-glow)}'
            '.sk-card,.sk-fig-frame{border:1px solid var(--k-line);box-shadow:0 0 0 1px color-mix(in srgb,var(--k-accent) 8%,'
            'transparent),0 10px 40px -12px var(--k-glow)}'
            '.sk-chart .sk-mark{filter:drop-shadow(0 0 4px color-mix(in srgb,currentColor 70%,transparent))}'
            '.sk-band{background:var(--k-block);color:var(--k-block-ink);border-block:1px solid var(--k-line)}'
            '.sk-band .sk-h2,.sk-band p{color:var(--k-block-ink)}'
            '.sk-nav{background:color-mix(in srgb,var(--k-bg) 85%,transparent);backdrop-filter:blur(8px);'
            'border-bottom:1px solid var(--k-line)}.sk-nav .sk-brand{font-family:var(--k-mono);letter-spacing:.04em}'
            'body.sk .sk-nav a[aria-current]{color:var(--k-accent);text-shadow:0 0 12px var(--k-glow)}'
            '.sk-take{color:var(--k-ink)}'
        ),
    },
    'playful': {
        'label': 'Warm and playful',
        'blurb': 'Cream paper, rounded type, sticker cards with chunky shadows and sunny colour blocks.',
        'light': {
            'bg': '#fff6ea', 'ink': '#2a1a12', 'soft': '#4b3428', 'muted': '#6c5244', 'card': '#ffffff',
            'line': '#efd6ba', 'chip': '#fde9cf', 'accent': '#c2410c', 'on-accent': '#ffffff',
            'hero': '#ffd166', 'hero-ink': '#2a1a12', 'hero-soft': '#4b3428', 'block': '#b9e6d6',
            'block-ink': '#13372b', 'c1': '#d0321f', 'c2': '#a86c00', 'c3': '#8b4fe0', 'c4': '#2683cf',
            'c5': '#16956a', 'bad': '#c0331f', 'good': '#12775a', 'shadow-c': '#2a1a12', 'grid': 'rgba(42,26,18,.06)'},
        'dark': {
            'bg': '#1c1310', 'ink': '#fbeee0', 'soft': '#e8d5c2', 'muted': '#c2a893', 'card': '#2a1d17',
            'line': '#4c362a', 'chip': '#3a2a20', 'accent': '#ff9157', 'on-accent': '#1c1310',
            'hero': '#3b2a12', 'hero-ink': '#ffe3a3', 'hero-soft': '#efd3a0', 'block': '#173a30',
            'block-ink': '#c9f2e3', 'c1': '#ff6a55', 'c2': '#ffc145', 'c3': '#bf95ff', 'c4': '#6cb8ff',
            'c5': '#4fdcaa', 'bad': '#ff7a66', 'good': '#5be0b0', 'shadow-c': '#000000', 'grid': 'rgba(251,238,224,.06)'},
        'fonts': {'display': 'ui-rounded,"SF Pro Rounded","Nunito","Varela Round","Arial Rounded MT Bold",'
                             'system-ui,-apple-system,"Segoe UI",sans-serif',
                  'body': 'ui-rounded,"SF Pro Rounded","Nunito",system-ui,-apple-system,"Segoe UI",sans-serif',
                  'mono': 'ui-monospace,SFMono-Regular,Menlo,Consolas,monospace'},
        'radius': '16px', 'radius-lg': '26px',
        'css': (
            '.sk-hero{background:var(--k-hero);color:var(--k-hero-ink);position:relative;overflow:hidden}'
            '.sk-hero:before,.sk-hero:after{content:"";position:absolute;border-radius:50%;pointer-events:none}'
            '.sk-hero:before{width:340px;height:340px;right:-90px;top:-120px;background:color-mix(in srgb,var(--k-accent) 30%,'
            'transparent)}.sk-hero:after{width:220px;height:220px;left:-80px;bottom:-110px;'
            'background:color-mix(in srgb,var(--k-c4) 25%,transparent)}.sk-hero>*{position:relative;z-index:1}'
            '.sk-hero .sk-lead{color:var(--k-hero-soft)}.sk-hero .sk-stat dt{color:var(--k-hero-soft)}'
            '.sk-hero .sk-stat dd{color:var(--k-hero-ink)}.sk-hero .sk-kicker{color:var(--k-hero-ink)}'
            '.sk-h1,.sk-h2{font-weight:800;letter-spacing:-.02em}'
            '.sk-kicker{display:inline-block;background:var(--k-card);color:var(--k-ink);padding:4px 12px;'
            'border-radius:999px;border:2px solid var(--k-ink)}'
            '.sk-chapter-n{display:inline-grid;place-items:center;width:1.6em;height:1.6em;border-radius:50%;'
            'background:var(--k-accent);color:var(--k-on-accent);font-weight:800}'
            '.sk-card,.sk-fig-frame{border:2px solid var(--k-ink);box-shadow:6px 6px 0 var(--k-shadow-c)}'
            '.sk-take{background:var(--k-chip);border-radius:var(--k-radius);padding:12px 16px}'
            '.sk-num>div{background:var(--k-card);border:2px solid var(--k-ink);border-radius:var(--k-radius);'
            'padding:12px 14px;box-shadow:4px 4px 0 var(--k-shadow-c)}'
            '.sk-band{background:var(--k-block);color:var(--k-block-ink)}'
            '.sk-band .sk-h2,.sk-band p{color:var(--k-block-ink)}'
            'body.sk a.sk-btn{border-width:2px;border-color:var(--k-ink);box-shadow:3px 3px 0 var(--k-shadow-c)}'
            'body.sk .sk-nav a[aria-current]{background:var(--k-ink);color:var(--k-bg)}'
        ),
    },
}

# Shared components (every skin), written only in skin tokens.
COMPONENTS = (
    '*,*:before,*:after{box-sizing:border-box}'
    'body.sk{margin:0;padding:0;background-color:var(--k-bg);color:var(--k-ink);font:17px/1.6 var(--k-body);'
    '-webkit-font-smoothing:antialiased;overflow-x:hidden}'
    'body.sk a{color:var(--k-accent)}'
    '.sk-wrap{max-width:1240px;margin:0 auto;padding:0 32px}'
    '.sk-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}'
    # Top navigation
    '.sk-nav{position:sticky;top:0;z-index:40;background:var(--k-bg)}[id]{scroll-margin-top:72px}'
    '.sk-nav .sk-wrap{display:flex;align-items:center;justify-content:space-between;gap:16px;min-height:60px}'
    '.sk-brand{font-weight:700;color:var(--k-ink)!important;text-decoration:none;white-space:nowrap}'
    '.sk-links{display:flex;gap:4px;margin:0;padding:0;list-style:none}'
    # body.sk: above the page's generic accent link colour (body.sk a), so the bar's links read as navigation.
    'body.sk .sk-links a{display:block;padding:8px 14px;border-radius:var(--k-radius);color:var(--k-soft);'
    'text-decoration:none;font-weight:600;font-size:15px;white-space:nowrap}body.sk .sk-links a:hover{color:var(--k-ink)}'
    'body.sk .sk-links a[aria-current]{color:var(--k-ink)}'
    # Hero
    '.sk-hero{padding:72px 0 56px}'
    '.sk-kicker{margin:0 0 18px;font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;'
    'color:var(--k-accent)}'
    '.sk-h1{font-size:clamp(2.4rem,6.2vw,5.4rem);line-height:1.02;margin:0 0 22px;max-width:17ch;font-family:var(--k-display)}'
    '.sk-lead{font-size:clamp(1.08rem,1.7vw,1.32rem);line-height:1.55;max-width:62ch;margin:0;color:var(--k-soft)}'
    '.sk-stats{display:flex;flex-wrap:wrap;gap:20px 56px;margin:40px 0 0;padding:0}'
    '.sk-stat{display:flex;flex-direction:column-reverse}.sk-stat dd{margin:0;font-size:clamp(2rem,4vw,3rem);'
    'font-weight:800;line-height:1;font-variant-numeric:tabular-nums;letter-spacing:-.02em;font-family:var(--k-display)}'
    '.sk-stat dt{font-size:14px;margin-top:6px;color:var(--k-muted)}'
    '.sk-cta{display:flex;flex-wrap:wrap;gap:10px;margin:36px 0 0}'
    # Buttons
    'body.sk a.sk-btn,button.sk-btn{display:inline-flex;align-items:center;gap:8px;padding:11px 20px;border-radius:999px;'
    'border:1px solid var(--k-accent);background:var(--k-accent);color:var(--k-on-accent);font:700 15px/1.2 var(--k-body);'
    'text-decoration:none;cursor:pointer;min-height:44px}'
    'body.sk a.sk-btn.ghost,button.sk-btn.ghost{background:transparent;color:inherit;border-color:currentColor}'
    'a.sk-btn:hover{filter:brightness(1.08);text-decoration:none}'
    ':focus-visible{outline:3px solid var(--k-accent);outline-offset:2px}'
    # Section headings
    '.sk-h2{font-size:clamp(1.7rem,3.6vw,3rem);line-height:1.08;margin:0 0 14px;letter-spacing:-.02em;max-width:24ch;'
    'font-family:var(--k-display)}'
    '.sk-section{padding:64px 0}'
    '.sk-band{padding:56px 0}'
    # Story chapters
    '.sk-chapter{padding:56px 0 64px;margin-top:24px}'
    '.sk-chapter-head{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:24px 56px;align-items:start}'
    '.sk-chapter-n{font-size:clamp(1.4rem,2.4vw,2rem);font-weight:700;margin:0 0 10px;line-height:1}'
    '.sk-take{font-size:clamp(1.12rem,1.8vw,1.4rem);line-height:1.45;font-weight:600;margin:0 0 16px;color:var(--k-ink);'
    'max-width:46ch}'
    '.sk-story{margin:0;color:var(--k-soft);max-width:62ch}'
    '.sk-num{display:grid;gap:14px;margin:0;padding:0}'
    '.sk-num>div{display:flex;flex-direction:column-reverse}.sk-num dd{margin:0;font-size:clamp(2.2rem,4.6vw,3.6rem);'
    'font-weight:800;line-height:1;letter-spacing:-.02em;font-variant-numeric:tabular-nums;color:var(--k-accent)}'
    '.sk-num dt{font-size:14.5px;line-height:1.35;color:var(--k-soft);margin-top:6px}'
    '.sk-links-row{display:flex;flex-wrap:wrap;gap:10px;margin:24px 0 0}'
    # Figures
    '.sk-figs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;margin:32px 0 0}'
    '.sk-fig{margin:0;grid-column:1/-1;min-width:0}.sk-fig.half{grid-column:auto}'
    '.sk-fig-frame{display:block;position:relative;background:var(--k-card);border-radius:var(--k-radius-lg);overflow:hidden}'
    '.sk-fig-frame iframe{display:block;width:100%;height:clamp(460px,78vh,820px);border:0;background:var(--k-bg)}'
    '.sk-fig-frame img{display:block;width:100%;height:auto;background:#fff}'
    '.sk-fig figcaption{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:8px 16px;'
    'margin:12px 2px 0;font-size:14px;line-height:1.45;color:var(--k-muted)}.sk-fig figcaption span{max-width:80ch}'
    '.sk-fig figcaption .sk-acts{display:flex;flex-wrap:wrap;gap:8px}'
    '.sk-fig figcaption a.sk-btn{padding:8px 15px;min-height:38px;font-size:14px}'
    '.sk-gwrap.open{position:fixed;inset:0;z-index:1000;background:var(--k-bg)}.sk-gwrap.open iframe{height:100%}'
    'html.sk-lock{overflow:hidden}'
    '.sk-gclose{position:absolute;top:10px;right:12px;z-index:2;width:42px;height:42px;border-radius:50%;'
    'border:1px solid var(--k-line);background:var(--k-card);color:var(--k-ink);font-size:18px;cursor:pointer}'
    '.sk-gclose[hidden]{display:none}'
    # Charts (inline SVG)
    '.sk-chart{padding:22px 24px 16px}.sk-chart h3{margin:0;font-size:clamp(1.15rem,2vw,1.5rem);font-family:var(--k-display)}'
    '.sk-chart .sk-sub{margin:4px 0 14px;color:var(--k-muted);font-size:14px;max-width:none}'
    '.sk-chart svg{display:block;width:100%;height:auto;overflow:visible}'
    '.sk-chart text{fill:var(--k-muted);font:13px var(--k-body)}.sk-chart .lbl{fill:var(--k-ink);font-weight:650;font-size:15px}'
    '.sk-chart .big{font-weight:800;font-size:30px;font-family:var(--k-display)}'
    '.sk-chart .ax{stroke:var(--k-line);stroke-width:1}.sk-chart .track{stroke:var(--k-line);stroke-width:10;stroke-linecap:round}'
    '.sk-chart .rep{opacity:.28}'
    '.sk-narrow{display:none}'
    '.sk-dots{display:grid;gap:6px}.sk-dotrow{display:grid;grid-template-columns:minmax(150px,230px) minmax(0,1fr) 90px;'
    'align-items:center;gap:16px}.sk-dotlbl{font-weight:650;text-align:right;line-height:1.25;color:var(--k-ink)}'
    '.sk-strip{position:relative;height:40px;display:block}.sk-strip svg{position:absolute;inset:0;width:100%;height:100%}'
    '.sk-dot{position:absolute;top:50%;width:22px;height:22px;margin:-11px 0 0 -11px;border-radius:50%;'
    'background:currentColor;border:3px solid var(--k-card);box-sizing:border-box}a.sk-dot:hover{transform:scale(1.2)}'
    '.sk-dotavg{font:800 clamp(1.5rem,3vw,2.1rem)/1 var(--k-display);text-align:right;font-variant-numeric:tabular-nums}'
    '.sk-dotaxis .sk-strip{height:18px}.sk-dotaxis i{position:absolute;transform:translateX(-50%);font:12px var(--k-body);'
    'font-style:normal;color:var(--k-muted)}'
    '.sk-legend{display:flex;flex-wrap:wrap;gap:6px 18px;margin:10px 0 0;padding:0;list-style:none;font-size:13.5px;'
    'color:var(--k-soft)}.sk-legend i{display:inline-block;width:14px;height:14px;border-radius:3px;margin-right:6px;'
    'vertical-align:-2px}'
    '.sk-basis{font-size:12.5px;color:var(--k-muted);margin:10px 0 0;max-width:none}'
    '.sk-c1{color:var(--k-c1)}.sk-c2{color:var(--k-c2)}.sk-c3{color:var(--k-c3)}.sk-c4{color:var(--k-c4)}.sk-c5{color:var(--k-c5)}'
    # Cards (upcoming, experiments mock, about)
    '.sk-card{background:var(--k-card);border-radius:var(--k-radius-lg);padding:24px 26px}'
    '.sk-pending{display:inline-block;font-size:13px;font-weight:700;padding:4px 12px;border-radius:999px;'
    'background:var(--k-chip);color:var(--k-ink)}'
    '.sk-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr));gap:20px}'
    '.sk-prose{max-width:70ch}.sk-prose p,.sk-prose li{color:var(--k-soft)}'
    '.sk-steps{counter-reset:s;list-style:none;padding:0;margin:0;display:grid;gap:16px;'
    'grid-template-columns:repeat(auto-fill,minmax(min(260px,100%),1fr))}'
    '.sk-steps li{counter-increment:s}.sk-steps li:before{content:counter(s,decimal-leading-zero);display:block;'
    'font:800 28px/1 var(--k-display);color:var(--k-accent);margin-bottom:8px}'
    '.sk-steps b{display:block;color:var(--k-ink);margin-bottom:4px}'
    '.sk-gloss{display:grid;grid-template-columns:minmax(140px,max-content) 1fr;gap:10px 24px;margin:0}'
    '.sk-gloss dt{font-weight:700}.sk-gloss dd{margin:0;color:var(--k-soft)}'
    'code{font-family:var(--k-mono);font-size:.9em;background:var(--k-chip);padding:1px 5px;border-radius:4px}'
    '.sk-foot{padding:40px 0 80px;border-top:1px solid var(--k-line);color:var(--k-muted);font-size:14px}'
    '.sk-foot p{max-width:80ch;color:var(--k-muted)}'
    # Phone and narrow screens
    '@media (max-width:900px){.sk-chapter-head{grid-template-columns:minmax(0,1fr)}'
    '.sk-num{grid-template-columns:repeat(3,minmax(0,1fr))}}'
    '@media (max-width:640px){body.sk{font-size:16px;margin:0!important;padding:0!important}'
    '.sk-wrap{padding:0 16px}.sk-hero{padding:44px 0 36px}.sk-nav .sk-wrap{min-height:52px}'
    '.sk-links a{padding:7px 8px;font-size:14px}.sk-brand{font-size:15px!important;white-space:normal;line-height:1.15;'
    'max-width:8.5em}'
    '.sk-figs{grid-template-columns:minmax(0,1fr)}.sk-fig.half{grid-column:1/-1}'
    '.sk-fig-frame iframe{height:72vh}.sk-chart{padding:16px 12px 12px}'
    '.sk-num{grid-template-columns:minmax(0,1fr)}.sk-chapter{padding:40px 0 44px}'
    '.sk-wide{display:none}.sk-narrow{display:block}'
    '.sk-dotrow{grid-template-columns:minmax(0,1fr) 56px;gap:2px 10px}.sk-dotlbl{grid-column:1/-1;text-align:left;'
    'font-size:14px;margin-top:6px}.sk-dotaxis>span:first-child{display:none}'
    '.sk-links{gap:0}.sk-links a{white-space:nowrap}'
    '.sk-gloss{grid-template-columns:minmax(0,1fr)}.sk-gloss dd{margin-bottom:8px}}'
)


def _vars(tokens, prefix):
    return ''.join(f'--{prefix}{k}:{v};' for k, v in tokens.items())


def _token_block(skin, mode):
    tokens = dict(SKINS[skin][mode])
    s = SKINS[skin]
    extra = {'display': s['fonts']['display'], 'body': s['fonts']['body'], 'mono': s['fonts']['mono'],
             'radius': s['radius'], 'radius-lg': s['radius-lg']}
    tokens.setdefault('glow', 'transparent')
    site = ''.join(f'--{name}:var(--k-{key});' for name, key in _SITE_MAP.items())
    return _vars(tokens, 'k-') + _vars(extra, 'k-') + site


def tokens_css(name=DEFAULT):
    """The skin's tokens for light and dark, under the same selectors as site_style (system setting, overridden by
    data-theme on <html>), plus the site_style tokens mapped onto them."""
    name = name if name in SKINS else DEFAULT
    light, dark = _token_block(name, 'light'), _token_block(name, 'dark')
    return (f':root{{color-scheme:light;{light}}}'
            f'@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{color-scheme:dark;{dark}}}}}'
            f':root[data-theme="dark"]{{color-scheme:dark;{dark}}}')


def skin_css(name=DEFAULT, scope='full'):
    """The skin's stylesheet. ``scope='full'``: tokens, shared sk- components and the skin's own rules (Explore,
    About). ``scope='tokens'``: only the tokens (with the site_style ones mapped) and fonts for body and headings, for
    an existing page such as the catalogue."""
    name = name if name in SKINS else DEFAULT
    if scope == 'tokens':
        return (tokens_css(name) + 'body{font-family:var(--k-body);background-color:var(--k-bg);color:var(--k-ink)}'
                'h1,h2,h3{font-family:var(--k-display)}')
    return tokens_css(name) + COMPONENTS + SKINS[name]['css']


NAV = (('index.html', 'explore', 'Explore'), ('experiments.html', 'experiments', 'Experiments'),
       ('about.html', 'about', 'About'))


def nav(current, prefix='', brand='GDM Math Swarm'):
    """The top navigation bar shared by Explore, Experiments and About (``prefix``: path back to the site root, for
    pages in subfolders, e.g. '../')."""
    esc = html.escape
    items = ''.join(f'<li><a href="{esc(prefix + href, quote=True)}"'
                    + (' aria-current="page"' if key == current else '') + f'>{esc(label)}</a></li>'
                    for href, key, label in NAV)
    return (f'<nav class="sk-nav" aria-label="Site"><div class="sk-wrap"><a class="sk-brand" '
            f'href="{esc(prefix + "index.html", quote=True)}">{esc(brand)}</a><ul class="sk-links">{items}</ul></div></nav>')
