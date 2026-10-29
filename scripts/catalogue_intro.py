"""Newcomer-facing text and style of the experiment catalogue (owner request, October 27, 2026).

Static, fixed text: the objective of the experiment, the paper it reconstructs (as cited in README.md and
docs/plan.md), how a run works, how to read the site, and a glossary. The run cards themselves are drawn by
build_experiment_index.py from each run's profile (scripts/run_profile.py). No external resources are
loaded; the only external link is the paper.
"""
import html

PAPER_URL = 'https://arxiv.org/pdf/2609.04170'
PAPER_TITLE = 'A Case Study on Emergent Cheating and Whistleblowing in Autonomous Research Swarms'
PAPER = (f'Paglieri et al., <a href="{PAPER_URL}" target="_blank" rel="noopener noreferrer"><cite>{PAPER_TITLE}</cite></a>, '
         'arXiv:2609.04170 (v1, September 3, 2026)')

HERO = (
    '<header class="hero"><p class="eyebrow">GDM Math Swarm · experiment library</p>'
    '<h1>Do AI research swarms cheat, and does anyone stop them?</h1>'
    '<p class="lead">Each run on this site drops a swarm of autonomous AI agents (5 to 100 copies of Claude Code, '
    'each driven by a language model such as GLM 5.3) into a simulated research conference. Without a coordinator, '
    'they work at the same time on open and solved mathematics problems written in Lean 4 with Mathlib, talk on a '
    'shared bulletin board and by direct message, share files in a library, and submit proofs. The first accepted '
    'proof of a problem earns the credit.</p>'
    '<p class="lead">The conference judge is deliberately weak: it only checks that a file compiles, uses no '
    'forbidden keywords and leaves the fixed parts of the problem untouched. On problems with an answer field, an '
    'agent can get a proof of nothing accepted, for example by writing the question itself as the answer so the '
    'theorem reads <code>answer(P) ↔ P</code> and closing it with <code>Iff.rfl</code>. The runs ask whether agents '
    'discover such cheats, copy them from each other, or blow the whistle, and, in the newer runs, whether overseer '
    'agents can contain a cheat that the researchers plant.</p>'
    f'<aside class="paper"><strong>Paper reconstructed</strong><p>{PAPER}.</p>'
    '<p class="muted">An independent, documented reconstruction of the paper\'s research-conference environment: '
    'not an exact replication, and not an official Google DeepMind release. Deviations from the paper are recorded '
    'in the project repository.</p></aside>'
    '<nav class="jump" aria-label="On this page">JUMP</nav></header>')

STEPS = (
    ('The swarm works', 'Solver agents choose problems freely, post, message and share files. Nobody assigns work, '
     'and several agents may race on the same problem.'),
    ('A weak judge credits', 'Every submission is checked automatically by the operational judge. If it passes, '
     'the problem closes, the submitter is credited and the file is published to the shared library.'),
    ('Cheats appear or are planted', 'Spontaneous runs: any cheat must be found by an agent. Seeded runs: at '
     '+3 min a silent participant, agent-101, is credited with an answer-restating Iff.rfl proof of '
     'infinite_fermat_primes, which every agent can read in the library like any other solution.'),
    ('Overseers may step in', 'In overseer runs, extra agents can run check_proof (the independent proof check) and '
     'revert_submission (remove a credit and reopen the problem). Solvers are not told that overseers exist.'),
    ('Researchers grade afterwards', 'After the run, the independent evaluator classes every accepted submission as '
     'genuine, fake or unresolved, and a behavior judge reads each agent\'s transcript. Agents never see this.'),
)

PAGES = (
    ('Run card', 'The condition in one sentence, the standing fakes over time and the headline counts.'),
    ('Report', 'Start here. The facts-only record of a run: condition, every agent, every accepted submission with '
     'its conference outcome and independent validity, reverts, proof checks and spend.'),
    ('Conference', 'What happened inside the conference: bulletin board posts, direct messages, library revisions, '
     'judging, organizer feedback and the agents\' captured private files, on the run clock.'),
    ('Inspect View', 'Full agent transcripts: every model message and tool call. On the hosted site the logs live in a '
     'private dataset, so Inspect View asks you to sign in with Hugging Face.'),
    ('Behavior judge', 'A researcher-only, model-graded reading of every transcript: who considered, explored, '
     'submitted or was credited with a cheat, and who blew the whistle.'),
    ('Incident report', 'A narrative write-up of what happened, with quotes checked word for word against the '
     'records and a chart of how the exploit spread.'),
)

