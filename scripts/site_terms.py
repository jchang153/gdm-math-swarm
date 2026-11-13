"""Plain words for the public site (owner request, November 13, 2026).

"things like 'rubric 3.1' or 'simple behavior judge' are confusing terms because anyone reading the site won't know
what those mean. Can you simplify so these backend side things aren't exposed."

One vocabulary for every page a visitor sees:

- the simplified one-pass judge ("simple", "simplified", gdm-simple-judge/1) is the **behavior judge**;
- the earlier multi-stage rubric judge ("rubric 3.1", gdm-judge-rubric/3.1) is the **detailed behavior judge**;
- the conference's operational judge is the **weak judge** (the About page's key term);
- the story map's model readers are named by what they read instead of stage 1/2/3: the **thought reading**
  (stage 1), the **reaction reading** (stage 2) and the **final reading** (stage 3);
- script, module and record names (roles.py, run_metrics.py, settings.json, budget.pilot, common_start, ...) and
  version tags (answer-echo-v2, judge revision v3) are dropped from running text.

``apply(document)`` rewrites the visible text of a page: text nodes and the title/aria-label/alt/placeholder
attributes, outside <script>, <style>, <pre> and <code>. Tags, links, ids, classes, data attributes, URL hashes and
scripts never change, and the rules match only template wording (never agent quotes: agents were never told these
names). It is idempotent. Generators call it through ``site_layout.apply``; ``publish_cloudflare.py restyle``
applies it to the pages already on the site (``covers``), and ``prepare_hf_library.py`` to a bundle's pages.
Raw records (results.json, judge JSON) keep their field names and versions.
"""
import re

APOS = r"(?:'|&#x27;|&#39;|’)"
SEP = r'(?:·|&middot;)'

# Pages of a run whose visible text this module rewrites (site paths or run-relative paths).
PAGES = re.compile(r'(?:^|/)(?:results\.html|conference-view/index\.html|analysis/(?:simple_judge|judge|incident)/'
                   r'(?:[^/]+/)*[^/]+\.html|analysis/storymap/(?:[^/]+/)*[^/]+\.html)$')

_NAME = {'1': 'thought', '2': 'reaction', '3': 'final'}


def covers(name):
    """True for a run page whose text this module rewrites (not the conference's captured agent files)."""
    return bool(PAGES.search(name)) and not name.endswith('/graph.html')


def _case(word, like):
    return word[:1].upper() + word[1:] if like[:1].isupper() else word


def _stage_noun(m):
    """stage-N <noun> -> the reading's name (stage-1 reader -> thought reader, stage-3 reading -> final reading)."""
    cap, n, noun = m[1], m[2], m[3]
    name = _NAME[n]
    if noun.startswith(('reading', 'reader')):
        text = f'{name} {noun}'
    elif noun.startswith('reaction') and n == '2':
        text = 'reaction reading' + ('s' if noun.endswith('s') else '')
    else:  # quotes, chunk, position, phases, state(s), call, codes
        text = f'{name}-reading {noun}'
    return _case(text, cap)


_READS = {'1': 'thoughts', '2': 'reactions', '3': 'final reading'}


def _paren_stage(m):
    """'<label> (stage N)': model names say which reading they made; labels that already name it drop the tag."""
    label, first, last = m[1], m[2], m[3]
    if not re.match(r'(?:Claude|GPT|Gemini)\b', label):
        return label
    if last:
        return f'{label} ({"reactions and final reading" if (first, last) == ("2", "3") else _READS[first] + " to " + _READS[last]})'
    return f'{label} ({_READS[first]})'


