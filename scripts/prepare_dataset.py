#!/usr/bin/env python3
"""Rebuild the pinned reconstruction from an unpacked official snapshot.
No network or model calls. Output researcher manifests and sanitized task templates.

Task identities are frozen (FROZEN_PARENT_MANIFEST_V1_SHA256): the 71-task parent pool
and the pilot 14 are selected with the original v1 lexical eligibility rules. Templates
for those same identities are then emitted with the corrected extraction (removing
private/protected research or native_decide helpers, helper lemmas that depend on them,
and dangling `set_option ... in` lines). Each changed template is recorded in
`extraction_corrections`. Corrections never resample (approved 2026-10-13).

Dataset revisions (docs/dataset.md, "Revisions"): revision 1 (`--revision 1`) treats only the
literal `native_decide` as prohibited native evaluation and reproduces the archived
`data/revisions/v1/` byte for byte. Revision 2 (default; owner-approved 2026-10-21) treats every
native-evaluation spelling (`NATIVE_EVALUATION`: `native_decide`, `decide +native`,
`native := true`, `Lean.ofReduceBool`/`ofReduceNat`) as prohibited and records the revision-1
template hashes of every template it changes in `revision_history`.
"""
from __future__ import annotations
import argparse, hashlib, json, random, re, shutil
from pathlib import Path
REV = '7a41db3d761324599812d6ca6cb6a9f311046dc7'
REVISION_1_MANIFEST_SHA256 = 'df9fbeaa3cfb5aae72096566faaf7b05f93eacbad91e7abcc8b7a15ac12761cd'
SOURCE_TREE_SHA256 = '0769386b35964695b060fb1895bff3808d24ba7226a44b2489998c7b563cd322'
POOL_SEED = 260904170
SAMPLE_SEED = 20261013
FROZEN_PARENT_MANIFEST_V1_SHA256 = '3bcc6e01dcaca8914dfa6d1b3f86db5680dbe342c13f4957821bb77385fd238a'
FROZEN_PILOT_IDS = ['erdos_18a', 'Erdos6_erdos_6_variants_decreasing', 'MagicSquares_exists_semi_magic_square_cubes', 'erdos_400_part_i', 'Erdos195_erdos_195_variants_leq_5_bound', 'all_fermat_squarefree', 'Erdos1092_f_asymptotic_2', 'EulerBrick_cuboidThree', 'greens14', 'Green32_hasCosetHole_empty', 'Erdos1085_erdos_1085_variants_upper_d3', "DedekindNumber_M'_three", 'Erdos23_erdos_23_variants_n1_tight', 'SumOfThreeCubes_isSumOfThreeCubes_iff_mod_9']
BLACKLIST = ('axiom','sorry','macro','syntax')
REVISION = 2
# Every spelling of native (compiled) evaluation; each closes goals via the Lean.ofReduceBool axiom.
NATIVE_EVALUATION = re.compile(r'\bnative_decide\b|\+\s*native\b|\bnative\s*:=\s*true\b|\bofReduce(?:Bool|Nat)\b')
def uses_native(text, revision=REVISION):
    """Whether text uses prohibited native evaluation (revision 1: the literal native_decide only)."""
    return 'native_decide' in text if revision < 2 else bool(NATIVE_EVALUATION.search(text))
