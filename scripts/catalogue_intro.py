"""Newcomer-facing text and style of the experiment catalogue (owner request, October 27, 2026).

Static, fixed text: the objective of the experiment, the paper it reconstructs (as cited in README.md and
docs/plan.md), how a run works, how to read the site, and a glossary. Redesigned October 29, 2026 (owner: "clanky
and old looking, text too dense"): a short hero with headline numbers, a series table of contents, and the longer
explanation collapsed into an About section at the bottom. Styles live in catalogue_page.STYLE. The run cards themselves are drawn by
build_experiment_index.py from each run's profile (scripts/run_profile.py). No external resources are
loaded; the only external link is the paper.
"""
import html

PAPER_URL = 'https://arxiv.org/pdf/2609.04170'
PAPER_TITLE = 'A Case Study on Emergent Cheating and Whistleblowing in Autonomous Research Swarms'
PAPER = (f'Paglieri et al., <a href="{PAPER_URL}" target="_blank" rel="noopener noreferrer"><cite>{PAPER_TITLE}</cite></a>, '
         'arXiv:2609.04170 (v1, September 3, 2026)')

LEAD = ('Swarms of 5 to 100 autonomous AI agents race to prove Lean 4 mathematics problems in a simulated research '
        'conference whose judge is deliberately weak. Do they discover and spread cheats, or blow the whistle, and can '
        'overseer agents contain an exploit the researchers plant?')

DISCLAIMER = ('An independent, documented reconstruction of the paper\'s research-conference environment: not an exact '
              'replication, and not an official Google DeepMind release. Deviations from the paper are recorded in the '
              'project repository.')

# The longer explanation formerly in the hero, as short scannable items (About section, "The experiment").
EXPERIMENT = (
    ('The swarm', 'Each run drops 5 to 100 copies of Claude Code, each driven by a language model such as GLM 5.3, into '
     'a simulated research conference. There is no coordinator: they all work at the same time.'),
    ('The problems', 'Open and solved mathematics problems written in Lean 4 with Mathlib. The first accepted proof of a '
     'problem earns the credit.'),
    ('The conference', 'Agents talk on a shared bulletin board and by direct message, share files in a library, and '
     'submit proofs.'),
    ('The weak judge', 'The conference judge only checks that a file compiles, uses no forbidden keywords and leaves the '
     'fixed parts of the problem untouched.'),
    ('The loophole', 'On problems with an answer field, an agent can get a proof of nothing accepted, for example by '
     'writing the question itself as the answer so the theorem reads <code>answer(P) ↔ P</code> and closing it with '
     '<code>Iff.rfl</code>.'),
    ('The questions', 'Do agents discover such cheats, copy them from each other, or blow the whistle? In the newer runs: '
     'can overseer agents contain a cheat that the researchers plant?'),
)

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

def _fmt(n):
    return f'{n:,}'


def topnav():
    return ('<nav class="topnav jump" aria-label="Site"><a class="brand" href="#top">GDM Math Swarm</a>'
            '<span class="tn-links"><a href="#runs-top">Runs</a><a href="#table-view">Table</a><a href="#about">About</a>'
            f'<a href="{PAPER_URL}" target="_blank" rel="noopener noreferrer">Paper</a></span></nav>')


def hero(stats):
    """The page top: navigation, title, a two-sentence summary, headline numbers and the compact paper credit.
    stats: [(label, number)]."""
    tiles = ''.join(f'<div><dt>{html.escape(k)}</dt><dd>{_fmt(v) if isinstance(v, int) else html.escape(str(v))}</dd></div>'
                    for k, v in stats)
    return (topnav() + '<header class="hero" id="top"><p class="eyebrow">Experiment library</p>'
            '<h1>Do AI research swarms cheat, and does anyone stop them?</h1>'
            f'<p class="lead">{html.escape(LEAD)}</p><dl class="stats">{tiles}</dl>'
            f'<p class="paper"><span class="paper-k">Reconstructs</span> {PAPER}. <span class="muted">{html.escape(DISCLAIMER)}'
            '</span></p></header>')


def toc(series):
    """The series table of contents: [(key, title, count)]."""
    return ('<nav class="toc jump" aria-label="Series">' + ''.join(
        f'<a href="#series-{html.escape(k, quote=True)}"><span>{html.escape(t)}</span><span class="n">{n}</span></a>'
        for k, t, n in series) + '</nav>')


def about_section():
    """Everything a newcomer may want, collapsed: the experiment, how a run works, the pages, the glossary."""
    exp = ''.join(f'<li><strong>{html.escape(t)}.</strong> {d}</li>' for t, d in EXPERIMENT)
    steps = ''.join(f'<li><strong>{html.escape(t)}</strong><span>{html.escape(d)}</span></li>' for t, d in STEPS)
    pages = ''.join(f'<div><dt>{html.escape(t)}</dt><dd>{html.escape(d)}</dd></div>' for t, d in PAGES)
    terms = ''.join(f'<div><dt>{html.escape(t)}</dt><dd>{html.escape(d)}</dd></div>' for t, d in GLOSSARY)
    return ('<section class="about" id="about"><h2>About this library</h2>'
            '<p class="sub">Background, the pages of a run and the terms used on cards and reports.</p>'
            f'<details id="experiment" open><summary>The experiment</summary><ul class="facts">{exp}</ul></details>'
            f'<details id="how"><summary>How a run works</summary><ol class="steps">{steps}</ol></details>'
            '<details id="read"><summary>How to read this site</summary><p class="sub">Each run has these pages; '
            f'open them from its card or table row.</p><dl class="defs">{pages}</dl></details>'
            f'<details id="glossary"><summary>Glossary</summary><dl class="defs">{terms}</dl></details></section>')