_RULES = [(re.compile(p), r) for p, r in (
    # --- The two behavior judges -------------------------------------------------------------------------------
    (r'Behavior judge \(rubric 3\.1\)', 'Detailed behavior judge'),
    (r'\b([Tt])he earlier rubric[-‑ ]3\.1 (?:behavior )?judge', r'\1he detailed behavior judge'),
    (r'\brubric[-‑ ]3\.1 (?:behavior )?judge', 'detailed behavior judge'),
    (r'\bRubric[-‑ ]3\.1 (?:behavior )?judge', 'Detailed behavior judge'),
    (r'\(rubric 3\.1\)', '(detailed behavior judge)'),
    (r'\brubric 3\.1: ', 'detailed judge: '),
    (r'\b([Ss])implified one-pass (?:behavior )?judge(?: \(gdm-simple-judge/\d+\))?',
     lambda m: _case('behavior judge', m[1])),
    (r'\b([Ss])implified (?:behavior )?judge', lambda m: _case('behavior judge', m[1])),
    (r'\bSimple behavior judge\b', 'Behavior judge'),
    (r'\b([Ss])imple judge\b', lambda m: _case('behavior judge', m[1])),
    (r'\b([Bb]ehavior judge) \(default since October 24, 2026\)', r'\1'),
    (r' ' + SEP + r' gdm-simple-judge/\d+', ''),
    (r'gdm-simple-judge/\d+ ' + SEP + ' ', ''),
    (r'(detailed behavior judge) \(gdm-judge-rubric/[\d.]+\)', r'\1'),
    (r'^Behavior judge ([A-Za-z0-9_-]+\d{8}T\d{6}Z)$', r'Detailed behavior judge \1'),
    (r'\bBehavior judge: misbehavior and evaluation awareness', 'Detailed behavior judge: misbehavior and evaluation awareness'),
    (r'(?<!Detailed )\bBehavior judge evidence', 'Detailed behavior judge evidence'),
    (r'\(judge rubric gdm-judge-rubric/[\d.]+, post-hoc\)', '(post-hoc)'),
    (r' ' + SEP + r' rubric gdm-judge-rubric/[\d.]+ ' + SEP + r' judge revision v[\d.]+(?::[^<·&]*)?', ''),
    (r' ' + SEP + r' mode (?:paid|mock)\b', ''),
    (r'Definition \(behavior judge fields\)', 'Definition'),
    # Unlabelled agents: why the judge gave no label, in words.
    (r'gave no label \((?:failed|unparsed): (?:unparseable_response|invalid_json|parse_error)\)',
     'gave no label (its reply could not be read)'),
    (r'gave no label \(failed: max_tokens\)', 'gave no label (its reply was cut off)'),
    (r'gave no label \((?:failed|unparsed): [A-Za-z0-9_]+\)', 'gave no label (the model call failed)'),
    (r'gave no label \(no judge row\)', 'gave no label (not judged)'),
    (r'gave no label \([a-z_]+: [A-Za-z0-9_]+\)', 'gave no label (the model call failed)'),
    (r'Role from the run settings \(research/analysis/roles\.py\): ', 'Role from the run settings: '),
    (r'As in the judge rubric, Section 5: (t)he agent', r'The agent'),
    (r'As in the judge rubric, Section 5: ', ''),
    # --- The conference's weak judge ---------------------------------------------------------------------------
    (r'\b([Tt]he|[Aa]) weak operational judge', r'\1 weak judge'),
    (r'\boperational judge', 'weak judge'),
    (r'\bOperational judge', 'Weak judge'),
    (r'; operational only\b', '; weak judge only'),
    (r'([Cc])onference \(operational\) outcomes', r'\1onference (weak judge) outcomes'),
    # --- Story map readings instead of stages ------------------------------------------------------------------
    (r'read by models in three stages \(stage 1 codes its thoughts; stage 2 judges its reaction to each event; '
     r'stage 3 writes its positions and final reading\)',
     'read by models in three passes (one codes its thoughts, one judges its reaction to each event, one writes its '
     'positions and final reading)'),
    (r'Stage 1 codes the (agent|overseer)(' + APOS + r')s thoughts; stage 2 judges its reaction to each event; '
     r'stage 3 writes its positions and final reading',
     r'One model pass codes the \1\2s thoughts, one judges its reaction to each event, one writes its positions '
     r'and final reading'),
    (r'\bStage-2 reactions are read only at the first block', 'Reaction readings are made only at the first block'),
    (r'\bStage-2 reactions to pushed items', 'Reaction readings of pushed items'),
    (r'\(stage 1 ([^,()]+), stages 2[-–]3 ([^()]+)\)', r'(\1 for thoughts, \2 for reactions and final readings)'),
    (r'\(stage 1 ([^,()]+), stage 2 ([^,()]+), stage 3 ([^()]+)\)',
     r'(\1 for thoughts, \2 for reactions, \3 for final readings)'),
    (r'the stage-3 reader noted that the stage-1 or stage-2 readings',
     'the final reader noted that the thought or reaction readings'),
    (r'the stage-3 reading; the stage-1 reading where stage 3 is missing',
     'the final reading; the thought reading where there is no final reading'),
    (r'the stage-1 to stage-3 model readers', 'the model readers'),
    (r'in the stage-2 reaction to\b', 'in the reaction reading of'),
    (r'drew a stage-2 reaction', 'drew a reaction'),
    (r'stage 3 not_run: run_limit', 'no final reading (spending limit reached)'),
    (r'(How the (?:readers|agents) reacted[^()<>]*|Reactions at episode starts) \(stage 2; ', r'\1 ('),
    (r'(Agents with each response code) \(stage 1\):', r'\1:'),
    (r'\bStage 3 cites a (block|reminder)', r'The final reading cites a \1'),
    (r'\bStage-1 reading, chunk by chunk', 'Thought reading, chunk by chunk'),
    (r'\bStage-2 reaction cites', 'Reaction reading cites'),
    (r'\bStage-([12]) quotes', lambda m: _case(_NAME[m[1]] + '-reading quotes', 'T')),
    (r'(Agents with each response code at any time) \(stage 1, ', r'\1 ('),
    (r'\(stage 2: registered yes\)', '(reaction reading: registered yes)'),
    (r'(Thought codes) \(Stage 1\)', r'\1'),
    (r'\(Stage 2, (one per (?:selected )?event)\)', r'(\1)'),
    (r' \(Stage 1 state, Stage 3\)', ''),
    (r'\(Stage 1 records them', '(the thought reading records them'),
    (r'; Stage 2 decides them', '; the reaction reading decides them'),
    (r'\bStage 2 reads the overseer', 'The reaction reading reads the overseer'),
    (r'\bStage ([123]) final fields', lambda m: _case(_NAME[m[1]] + ' reading fields', 'F')),
    (r'\bStage ([123]): (\d+ agents)', lambda m: _case(_NAME[m[1]] + ' reading', 'T') + ': ' + m[2]),
    (r'((?:Claude|GPT|Gemini)[- ][^;()<>]{1,30}?|thought codes|overseer readings|Reactions|How it reacted|Summary|'
     r'Final reading|a monitor block) \(stages? ([123])(?:[-–]([123]))?\)', _paren_stage),
    (r'\b(s)tage-([123]) (readings?|readers?|reactions?|quotes|chunk|position|phases?|states?|call|codes?|summary)\b',
     _stage_noun),
    # --- Script, module, record and config names ---------------------------------------------------------------
    (r' \(research/analysis/run_metrics\.py\)', ''),
    (r' \(scripts/rejudge_submissions\.py[^)]*\)', ''),
    (r'no results\.json budget\.pilot', 'no recorded spending'),
    (r' \(fake_rule\.is_fake, answer-echo-v\d+\)', ''),
    (r'(strict fake rule), answer-echo-v\d+', r'\1'),
    (r'as recorded in settings\.json', 'as recorded in the run settings'),
    (r'settings\.json names none', 'the run settings name none'),
    (r'\(the runtime' + APOS + r's common_start, \+0:00\)', '(+0:00)'),
    (r' \(common_start\)', ''),
    (r'\bstop: max_run_seconds\b', 'stop: time limit'),
    (r'USD; agent = results\.json budget\.pilot settled \+ held minus its monitor subtotal .*?; total = agent \+ '
     r'monitor', 'USD of model spending, settled plus still held: the agents (solvers and overseers) and the '
     'monitor; total = agents + monitor'),
)]