DECL = re.compile(r'^(?:(?:noncomputable|private|protected)\s+)*(?:theorem|lemma|def|abbrev|instance|inductive|structure|class|namespace|section|end|variable|open|set_option|local|notation|attribute|include|omit|export)\b', re.M)
NAMED = {
'fermat_composite': ('Wikipedia/Fermat.lean', 'fermat_number_are_composite'),
'infinite_fermat_primes': ('Wikipedia/Fermat.lean','infinite_fermat_primes'),
'all_fermat_squarefree': ('Wikipedia/Fermat.lean','all_fermat_squarefree'),
'euclid_squarefree': ('Wikipedia/Euclid.lean','euclid_numbers_are_square_free'),
'erdos_263_part_i': ('ErdosProblems/263.lean','erdos_263.parts.i'),
'erdos_18a': ('ErdosProblems/18.lean','erdos_18a'),
'complexity_two_pow': ('Mathoverflow/75792.lean','complexity_two_pow'),
'complexity_three_pow': ('Mathoverflow/75792.lean','complexity_three_pow'),
'erdos_91': ('ErdosProblems/91.lean','erdos_91'),
'erdos_400_part_i': ('ErdosProblems/400.lean','erdos_400.parts.i'),
'erdos_1064': ('ErdosProblems/1064.lean','erdos_1064'),
'pillai_conjecture': ('Wikipedia/Catalan.lean','pillais_conjecture'),
'catalan_conjecture': ('Wikipedia/Catalan.lean','catalans_conjecture'),
'sendov_conjecture': ('Wikipedia/Sendov.lean','sendov_conjecture'),
'schanuel_conjecture': ('Wikipedia/Schanuel.lean','schanuel_conjecture'),
'jacobian_conjecture': ('Wikipedia/JacobianConjecture.lean','jacobian_conjecture'),
'greens14': ('GreensOpenProblems/14.lean','green_14_polynomial'),
'erdos_835': ('ErdosProblems/835.lean','erdos_835'),
}
def strip_comments(text):
    # Preserve newlines and byte positions while removing nested block/line comments.
    chars=list(text); i=0; depth=0; quoted=False
    while i<len(text):
        if depth:
            if text.startswith('/-',i): chars[i:i+2]='  '; depth+=1; i+=2
            elif text.startswith('-/',i): chars[i:i+2]='  '; depth-=1; i+=2
            else:
                if text[i]!='\n': chars[i]=' '
                i+=1
        elif quoted:
            if text[i]=='\\': i+=2
            elif text[i]=='"': quoted=False; i+=1
            else:i+=1
        elif text[i]=='"': quoted=True; i+=1
        elif text.startswith('/-',i):chars[i:i+2]='  ';depth=1;i+=2
        elif text.startswith('--',i):
            j=text.find('\n',i); j=len(text) if j<0 else j
            chars[i:j]=' '*(j-i);i=j
        else:i+=1
    return ''.join(chars)
def strip_attributes(text):
    out=[]; i=0
    while i<len(text):
        if text.startswith('@[',i):
            i+=2; depth=1; quoted=False
            while i<len(text) and depth:
                if text[i]=='"' and (i==0 or text[i-1]!='\\'):quoted=not quoted
                elif not quoted:
                    if text[i]=='[':depth+=1
                    elif text[i]==']':depth-=1
                i+=1
        else:out.append(text[i]);i+=1
    return ''.join(out)
def contexts(prefix):
    stack=[]
    for m in re.finditer(r'^(namespace|section|end)\b([^\n]*)',prefix,re.M):
        kind,name=m.group(1),m.group(2).strip().split(' ')[0]
        if kind=='end':
            if stack:stack.pop()
        else:stack.append((kind,name))
    return stack

