#!/usr/bin/env python3
"""Rebuild the pinned reconstruction from an unpacked official snapshot.
No network or model calls. Output researcher manifests and sanitized task templates.
"""
from __future__ import annotations
import argparse, hashlib, json, random, re, shutil
from pathlib import Path
REV = '7a41db3d761324599812d6ca6cb6a9f311046dc7'
SOURCE_TREE_SHA256 = '0769386b35964695b060fb1895bff3808d24ba7226a44b2489998c7b563cd322'
POOL_SEED = 260904170
SAMPLE_SEED = 20261013
BLACKLIST = ('axiom','sorry','macro','syntax')
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

def candidates(source):
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
            decls=list(DECL.finditer(before)); chunks=[]
            last=0
            for j,d in enumerate(decls):
                end=decls[j+1].start() if j+1<len(decls) else len(before)
                block=before[d.start():end]
                chunk_raw=raw[d.start():end]
                preceding=raw[max(0,d.start()-1000):d.start()]
                attrs=re.findall(r'@\[category\s+([^,\]]+)',preceding)
                is_research=attrs and attrs[-1].strip().startswith('research')
                if re.match(r'^(?:theorem|lemma)\b',block) and (is_research or 'sorry' in block or 'native_decide' in block):
                    chunks.append(before[last:d.start()]);last=end
            chunks.append(before[last:]); prefix=strip_attributes(''.join(chunks))
            prefix='\n'.join(line.rstrip() for line in prefix.splitlines())
            prefix=re.sub(r'\n{3,}','\n\n',prefix).strip()+'\n\n'
            # Attribute associated with target should not survive removing comments.
            prefix=strip_attributes(prefix)
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
            if any(word in template for word in BLACKLIST):continue
            rel=path.relative_to(source).as_posix()
            title_match=re.search(r'^#\s+(.+)',raw,re.M)
            title=(title_match.group(1).strip() if title_match else path.stem)+' — '+name
            urls=re.findall(r'https?://[^\s)\]>"]+',raw[:m.start()])
            description=next((u for u in urls if 'apache.org' not in u and 'github.com' not in u),f'https://github.com/google-deepmind/formal-conjectures/blob/{REV}/{rel}')
            results.append(dict(task_id=qualified.replace('.','_'),title=title,theorem_name=qualified,source_path=rel,answer_based=answer,description_url=description,source_url=f'https://github.com/google-deepmind/formal-conjectures/blob/{REV}/{rel}#L{raw[:m.start()].count(chr(10))+1}',original_statement=original_statement,category=category,template_adjustments=adjustments,namespace_context=ctx,template=template,source_sha256=hashlib.sha256(raw.encode()).hexdigest(),answer_placeholder='default' if 'answer(sorry)' in statement else None))
    return results

def build(source,out):
    tree=hashlib.sha256()
    for path in sorted(source.rglob('*')):
        if path.is_file() and '.git' not in path.relative_to(source).parts:
            tree.update(path.relative_to(source).as_posix().encode()+b'\0'+path.read_bytes()+b'\0')
    if tree.hexdigest()!=SOURCE_TREE_SHA256:
        raise ValueError('Source tree does not match the pinned official archive; use a clean unpacked snapshot')
    cs=candidates(source); by={(c['source_path'].removeprefix('FormalConjectures/'),c['theorem_name'].split('.')[-1]):c for c in cs}
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
    out.mkdir(parents=True,exist_ok=True);(out/'templates').mkdir(exist_ok=True)
    for c in selected:
        template=c.pop('template');rel='data/templates/'+c['task_id']+'.lean';(out/'templates'/(c['task_id']+'.lean')).write_text(template)
        c['template_path']=rel;c['sha256']=hashlib.sha256(template.encode()).hexdigest()
    ids=random.Random(SAMPLE_SEED).sample([c['task_id'] for c in selected],14)
    manifest=dict(schema_version=1,upstream_revision=REV,upstream_tag='bench-v1-lean4.27.0',pool_seed=POOL_SEED,selection_method='Named mappings plus 15 test, 20 solved and remaining open substitutions sampled without replacement from lexically eligible declarations.',compilation_validation='pending',tasks=selected,paper_mappings=mappings)
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
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,default=Path('data'));args=p.parse_args();build(args.source,args.output)