# Story map taxonomy pages: the developer notes at the top (taxonomy version, file conventions, schema tests) give way
# to one plain sentence. The codes themselves are unchanged.
_TAXONOMY_INTRO = re.compile(
    r'(<h1>(?:Overseer s|S)tory map taxonomy[^<]*</h1>)<p>Version <code>storymap-(?:overseer-)?taxonomy(?:-code)?/\d+</code>\..*?</p>'
    r'<p>The prompts insert this file verbatim\..*?</p>', re.S)
_TAXONOMY_TEXT = ('<p>The codes the story map\'s model readers apply to each {who} records. Experimental agents never '
                  'see them, the story-map prompts or any story-map output.</p>')

_SKIP = re.compile(r'<(script|style|pre|code|textarea)\b[^>]*>.*?</\1\s*>|<!--.*?-->', re.S | re.I)
_TAG = re.compile(r'<[^>]*>')
_ATTR = re.compile(r'(\s(?:title|aria-label|alt|placeholder)=)(?:"([^"]*)"|\'([^\']*)\')')


# Every rule needs one of these words, so most text runs skip the rules (story-map agent pages have ~20k runs each).
_ANY = re.compile(r'(?i)stage|rubric|simpl|operational|judge|settings\.json|common_start|max_run_seconds|budget\.pilot|'
                  r'answer-echo|roles\.py|run_metrics|rejudge_submissions|fake_rule')


def text(value):
    """One run of visible text with the plain vocabulary."""
    if not _ANY.search(value):
        return value
    for pattern, repl in _RULES:
        value = pattern.sub(repl, value)
    return value


def _tag(tag):
    if '=' not in tag:
        return tag
    return _ATTR.sub(lambda m: (f'{m[1]}"{text(m[2])}"' if m[2] is not None else f"{m[1]}'{text(m[3])}'"), tag)


def _plain(chunk):
    out, pos = [], 0
    for m in _TAG.finditer(chunk):
        out.append(text(chunk[pos:m.start()]))
        out.append(_tag(m[0]))
        pos = m.end()
    out.append(text(chunk[pos:]))
    return ''.join(out)


def apply(document):
    """``document`` (HTML) with its visible text in the plain vocabulary; idempotent."""
    if not _ANY.search(document):
        return document
    document = _TAXONOMY_INTRO.sub(lambda m: m[1] + _TAXONOMY_TEXT.format(
        who="overseer's" if m[1].startswith('<h1>Overseer') else "agent's"), document, count=1)
    out, pos = [], 0
    for m in _SKIP.finditer(document):
        out.append(_plain(document[pos:m.start()]))
        out.append(m[0])
        pos = m.end()
    out.append(_plain(document[pos:]))
    return ''.join(out)