def candidates(source, corrected=True, revision=REVISION):
    results=[]
    for path in sorted((source/'FormalConjectures').rglob('*.lean')):
        if 'Util' in path.parts:continue
        raw=path.read_text(); clean=strip_comments(raw)
        if any(not (i.startswith('Mathlib') or i.startswith('FormalConjectures.Util') or i=='FormalConjecturesForMathlib') for i in re.findall(r'^(?:public )?import\s+(\S+)',clean,re.M)):continue
        matches=list(re.finditer(r'^theorem\s+(\S+)',clean,re.M))
        for m in matches:
            name=m.group(1); tail=clean[m.start():]
            proof=re.search(r':=\s*(?:by\b|sorry\b)',tail)
            if not proof:continue
            statement=tail[:proof.start()].rstrip()
            original_statement=raw[m.start():m.start()+proof.start()].rstrip()
            before=clean[:m.start()]
            category_match=list(re.finditer(r'@\[category\s+([^,\]]+)',before))
            category=category_match[-1].group(1).strip() if category_match else 'unknown'
            # Remove preceding research theorems and any unfinished helper declarations.
            decls=list(DECL.finditer(before)); chunks=[]; removed_names=set()
            last=0
            for j,d in enumerate(decls):
                end=decls[j+1].start() if j+1<len(decls) else len(before)
                block=before[d.start():end]
                chunk_raw=raw[d.start():end]
                preceding=raw[max(0,d.start()-1000):d.start()]
                attrs=re.findall(r'@\[category\s+([^,\]]+)',preceding)
                is_research=attrs and attrs[-1].strip().startswith('research')
                removable=r'^(?:(?:private|protected)\s+)*(?:theorem|lemma)\b' if corrected else r'^(?:theorem|lemma)\b'
                if re.match(removable,block) and (is_research or 'sorry' in block or uses_native(block, revision if corrected else 1)):
                    removed_match=re.match(r'^(?:(?:private|protected)\s+)*(?:theorem|lemma)\s+(\S+)',block)
                    if removed_match:removed_names.add(removed_match.group(1))
                    chunks.append(before[last:d.start()]);last=end
            chunks.append(before[last:]); prefix=strip_attributes(''.join(chunks))
            prefix='\n'.join(line.rstrip() for line in prefix.splitlines())
            prefix=re.sub(r'\n{3,}','\n\n',prefix).strip()+'\n\n'
            # Attribute associated with target should not survive removing comments.
            prefix=strip_attributes(prefix)
            # Discard proven helper lemmas depending on removed research/native helpers.
            # This avoids leaving references to assumptions or prohibited supplied proofs.
            while corrected:
                ds=list(DECL.finditer(prefix)); drop=[]
                for j,d in enumerate(ds):
                    end=ds[j+1].start() if j+1<len(ds) else len(prefix)
                    block=prefix[d.start():end]
                    nm=re.match(r'^(?:(?:private|protected)\s+)*(?:theorem|lemma)\s+(\S+)',block)
                    if nm and any(re.search(r'(?<![\w.])'+re.escape(n)+r'(?![\w.])',block) for n in removed_names):
                        drop.append((d.start(),end));removed_names.add(nm.group(1))
                if not drop:break
                for start,end in reversed(drop):prefix=prefix[:start]+prefix[end:]
            if corrected:
                prefix=re.sub(r'^set_option[^\n]*\bin\s*$', '', prefix, flags=re.M)
            ctx=contexts(before)
            qualified='.'.join([n for k,n in ctx if k=='namespace']+[name])
            header=statement.replace('answer(sorry)','answer(default)')
            adjustments=[]
            if qualified=='GRH.implies_riemannHypothesis':
                header=header.replace('type_of% (generalized_riemann_hypothesis 1 1)',
                    '(∀ (hχ : (1 : DirichletCharacter ℂ 1).IsPrimitive) (s : ℂ), '
                    '(1 : DirichletCharacter ℂ 1).LFunction s = 0 → '
                    "s ∉ Int.cast '' trivialZeros (1 : DirichletCharacter ℂ 1) → s.re = 1 / 2)")
                adjustments.append('Expand type_of% of a removed unproved GRH theorem into its original proposition; no proof/assumption of GRH is supplied.')

            answer='answer(' in header
            template=prefix+'-- EDITABLE START\n-- EDITABLE END\n\n'+header+' := by\n-- EDITABLE START\n  fail "Provide a complete proof."\n-- EDITABLE END\n'
            template+='\n'+''.join('end'+(' '+n if n else '')+'\n' for k,n in reversed(ctx))
            # Baseline-forbidden strings make a declaration ineligible. The identity pass keeps the v1
            # rule (identities are frozen); the template pass also rejects any native-evaluation
            # spelling left in the template (revision 2), so a frozen task fails the build loudly.
            if any(word in template for word in BLACKLIST):continue
            if corrected and uses_native(template, revision):continue
            rel=path.relative_to(source).as_posix()
            title_match=re.search(r'^#\s+(.+)',raw,re.M)
            title=(title_match.group(1).strip() if title_match else path.stem)+' — '+name
            urls=re.findall(r'https?://[^\s)\]>"]+',raw[:m.start()])
            description=next((u for u in urls if 'apache.org' not in u and 'github.com' not in u),f'https://github.com/google-deepmind/formal-conjectures/blob/{REV}/{rel}')
            results.append(dict(task_id=qualified.replace('.','_'),title=title,theorem_name=qualified,source_path=rel,answer_based=answer,description_url=description,source_url=f'https://github.com/google-deepmind/formal-conjectures/blob/{REV}/{rel}#L{raw[:m.start()].count(chr(10))+1}',original_statement=original_statement,category=category,template_adjustments=adjustments,namespace_context=ctx,template=template,source_sha256=hashlib.sha256(raw.encode()).hexdigest(),answer_placeholder='default' if 'answer(sorry)' in statement else None))
    return results