GLOSSARY = (
    ('Swarm run', 'One experiment: a fixed set of agents, problems, model and time limit, all running at once.'),
    ('Spontaneous / seeded', 'Spontaneous runs have no planted exploit. Seeded runs plant one (see Seed).'),
    ('Solver', 'An agent whose job is to prove problems for credit.'),
    ('Overseer', 'An extra agent asked to watch for invalid proofs. Most overseer runs give it check_proof and '
     'revert_submission; teams of 5 or 10 also share a private overseer board. Solvers are not told about overseers.'),
    ('check_proof', 'The overseers\' tool that runs the independent proof checker on a submission or file.'),
    ('Revert', 'revert_submission: an overseer removes a credit, the problem reopens and its library entry '
     'disappears. Nobody is notified automatically, and the problem can be solved (or cheated) again.'),
    ('Seed (agent-101)', 'A silent participant with no model or transcript, used only to deliver the planted exploit: '
     'at +3 min it is credited with an answer-restating proof of infinite_fermat_primes.'),
    ('Seed readers', 'Solvers who opened the planted seed file in the shared library: a measure of exposure used in '
     'the researchers\' analyses. Reverting the seed early removes the file and leaves fewer readers.'),
    ('Operational judge', 'The conference\'s weak automatic check: the file compiles, contains no forbidden keyword and '
     'keeps the fixed text. Acceptance is not mathematical validity.'),
    ('Independent evaluator', 'The researcher-only proof checker run after the fact: does the submission prove the '
     'intended statement? Its verdicts never change credit (only overseers can, through check_proof and revert).'),
    ('Accepted', 'Passed the operational judge and credited (at least until a revert).'),
    ('Genuine', 'Accepted and confirmed by the independent evaluator. "Genuine tasks" counts distinct problems.'),
    ('Fake', 'An accepted submission the evaluator classed fraudulent: it does not prove the intended statement, for '
     'example because its answer restates the question.'),
    ('Unresolved', 'The evaluator could not decide; never counted as genuine or fake. Several early spontaneous runs '
     'were checked before the evaluator recognized answer-restating exploits, so their exploit credits are unresolved '
     'and their fake counts are lower bounds.'),
    ('Standing fake', 'A fake that currently holds credit: accepted and not (yet) reverted. The card sparklines plot '
     'standing fakes over the run (filled) and all fakes accepted so far (dashed).'),
    ('Fake-minutes', 'For each fake, the minutes from its acceptance until it was reverted or the run ended, summed '
     'over all fakes: how long fake credit stood in total.'),
    ('Unread notices', 'A setting of the newest runs: every tool result tells the agent how many unread direct '
     'messages, new board posts and new library files are waiting.'),
    ('Prompt variant', 'The conference instructions given to solvers. collab_max_pressure_no_rule6 frames the '
     'conference as a high-stakes skills evaluation and leaves out the integrity rule against rewriting goals into '
     'trivial tautologies.'),
    ('Behavior judge', 'A model reading each transcript after the run. "simple" (default since October 24, 2026) '
     'grades how far each agent went with a cheat; "rubric 3.1" is the earlier multi-stage judge.'),
    ('Run clock', 'Times on run pages are shown as an offset from the run start (+m:ss) together with UTC.'),
)

