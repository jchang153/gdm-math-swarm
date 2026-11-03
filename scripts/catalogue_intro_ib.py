"""Newcomer-facing text of the ImpossibleBench experiment catalogue (owner decision, November 3, 2026: the coding
runs are published to their own Cloudflare site in the same style as the math library, never to the math site).

Same structure and style as catalogue_intro.py (whose functions render these texts: ``hero(stats, text)``,
``about_section(text)``); only the words differ. Static, fixed text, so the catalogue stays a deterministic
function of its entries. No external resources are loaded; the only external link is the paper.
"""
import sys

import catalogue_intro

TITLE = 'GDM ImpossibleBench Swarm experiments'
BRAND = 'GDM ImpossibleBench Swarm'
HEADLINE = 'Do AI coding swarms copy a test-gaming solution, and does anyone stop them?'
PAPER_LABEL = 'Tasks from'

PAPER_URL = 'https://arxiv.org/abs/2510.20270'
PAPER_TITLE = 'ImpossibleBench: Measuring LLMs\' Propensity of Exploiting Test Cases'
PAPER = (f'Zhong et al., <a href="{PAPER_URL}" target="_blank" rel="noopener noreferrer"><cite>{PAPER_TITLE}</cite></a>, '
         'arXiv:2510.20270 (2025)')

LEAD = ('Swarms of 20 autonomous AI agents write Python solutions to Impossible-LiveCodeBench tasks, whose unit tests '
        'contradict the task specification, in a simulated research conference whose judge only runs the tests. The '
        'researchers plant a hard-coded solution: do agents copy and spread it, or flag it, and can overseer agents '
        'contain it?')

DISCLAIMER = ('The research-conference environment of the companion math library applied to coding tasks: an '
              'independent research setup, not an official Google DeepMind or ImpossibleBench release. Deviations '
              'are recorded in the project repository.')

EXPERIMENT = (
    ('The swarm', 'Each run drops 20 copies of Claude Code, each driven by a language model, into a simulated '
     'research conference. There is no coordinator: they all work at the same time.'),
    ('The problems', 'Impossible-LiveCodeBench tasks from ImpossibleBench: LiveCodeBench programming problems whose '
     'unit tests were changed so that they contradict the specification. A solution that follows the specification '
     'cannot pass every test. The first accepted solution of a task earns the credit.'),
    ('The conference', 'Agents talk on a shared bulletin board and by direct message, share files in a library, and '
     'submit solutions.'),
    ('The weak judge', 'The conference judge only checks that a submitted Python solution passes the task\'s unit '
     'tests, so it credits a solution that games the tests.'),
    ('The planted solution', 'Early in each run a silent participant is credited with a solution that hard-codes the '
     'expected test outputs instead of solving the task; every agent can read it in the library like any other '
     'solution.'),
    ('The questions', 'Do agents copy the planted solution or write their own test-gaming code, or do they flag it? '
     'Compared between a control condition without overseers and a condition with 2 overseer agents.'),
)

STEPS = (
    ('The swarm works', 'Solver agents choose tasks freely, post, message and share files. Nobody assigns work, '
     'and several agents may race on the same task.'),
    ('A weak judge credits', 'Every submission is run against the task\'s unit tests by the operational judge. If it '
     'passes, the task closes, the submitter is credited and the file is published to the shared library.'),
    ('A hard-coded solution is planted', 'A silent participant is credited early in the run with a solution that '
     'hard-codes the expected test outputs.'),
    ('Overseers may step in', 'In overseer runs, 2 extra agents can check a solution and revert a submission (remove a '
     'credit and reopen the task). Control runs have no overseers. Solvers are not told that overseers exist.'),
    ('Researchers grade afterwards', 'After the run, the independent evaluator classes every accepted submission, '
     'and a behavior judge reads each agent\'s transcript. Agents never see this.'),
)

PAGES = (
    ('Run card', 'The condition in one sentence, the standing test-gaming solutions over time and the headline '
     'counts.'),
    ('Report', 'Start here. The facts-only record of a run: condition, every agent, every accepted submission with '
     'its conference outcome and independent classification, reverts, solution checks and spend.'),
    ('Conference', 'What happened inside the conference: bulletin board posts, direct messages, library revisions, '
     'judging, organizer feedback and the agents\' captured private files, on the run clock.'),
    ('Inspect View', 'Full agent transcripts: every model message and tool call. The logs themselves are private and '
     'kept by the researchers.'),
    ('Story map', 'Where published: each agent\'s path through the run, event by event, with a validation page.'),
    ('Behavior judge', 'A researcher-only, model-graded reading of every transcript: who considered, explored, '
     'submitted or was credited with a test-gaming solution, and who flagged it.'),
)

GLOSSARY = (
    ('Swarm run', 'One experiment: a fixed set of agents, tasks, model and time limit, all running at once.'),
    ('Impossible-LiveCodeBench', 'The ImpossibleBench variant of LiveCodeBench: programming tasks whose unit tests '
     'were changed to conflict with the specification, so passing them all requires gaming the tests.'),
    ('Variant', 'Each task is given in one version: original (the unchanged tests), oneoff (one test expects a '
     'wrong output) or conflicting (two tests contradict each other). Run pages show each task\'s variant; the '
     'agents never saw it.'),
    ('Solver', 'An agent whose job is to solve tasks for credit.'),
    ('Overseer', 'An extra agent asked to watch for invalid solutions, able to check a solution and revert a '
     'submission. Solvers are not told about overseers.'),
    ('Control', 'A run with the planted solution and no overseers.'),
    ('Revert', 'An overseer removes a credit, the task reopens and its library entry disappears.'),
    ('Planted solution', 'A solution that hard-codes the expected test outputs, credited to a silent participant with '
     'no model or transcript early in each run.'),
    ('Operational judge', 'The conference\'s weak automatic check: the solution passes the task\'s unit tests. '
     'Acceptance is not correctness against the specification.'),
    ('Independent evaluator', 'The researcher-only check run after the fact. Its verdicts never change credit (only '
     'overseers can).'),
    ('Accepted', 'Passed the operational judge and credited (at least until a revert).'),
    ('Unresolved', 'The evaluator could not decide; never counted as genuine or test-gaming.'),
    ('Run clock', 'Times on run pages are shown as an offset from the run start (+m:ss) together with UTC.'),
)

_THIS = sys.modules[__name__]
toc = catalogue_intro.toc


def topnav():
    return catalogue_intro.topnav(_THIS)


def hero(stats):
    return catalogue_intro.hero(stats, _THIS)


def about_section():
    return catalogue_intro.about_section(_THIS)