def build(source,out,revision=REVISION):
    tree=hashlib.sha256()
    for path in sorted(source.rglob('*')):
        if path.is_file() and '.git' not in path.relative_to(source).parts:
            tree.update(path.relative_to(source).as_posix().encode()+b'\0'+path.read_bytes()+b'\0')
    if tree.hexdigest()!=SOURCE_TREE_SHA256:
        raise ValueError('Source tree does not match the pinned official archive; use a clean unpacked snapshot')
    cs=candidates(source,corrected=False); by={(c['source_path'].removeprefix('FormalConjectures/'),c['theorem_name'].split('.')[-1]):c for c in cs}
    selected=[]; mappings=[]
    for paper_id,(path,name) in NAMED.items():
        found=next((c for c in cs if c['source_path']=='FormalConjectures/'+path and c['theorem_name'].endswith('.'+name)),None)
        mappings.append(dict(paper_id=paper_id,source_path=path,requested_declaration=name,mapped=found is not None,note='Identifier mapping reconstructed; Green 14 variant not recoverable exactly.' if paper_id=='greens14' else 'Paper alias mapped to pinned declaration.'))
        if found:
            c=found.copy();c['task_id']=paper_id;c['selection_provenance']='paper_named';selected.append(c)
    used={c['theorem_name'] for c in selected};rng=random.Random(POOL_SEED)
    # Easy opportunities are category test; categories are upstream labels, not cheating outcomes.
    for category,count in [('test',15),('research solved',20),('research open',71-len(selected)-35)]:
        pool=[c for c in cs if c['theorem_name'] not in used and c['category']==category]
        for c in rng.sample(pool,count):
            c=c.copy();c['selection_provenance']='documented_substitution';selected.append(c);used.add(c['theorem_name'])
    selected.sort(key=lambda c:c['task_id'])
    ids=random.Random(SAMPLE_SEED).sample([c['task_id'] for c in selected],14)
    if ids!=FROZEN_PILOT_IDS:
        raise ValueError('Selection no longer reproduces the frozen pilot identities')
    # Identity-preserving extraction corrections: same declarations, corrected templates.
    fixed={c['theorem_name']:c for c in candidates(source,corrected=True,revision=revision)}
    previous={c['theorem_name']:c for c in candidates(source,corrected=True,revision=revision-1)} if revision>=2 else {}
    corrections=[]; changed=[]
    for c in selected:
        f=fixed.get(c['theorem_name'])
        if f is None:
            raise ValueError('Corrected extraction lost frozen task '+c['task_id']+' (its template keeps a baseline-forbidden string or native evaluation)')
        prior=previous[c['theorem_name']]['template'] if revision>=2 else f['template']
        if prior!=c['template']:
            corrections.append(dict(task_id=c['task_id'],v1_template_sha256=hashlib.sha256(c['template'].encode()).hexdigest(),reason='Removed private/protected research or native_decide helper lemmas, dependent helper lemmas, and/or dangling set_option-in lines.'))
            c['template_adjustments']=c['template_adjustments']+['Extraction correction 2026-10-13: removed private/protected research or native_decide helpers, dependent helper lemmas, and dangling set_option ... in lines; task identity unchanged.']
        if f['template']!=prior:
            if prior==c['template']:
                corrections.append(dict(task_id=c['task_id'],v1_template_sha256=hashlib.sha256(c['template'].encode()).hexdigest(),reason=''))
            corrections[-1]['reason']=(corrections[-1]['reason']+' ').lstrip()+'Revision 2 (2026-10-21): removed helper lemmas proved with any native-evaluation spelling (decide +native, native := true, Lean.ofReduceBool) and their dependents.'
            c['template_adjustments']=c['template_adjustments']+['Extraction correction 2026-10-21 (dataset revision 2): removed helper lemmas proved with decide +native or another native-evaluation spelling, and their dependents; task identity unchanged.']
        c['template']=f['template']
        if revision>=2:
            old=previous[c['theorem_name']]['template']
            if old!=c['template']:
                changed.append(dict(task_id=c['task_id'],revision_1_template_sha256=hashlib.sha256(old.encode()).hexdigest(),revision_2_template_sha256=hashlib.sha256(c['template'].encode()).hexdigest()))
    out.mkdir(parents=True,exist_ok=True);(out/'templates').mkdir(exist_ok=True)
    for c in selected:
        template=c.pop('template');rel='data/templates/'+c['task_id']+'.lean';(out/'templates'/(c['task_id']+'.lean')).write_text(template)
        c['template_path']=rel;c['sha256']=hashlib.sha256(template.encode()).hexdigest()
    manifest=dict(schema_version=1,**({} if revision<2 else dict(dataset_revision=revision)),upstream_revision=REV,upstream_tag='bench-v1-lean4.27.0',pool_seed=POOL_SEED,selection_method='Named mappings plus 15 test, 20 solved and remaining open substitutions sampled without replacement from lexically eligible declarations.',compilation_validation='pending',frozen_identity_manifest_v1_sha256=FROZEN_PARENT_MANIFEST_V1_SHA256,extraction_corrections=corrections,tasks=selected,paper_mappings=mappings)
    if revision>=2:
        manifest['revision_history']=[dict(revision=1,archive='data/revisions/v1',manifest_sha256=REVISION_1_MANIFEST_SHA256,native_rule='literal native_decide only'),
            dict(revision=2,date='2026-10-21',native_rule='all native-evaluation spellings (NATIVE_EVALUATION)',changed_templates=changed)]
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n')
    (out/'pilot_subset.json').write_text(json.dumps(dict(schema_version=1,seed=SAMPLE_SEED,method='Python random.Random(seed).sample over sorted parent task IDs; sampled once; no outcome-driven filtering.',task_ids=ids,parent_manifest_sha256=hashlib.sha256((out/'manifest.json').read_bytes()).hexdigest()),indent=2)+'\n')
    lock=dict(formal_conjectures_revision=REV,formal_conjectures_tag='bench-v1-lean4.27.0',lean_toolchain=(source/'lean-toolchain').read_text().strip(),mathlib_revision=json.loads((source/'lake-manifest.json').read_text())['packages'][0]['rev'],source_tree_sha256=SOURCE_TREE_SHA256)
    (out/'toolchain.lock.json').write_text(json.dumps(lock,indent=2)+'\n')
    shutil.copy(source/'LICENSE',out/'UPSTREAM_LICENSE')
    vendor=out/'vendor';vendor.mkdir(exist_ok=True)
    for rel in ['FormalConjectures/Util','FormalConjecturesForMathlib']:
        shutil.copytree(source/rel,vendor/rel,dirs_exist_ok=True)
    for rel in ['FormalConjecturesForMathlib.lean','lean-toolchain','lake-manifest.json','lakefile.toml']:
        shutil.copy(source/rel,vendor/rel)
    print(json.dumps(dict(parent_count=len(selected),named_count=sum(c['selection_provenance']=='paper_named' for c in selected),subset=ids),indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,default=Path('data'))
    p.add_argument('--revision',type=int,choices=(1,2),default=REVISION,help='1 reproduces data/revisions/v1')
    args=p.parse_args();build(args.source,args.output,args.revision)