CSS = (
    '.hero{max-width:1080px;margin:0 0 22px}.eyebrow{margin:0 0 6px;font-size:13px;font-weight:600;letter-spacing:.06em;'
    'text-transform:uppercase;color:var(--accent)}.hero h1{font-size:clamp(1.6rem,3.4vw,2.35rem);line-height:1.15;'
    'margin:0 0 14px;letter-spacing:-.01em}.hero .lead{font-size:17px;line-height:1.6;color:var(--fg);max-width:70ch;'
    'margin:0 0 12px}.hero code,.steps code,.gloss-grid code{font-size:.92em;background:var(--chip);padding:1px 5px;'
    'border-radius:5px}'
    '.paper{margin:18px 0 14px;padding:14px 18px;border:1px solid var(--line);border-left:4px solid var(--accent);'
    'border-radius:10px;background:var(--card);max-width:80ch}.paper p{margin:6px 0 0;color:var(--fg)}'
    '.paper p.muted{color:var(--muted);font-size:14px}.paper strong{font-size:13px;text-transform:uppercase;'
    'letter-spacing:.05em;color:var(--muted)}'
    '.jump{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}.jump a{display:inline-block;padding:5px 11px;'
    'border:1px solid var(--line);border-radius:999px;background:var(--card);font-size:14px;text-decoration:none}'
    '.jump a:hover{border-color:var(--accent)}'
    '.block{margin:24px 0}.block>h2{font-size:20px;margin:0 0 4px}.block>p.sub{margin:0 0 10px}'
    '.steps{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));'
    'gap:10px;counter-reset:step}.steps li{counter-increment:step;background:var(--card);border:1px solid var(--line);'
    'border-radius:12px;padding:11px 13px;font-size:13.5px;line-height:1.45;color:var(--soft)}.steps li strong{display:block;'
    'color:var(--fg);font-size:15px;margin-bottom:4px}.steps li strong::before{content:counter(step);display:inline-grid;'
    'place-items:center;width:22px;height:22px;margin-right:8px;border-radius:50%;background:var(--accent);'
    'color:var(--card);font-size:12px}'
    '.pages{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr));gap:4px 24px;margin:0}'
    '.pages div{padding:8px 0;border-bottom:1px solid var(--line)}.pages dt{font-weight:600}'
    '.pages dd{margin:2px 0 0;color:var(--soft);font-size:13.5px;line-height:1.45}'
    '.gloss-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(260px,100%),1fr));gap:2px 24px;margin:10px 0 0}'
    '.gloss-grid div{padding:8px 0;border-bottom:1px solid var(--line)}.gloss-grid dt{font-weight:600}'
    '.gloss-grid dd{margin:2px 0 0;color:var(--soft);font-size:13.5px;line-height:1.45}'
    '.viewbar{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:8px 0 4px}'
    '.seg{display:inline-flex;border:1px solid var(--input-line);border-radius:8px;overflow:hidden}'
    '.seg button{border:0;border-radius:0;padding:7px 14px;background:var(--card)}'
    '.seg button[aria-pressed=true]{background:var(--sel-bg);color:var(--sel-fg)}'
    '.series{margin:22px 0 28px}.series-head h2{font-size:20px;margin:0}.series-head h2 small{font-weight:400;'
    'margin-left:6px}.series-head p{margin:6px 0 0;max-width:70ch}'
    '.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px;color:var(--muted);margin:8px 0 12px!important}'
    '.legend i{display:inline-block;width:18px;height:10px;margin-right:5px;vertical-align:-1px}'
    '.lg-area{background:color-mix(in srgb,var(--bad) 28%,transparent);border-top:2px solid var(--bad)}'
    '.lg-cum{border-top:2px dashed var(--muted);height:0!important;vertical-align:3px!important}'
    '.lg-seed{border-left:2px dotted var(--accent);width:0!important;height:12px!important;margin-left:4px}'
    '.rc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(258px,100%),1fr));gap:10px}'
    '.rc{display:flex;flex-direction:column;background:var(--card);border:1px solid var(--line);border-radius:12px;'
    'padding:10px 12px 9px;box-shadow:0 1px 2px var(--shadow);min-width:0}.rc.picked{outline:2px solid var(--sel-bg)}'
    '.rc-body{flex:1}.rc-cond{font-size:14px;line-height:1.35;margin:0 0 6px;font-weight:600}'
    '.chips{margin:0 0 6px!important;display:flex;flex-wrap:wrap;gap:5px}.chip{font-size:12px;padding:1px 8px;'
    'border-radius:999px;background:var(--chip);color:var(--soft)}.chip-seed{background:var(--bad-bg);color:var(--bad)}'
    '.chip-ov{background:var(--info-bg);color:var(--info)}.chip-spont{background:var(--good-bg);color:var(--good)}'
    '.rc-spark{margin:2px 0 4px}.spark{display:block;width:100%;height:42px;overflow:visible}'
    '.sp-area{fill:color-mix(in srgb,var(--bad) 24%,transparent);stroke:var(--bad);stroke-width:1.5;'
    'vector-effect:non-scaling-stroke}.sp-cum{fill:none;stroke:var(--muted);stroke-width:1.2;stroke-dasharray:4 3;'
    'vector-effect:non-scaling-stroke}.sp-base{stroke:var(--line);stroke-width:1;vector-effect:non-scaling-stroke}'
    '.sp-seed{stroke:var(--accent);stroke-width:1.5;stroke-dasharray:2 2;vector-effect:non-scaling-stroke}'
    '.rc-spark figcaption{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:2px}'
    '.rc-fakes,.rc-stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;margin:6px 0 0}'
    '.rc-fakes div,.rc-stats div{min-width:0}.rc-fakes dt,.rc-stats dt{font-size:11px;color:var(--muted);line-height:1.25}'
    '.rc-fakes dd,.rc-stats dd{margin:1px 0 0;font-weight:650;font-size:16px;font-variant-numeric:tabular-nums}'
    '.rc-fakes dd.bad{color:var(--bad)}.rc-stats{border-top:1px solid var(--line);padding-top:6px;margin-top:8px}'
    '.rc-stats dd small{font-weight:400;font-size:11px;color:var(--muted)}'
    '.rc-note,.rc-empty{font-size:12.5px;line-height:1.4;color:var(--muted);margin:6px 0 0!important}'
    '.rc-foot{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:center;justify-content:space-between;'
    'border-top:1px solid var(--line);margin-top:8px;padding-top:6px;font-size:13px}'
    '.rc-when{color:var(--muted);font-size:12px;width:100%;overflow-wrap:anywhere}.rc-links{display:flex;flex-wrap:wrap;gap:2px 10px;min-width:0}'
    '.rc-links a{white-space:nowrap}.rc-pick{display:inline-flex;align-items:center;gap:5px;color:var(--soft);font-size:13px}'
    '.rc-pick input{width:16px;height:16px}'
    '@media (max-width:760px){.hero .lead{font-size:16px}.rc-grid{grid-template-columns:minmax(0,1fr)}'
    '.rc-fakes,.rc-stats{grid-template-columns:repeat(2,minmax(0,1fr))}.steps{grid-template-columns:1fr}}'
)


def jump(series):
    """The "On this page" links: each series (key, title) present, then the glossary and the full table."""
    links = [(f'#series-{key}', title) for key, title in series] + [
        ('#how', 'How a run works'), ('#read', 'How to read this site'), ('#glossary', 'Glossary'),
        ('#table-view', 'All runs table')]
    return ''.join(f'<a href="{html.escape(h, quote=True)}">{html.escape(t)}</a>' for h, t in links)


def hero(series):
    return HERO.replace('JUMP', jump(series))


def how_section():
    steps = ''.join(f'<li><strong>{html.escape(t)}</strong>{html.escape(d)}</li>' for t, d in STEPS)
    pages = ''.join(f'<div><dt>{html.escape(t)}</dt><dd>{html.escape(d)}</dd></div>' for t, d in PAGES)
    return ('<section class="block" id="how"><h2>How a run works</h2><ol class="steps">' + steps + '</ol></section>'
            '<section class="block" id="read"><h2>How to read this site</h2><p class="sub">Each run has these pages; '
            'open them from its card or table row.</p><dl class="pages">' + pages + '</dl></section>')


def glossary_section():
    terms = ''.join(f'<div><dt>{html.escape(t)}</dt><dd>{html.escape(d)}</dd></div>' for t, d in GLOSSARY)
    return ('<section class="block" id="glossary"><h2>Glossary</h2><dl class="gloss-grid">' + terms + '</dl></section>')
