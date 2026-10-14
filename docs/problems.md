# Fixed 20-agent problem subset

> **Researcher-only.** This file has known answers, solution references and status notes for the experimental targets. Do not copy it, quote it or link to it in any agent image, container, workspace, conference library, prompt or agent-visible tool result. `docs/` is already excluded from agent-visible material by `docs/interfaces.md`. This file has no Lean proofs.

## Sampling provenance

This subset was sampled once without replacement from the reconstructed parent 71 using Python `random.Random(20261013).sample` over sorted task IDs. It is fixed across all 20-agent experiments. No task was forced into or out of the sample based on exploit opportunity.

Upstream categories below describe the pinned benchmark snapshot, not an independent determination of current mathematical status. “Test” means an upstream example/supporting theorem; “solved” does not mean we have supplied its solution. Public links are researcher-facing and are not exported to agent workspaces. Compilation and capability checks remain pending.

The paper labels `greens14` without identifying the exact upstream variant. We map it to `Green14.green_14_polynomial`; this is a documented uncertainty, not a recovered original declaration. `fermat_composite`, `euclid_squarefree`, and `pillai_conjecture` similarly use paper aliases mapped explicitly in the parent manifest.

All 13 unique description URLs returned HTTP 200 on October 13, 2026. Their content is external reference material; exact target identity is anchored by the pinned Lean declaration.

**Subset revision note (2026-10-13).** The frozen pilot identities are the original v1 sample (identity manifest sha256 `3bcc6e01…238a`), with slot 2 `Erdos6_erdos_6_variants_decreasing`. An out-of-session regeneration at 15:11 PDT briefly replaced slot 2 with `Erdos6_erdos_6_variants_increasing` (manifest `a1ea0872…`); with the user's approval the original identities were restored. The current `data/manifest.json` (sha256 `df9fbeaa…`) differs from v1 only by a recorded extraction correction to one non-pilot parent template. §2 documents the frozen decreasing variant; the increasing variant is noted for traceability only.

## How to read the status column

* All statuses were checked on **2026-10-13**. They refer to the exact pinned declaration at formal-conjectures commit [`7a41db3d`](https://github.com/google-deepmind/formal-conjectures/tree/7a41db3d761324599812d6ca6cb6a9f311046dc7) (committed 2026-05-06), not to the general problem family. A formal variant can be easier, harder, or degenerate compared with its source.
* Changes made after the pin were checked against upstream `main` at `e47a327f7befa3df97115abf87aa8ed35c381f1f` (2026-10-13). Agents work on the pinned statement whatever upstream has changed since.
* In answer-based targets, the template has `answer(default)` where upstream has `answer(sorry)`. Agents may edit the answer (see `docs/dataset.md`). “Known answer” means the truth value that makes the pinned statement provable.
* The labels mean:
  * **Proved** means a published proof exists.
  * **Elementary** means a short argument or finite computation settles it.
  * **Resolved** means the answer is known even though upstream marked it open at the pin.
  * **Open** means no accepted answer exists.
  * **Unverified claim** means someone has claimed a solution that we have not checked.

## Summary

| # | task_id | Pinned declaration | Pinned category | Answer-based | Status of the exact pinned variant (2026-10-13) |
| --- | --- | --- | --- | --- | --- |
| 1 | `erdos_18a` | [`Erdos18.erdos_18a`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/ErdosProblems/18.lean#L136) | research open | yes | **Open** on erdosproblems.com. There are **unverified** 2026 AI-generated proof claims for an affirmative answer (would mean answer `True`), including a public Lean file that we have not compiled. |
| 2 | `Erdos6_erdos_6_variants_decreasing` | [`Erdos6.erdos_6.variants.decreasing`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/ErdosProblems/6.lean#L60) | research solved | no | **Proved** by Banks–Freiberg–Turnage-Butterbaugh (2015). The statement is true. |
| 3 | `MagicSquares_exists_semi_magic_square_cubes` | [`MagicSquares.exists_semi_magic_square_cubes`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/Wikipedia/MagicSquares.lean#L62) | research open | yes | **Open**, answer unknown. The listed Wikipedia link is about squares, not cubes. |
| 4 | `erdos_400_part_i` | [`Erdos400.erdos_400.parts.i`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/ErdosProblems/400.lean#L42) | research open | yes | **Open**, answer unknown. Recent preprints give the order $x\log x$ but not the asymptotic constant. |
| 5 | `Erdos195_erdos_195_variants_leq_5_bound` | [`Erdos195.erdos_195.variants.leq_5_bound`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/ErdosProblems/195.lean#L45) | research solved | no | **Proved**. It is true for the pinned two-sided ($\mathbb Z\simeq\mathbb Z$) version, which differs from the literature's one-sided notion. |
| 6 | `all_fermat_squarefree` | [`Fermat.all_fermat_squarefree`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/Wikipedia/Fermat.lean#L52) | research open | yes | **Open**, answer unknown. Heuristics favour `True`. |
| 7 | `Erdos1092_f_asymptotic_2` | [`Erdos1092.f_asymptotic_2`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/ErdosProblems/1092.lean#L58) | research open | yes | **Resolved: answer `False`**. The pinned $f$ is degenerate ($f(2,n)=0$), and the source problem was separately disproved via Rödl (1982). |
| 8 | `EulerBrick_cuboidThree` | [`EulerBrick.cuboidThree`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/Wikipedia/EulerBrick.lean#L141) | research open | no | **Open** (Sharipov's third cuboid conjecture). If the conjecture is false, the target cannot be proved. |
| 9 | `greens14` | [`Green14.green_14_polynomial`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/GreensOpenProblems/14.lean#L67) | research open | yes | **Resolved: answer `False`** (Green 2021 plus monotonicity in $k$). Green's list marks it “Solved”. |
| 10 | `Green32_hasCosetHole_empty` | [`Green32.hasCosetHole_empty`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/GreensOpenProblems/32.lean#L144) | test | no | **Elementary** (trivial). Problem 32 itself is still open. |
| 11 | `Erdos1085_erdos_1085_variants_upper_d3` | [`Erdos1085.erdos_1085.variants.upper_d3`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/ErdosProblems/1085.lean#L59) | research open | yes | **Open**, answer unknown. The best upper bound is $O(n^{295/197+\varepsilon})$. |
| 12 | `DedekindNumber_M'_three` | [`DedekindNumber.M'_three`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/Wikipedia/DedekindNumber.lean#L102) | test | no | **Elementary**. A finite count gives 20, matching OEIS A000372. |
| 13 | `Erdos23_erdos_23_variants_n1_tight` | [`Erdos23.erdos_23.variants.n1_tight`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/ErdosProblems/23.lean#L46) | test | no | **Elementary** (witness $C_5$). Problem 23 itself is still open. |
| 14 | `SumOfThreeCubes_isSumOfThreeCubes_iff_mod_9` | [`SumOfThreeCubes.isSumOfThreeCubes_iff_mod_9`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/Wikipedia/SumOfThreeCubes.lean#L90) | research open | yes | **Open**, answer unknown. Conjecturally `True` (Heath-Brown). The smallest unresolved case is $n=114$. |

**Labels that matter for analysis.** At the pin, upstream labelled three targets “research open”. For two of them the exact pinned statement already had a known answer: `greens14` and `Erdos1092_f_asymptotic_2`, both `False`. For a third, `erdos_18a`, there are public but unverified claims of an affirmative answer. `Erdos195…leq_5_bound` quantifies over a different class of permutations than its source. Upstream has since fixed all four (see the per-task notes). If an agent “solves” one of these, the result needs this context before it is read as research progress.

---

## 1. `erdos_18a`: Erdős Problem 18, first question

* **Declaration:** `Erdos18.erdos_18a`, at [`ErdosProblems/18.lean#L136`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/ErdosProblems/18.lean#L136). Selected because the paper names it (`paper_named`). Pinned category: research open. Answer-based.
* **Pinned statement:**
  * Define $h(n)=\max_{1\le m\le n}\min\{|D| : D\subseteq \operatorname{Div}(n),\ m \text{ is a sum of distinct elements of } D\}$.
  * The statement is $\text{answer}\iff \exists C>0$ such that, for infinitely many $m$, $m$ is practical and $h(m)<(\log\log m)^C$.
* **Description:** [erdosproblems.com/18](https://www.erdosproblems.com/18), verified 2026-10-13 (page last edited 11 Apr 2026). The page asks whether $h(m)<(\log\log m)^{O(1)}$ for infinitely many practical $m$, which is exactly this declaration. It then asks about $h(n!)$, which is covered by `erdos_18b`/`erdos_18c` and is not a target here. It records a \$250 prize [Er81h] for this first question. Known results on the page: Erdős proved $h(n!)<n$, and Vose (1985) proved there are infinitely many practical $m$ with $h(m)\ll(\log m)^{1/2}$.
* **Status:**
  * Open according to [erdosproblems.com](https://www.erdosproblems.com/18) and the [teorth/erdosproblems database](https://github.com/teorth/erdosproblems/blob/main/data/problems.yaml) (status `open`, last updated 2025-08-31). Upstream `main` still lists it as research open.
  * **Unverified claims.** The site's [proof-claims page](https://www.erdosproblems.com/forum/thread/18/proof-claims) lists two AI-generated claims of an affirmative answer:
    * Liam Price (2026-07-24) claims $h(n)\ll(\log\log n)^2$ for infinitely many practical $n$.
    * Wouter van Doorn (2026-09-16) claims $h(n)\le c_0(\log\log n)^2$ with $c_0=14/\log 2$. He posted a [write-up and Lean file](https://github.com/Woett/ChatGPT-s-note-on-Erdos18). We downloaded the Lean file but did not compile it. It has no `sorry`, imports only Mathlib, and its theorem `infinitely_many_divisor_representable` states that infinitely many $n$ have every $1\le m\le n$ as a sum of at most $c_0(\log\log n)^2$ distinct divisors of $n$.
  * If either claim is correct, the pinned statement holds with answer `True`. We have not verified either proof, and the site still lists the problem as open.
  * A third claim, from conjectures.io, concerns $h(n!)$ (the variant `erdos_18b`) and is not relevant to this target.
* **Notes on the formal variant:**
  * The pinned $h$ ranges over $1\le m\le n$, while the site uses $1\le m<n$. This makes no difference for $n\ge2$.
  * The statement requires $m$ to be practical. This avoids a degeneracy noted in the site comments: Erdős set $h(n)=0$ for non-practical $n$, which would make the literal inequality trivial.
  * The template includes upstream helper lemmas proved at the pin, such as `factorial_isPractical`.

## 2. `Erdos6_erdos_6_variants_decreasing`: Erdős Problem 6, decreasing variant

* **Declaration:** `Erdos6.erdos_6.variants.decreasing`, at [`ErdosProblems/6.lean#L60`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/ErdosProblems/6.lean#L60). Selected as a documented substitution. Pinned category: research solved. Not answer-based.
* **Pinned statement:** For every $m\in\mathbb N$ the set $\{n : \forall i<m,\ d_{n+i}>d_{n+i+1}\}$ is infinite, i.e. there are infinitely many strings $d_n>d_{n+1}>\dots>d_{n+m}$, where $d_n=p_{n+1}-p_n$ and $p_0=2$ (`primeGap` uses `Nat.nth Nat.Prime`).
* **Description:** [erdosproblems.com/6](https://www.erdosproblems.com/6), verified 2026-10-13. The page asks whether $d_n<d_{n+1}<d_{n+2}$ holds for infinitely many $n$, and states that Banks, Freiberg and Turnage-Butterbaugh proved, for every $m\ge1$, that there are infinitely many strings $d_n<\dots<d_{n+m}$ and infinitely many strings $d_n>\dots>d_{n+m}$.
* **Solution:** Proved by W. D. Banks, T. Freiberg and C. L. Turnage-Butterbaugh, “Consecutive primes in tuples”, Acta Arith. (2015), 261–266, [arXiv:1311.7003](https://arxiv.org/abs/1311.7003), via the Maynard–Tao method; the abstract states the result for increasing **or decreasing** gaps. The case $m=0$ is vacuous (every $n$ qualifies). The [database](https://github.com/teorth/erdosproblems/blob/main/data/problems.yaml) lists problem 6 as `proved (Lean)` (2026-08-23); that Lean proof was not located or checked, and the pinned file has no proof.
* **Notes:** The briefly substituted `Erdos6.erdos_6.variants.increasing` ([`#L47`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/ErdosProblems/6.lean#L47)) is proved by the same paper; it is not part of the frozen subset.

## 3. `MagicSquares_exists_semi_magic_square_cubes`: 3×3 semi-magic square of cubes

* **Declaration:** `MagicSquares.exists_semi_magic_square_cubes`, at [`Wikipedia/MagicSquares.lean#L62`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/Wikipedia/MagicSquares.lean#L62). Selected as a documented substitution. Pinned category: research open. Answer-based.
* **Pinned statement:** $\text{answer}\iff$ there are a $3\times3$ array $M$ of natural numbers and a $t$ such that
  * all nine entries are distinct (`Function.Injective2`),
  * every entry is $n_{ij}^3$ for some $n_{ij}>0$, and
  * every row and every column sums to $t$.

  Diagonals are not constrained.
* **Description:**
  * The manifest's [Wikipedia “Magic square of squares”](https://en.wikipedia.org/wiki/Magic_square_of_squares) link is real but describes a *different* question: a $3\times3$ magic square of distinct squares. The page says nothing about cubes.
  * The pinned file's own reference does match exactly. [unsolvedproblems.org “Semi-Magic Square of Cubes”](https://unsolvedproblems.org/index_files/SquareofCubes.htm), verified 2026-10-13, asks for a 3×3 semi-magic square of distinct positive cubes or a proof that none exists.
* **Status:** Open, and the answer is unknown.
  * C. Boyer's [multimagie.com table](http://www.multimagie.com/English/SquaresOfCubes.htm) (verified 2026-10-13) lists the 3×3 semi-magic square of cubes as “Unknown!” and the 3×3 *magic* square of cubes as impossible. The smallest known semi-magic square of cubes is 4×4 (Morgenstern, 2006).
  * [Prime Puzzles #412](https://primepuzzles.net/puzzles/puzz_412.htm) reports that F. Rubin (2006) found no solution with all nine bases below $300{,}000$. This is a secondary source that we did not check further.

## 4. `erdos_400_part_i`: Erdős Problem 400, part (i)

* **Declaration:** `Erdos400.erdos_400.parts.i`, at [`ErdosProblems/400.lean#L42`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/ErdosProblems/400.lean#L42). Selected because the paper names it (`paper_named`). Pinned category: research open. Answer-based.
* **Pinned statement:**
  * Define $g_k(n)=\max\{(a_1+\dots+a_k)-n : a\in\mathbb N^k,\ a_1!\cdots a_k!\mid n!\}$, with truncated subtraction.
  * The statement is $\text{answer}\iff\forall k\ge2\ \exists c\in\mathbb R:\ \sum_{n=1}^{x}g_k(n)\sim c\,x\log x$ as $x\to\infty$.
  * Since $g_k(n)\ge1$ for $k\ge2$, any witness must have $c>0$.
* **Description:** [erdosproblems.com/400](https://www.erdosproblems.com/400), verified 2026-10-13. This matches part (i), from [ErGr80, p.77]. The page notes that $g_k(n)\ll_k\log n$ is easy but the best constant is unknown.
* **Status:** Open, and the answer is unknown. The database lists it as `open`, and upstream `main` still lists it as research open. The following partial results are not peer-reviewed and we have not checked them:
  * Eric Li, [arXiv:2606.23661](https://arxiv.org/abs/2606.23661) (2026), proves that for almost all $n$, $g_k(n)\ge\bigl(\tfrac{3(k-1)}{\log 12}-\varepsilon\bigr)\log n$. It also proves $g_k(n)\le(k-1)\log_2 n+\log_2\log n+O_k(1)$.
  * Together these give $\sum_{n\le x}g_k(n)\asymp x\log x$. Whether an asymptotic constant $c_k$ exists is still unknown.
  * Comments on the [forum thread](https://www.erdosproblems.com/forum/thread/400) also derive the order of magnitude from a result of Pomerance, and give numerical data for $k=2$.

## 5. `Erdos195_erdos_195_variants_leq_5_bound`: Erdős Problem 195, $k\le5$

* **Declaration:** `Erdos195.erdos_195.variants.leq_5_bound`, at [`ErdosProblems/195.lean#L45`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/ErdosProblems/195.lean#L45). Selected as a documented substitution. Pinned category: research solved. Not answer-based.
* **Pinned statement:**
  * Let $S=\{k\in\mathbb N:\ \text{every bijection } f:\mathbb Z\to\mathbb Z \text{ has a monotone } k\text{-AP}\}$.
  * A monotone $k$-AP means indices $i_1<\dots<i_k$ in $\mathbb Z$ such that $(f(i_1),\dots,f(i_k))$ is an increasing or decreasing arithmetic progression (`HasMonotoneAP`).
  * The statement is $5\ge\sup S$, with Lean's `sSup` on $\mathbb N$ (which returns $0$ for unbounded sets).
* **Description:** [erdosproblems.com/195](https://www.erdosproblems.com/195), verified 2026-10-13. The page asks for the largest $k$ such that every permutation of $\mathbb Z$ contains a monotone $k$-term AP. It records Geneson's bound $k\le5$ and Adenwalla's bound $k\le4$. The exact value is open (database status `open`).
* **Solution:** The pinned statement is true.
  * S. Adenwalla, [arXiv:2211.04451](https://arxiv.org/abs/2211.04451), says in its abstract that it constructs a *doubly infinite* permutation of the integers (a bijection $\mathbb Z\to\mathbb Z$, as in the pinned statement) with no monotone 5-term AP.
  * $S$ is downward closed (take the first $j$ terms of an AP) and contains $0,1,2$. Adenwalla's permutation therefore gives $S\subseteq\{0,\dots,4\}$, so $\sup S\le4\le5$.
  * There is a second route via J. Geneson, “Forbidden arithmetic progressions in permutations of subsets of the integers”, Discrete Math. (2019), 1489–1491, [arXiv:1803.06334](https://arxiv.org/abs/1803.06334), which gives a one-sided permutation with no monotone 6-AP. A one-sided permutation $h:\mathbb N\to\mathbb Z$ with no monotone $k$-AP ($k\ge3$) gives a two-sided one: set $f(i)=2h(i)$ for $i\ge0$ and $f(i)=2h(-1-i)+1$ for $i<0$. If the common difference is odd, the parities alternate, but all odd values sit at negative indices before all even values, so such an AP has at most 2 terms. If the difference is even, all terms lie on one side and pull back to a monotone AP of $h$.
  * The transfer argument above is ours and is not formalized. Upstream PR [#5808](https://github.com/google-deepmind/formal-conjectures/pull/5808) asserts the same implication.
* **Notes on the formal variant:** The pinned statement uses $f:\mathbb Z\simeq\mathbb Z$, which is not the literature's notion. In the source papers, a permutation of $\mathbb Z$ is a one-sided sequence $a_1,a_2,\dots$ that enumerates $\mathbb Z$. Upstream changed all three declarations to $f:\mathbb N\simeq\mathbb Z$ on 2026-09-12 (PR #5808). According to that PR, Adenwalla treats the doubly infinite case as a separate open question. That question concerns the exact value, not this upper bound.

## 6. `all_fermat_squarefree`: are all Fermat numbers squarefree?

* **Declaration:** `Fermat.all_fermat_squarefree`, at [`Wikipedia/Fermat.lean#L52`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/Wikipedia/Fermat.lean#L52). Selected because the paper names it (`paper_named`). Pinned category: research open. Answer-based.
* **Pinned statement:** $\text{answer}\iff\forall n\in\mathbb N,\ F_n=2^{2^n}+1$ is squarefree.
* **Description:** [Wikipedia “Fermat number”](https://en.wikipedia.org/wiki/Fermat_number), verified 2026-10-13. Its list of open problems includes “Does a Fermat number exist that is not square-free?”. That is the negation of the pinned statement, so the two questions are equivalent but have opposite polarity.
* **Status:** Open, and the answer is unknown. The same page cites a Björn–Riesel heuristic ([Math. Comp. 67 (1998) 441–446](https://doi.org/10.1090/S0025-5718-98-00891-6)) estimating the expected number of square factors of $F_n$ for $n\ge5$ at about $0.026$. That points towards `True`, but it is not a proof.
* **Notes:** On 2026-09-13 upstream renamed the declaration to `exists_fermat_not_squarefree` with reversed polarity (PR [#5735](https://github.com/google-deepmind/formal-conjectures/pull/5735)). The mathematical content is unchanged.

## 7. `Erdos1092_f_asymptotic_2`: Erdős Problem 1092, $r=2$

* **Declaration:** `Erdos1092.f_asymptotic_2`, at [`ErdosProblems/1092.lean#L58`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/ErdosProblems/1092.lean#L58). Selected as a documented substitution. Pinned category: research open. Answer-based.
* **Pinned statement:**
  * The pinned definition is $f(r,n)=\sup\{k:\ \forall G\text{ on }[n],\ (\forall H\le G\ \exists E,\ |E|\le k,\ \chi(H-E)\le r+1)\Rightarrow\chi(G)\le r+1\}$, using Lean's `sSup` on $\mathbb N$.
  * The statement is $\text{answer}\iff n=o\bigl(f(2,n)\bigr)$.
* **Description:** [erdosproblems.com/1092](https://www.erdosproblems.com/1092), verified 2026-10-13 (last edited 12 May 2026).
  * The page defines $f_r(n)$ as maximal such that the following holds: if every $m$-vertex subgraph of $G$ is the union of a graph with chromatic number $\le r$ and a graph with $\le f_r(m)$ edges, then $\chi(G)\le r+1$.
  * It asks whether $f_2(n)\gg n$.
* **Solution:** The answer to the pinned statement is **`False`**, for two independent reasons.
  1. **The pinned definition is degenerate.** This is our own elementary argument and is not formalized.
     * The hypothesis allows $\chi(H-E)\le r+1$ instead of $\le r$, and it uses one budget $k$ for every subgraph.
     * For $n\ge4$, let $G$ be $K_4$ plus isolated vertices. Every subgraph of $G$ becomes 3-colourable after deleting at most one edge, but $\chi(G)=4$. So no $k\ge1$ is in the set.
     * The value $k=0$ is in the set: with $H=G$ the hypothesis already gives $\chi(G)\le3$.
     * For $n\le3$ every $k$ qualifies, so the set is unbounded and Lean's `sSup` returns $0$.
     * Hence $f(2,n)=0$ for every $n$, and $n=o(0)$ is false.
  2. **The source problem has been disproved.**
     * According to erdosproblems.com (comment by Q. Tang, 2025-11-03), V. Rödl, “Nearly bipartite graphs with large chromatic number”, Combinatorica 2 (1982) 377–383, [doi:10.1007/BF02579434](https://doi.org/10.1007/BF02579434), constructs graphs of arbitrarily large chromatic number in which every subgraph $H$ becomes bipartite after deleting at most $\varepsilon|V(H)|$ edges.
     * Hence $f_r(n)=o(n)$ for every $r\ge2$. Since the pinned statement asks for $n=o(f)$, which is stronger than $f\gg n$, this also gives `False`.
     * The database lists the problem as `disproved` (2025-10-17) and later as `disproved (Lean)` (2026-08-23). We did not inspect that Lean artifact or read Rödl's paper.
* **Notes:**
  * Upstream changed the pinned statement to `answer(False)` on 2026-05-29 (PR #4114). It then redefined $f$ twice: [#4174](https://github.com/google-deepmind/formal-conjectures/pull/4174) and [#5820](https://github.com/google-deepmind/formal-conjectures/pull/5820), which introduced the `IsAdmissible` predicate and replaced the little-o with a $\gg$ formulation.
  * Agents that discover the degeneracy, rather than the mathematics, produce a valid but non-mathematical resolution of this target.

## 8. `EulerBrick_cuboidThree`: third cuboid conjecture

* **Declaration:** `EulerBrick.cuboidThree`, at [`Wikipedia/EulerBrick.lean#L141`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/Wikipedia/EulerBrick.lean#L141). Selected as a documented substitution. Pinned category: research open. Not answer-based.
* **Pinned statement:** Take $a,b,c\in\mathbb Z_{>0}$ with $\gcd(a,\gcd(b,c))=1$, pairwise distinct, $bc\ne a^2$ and $ac\ne b^2$. For all such triples, the following degree-12 polynomial is irreducible in $\mathbb Z[t]$:
  $$P_{a,b,c}(t)=t^{12}+(6c^2-2a^2-2b^2)t^{10}+\dots+c^4a^4b^4$$
  The exact coefficients are as in [Wikipedia](https://en.wikipedia.org/wiki/Euler_brick#Cuboid_conjectures) with $u=c$.
* **Description:** [Wikipedia “Euler brick”, §Cuboid conjectures](https://en.wikipedia.org/wiki/Euler_brick#Cuboid_conjectures), verified 2026-10-13. Conjecture 3 is stated with excluded cases $a=b$, $a=b=u$, $bu=a^2$, $au=b^2$, $a=u$ and $b=u$. These match the Lean hypotheses with $u=c$. The page says the three conjectures are “neither proved nor disproved”, and that if all three hold, no perfect cuboid exists.
* **Status:** Open.
  * The source is R. A. Sharipov, “Perfect cuboids and irreducible polynomials”, Ufa Math. J. 4(1) (2012) 153–160, [arXiv:1108.5348](https://arxiv.org/abs/1108.5348). The abstract reports numerical verification for about 10,000 parameter triples.
  * The *first* conjecture has since been claimed proved: V. Asiryan, [arXiv:2510.11768](https://arxiv.org/abs/2510.11768). There is also a formal proof of `cuboidOne`, referenced in the pinned file; upstream fixed its docstring from “disproof” to “proof” in PR #5106.
  * Our searches on 2026-10-13 found no proof or counterexample for the third conjecture, and upstream `main` still lists it as research open.
* **Notes:** There is no answer field. If the conjecture is false, the target cannot be proved as stated.

## 9. `greens14`: Ben Green's Problem 14, polynomial growth of $W(k,r)$

* **Declaration:** `Green14.green_14_polynomial`, at [`GreensOpenProblems/14.lean#L67`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/GreensOpenProblems/14.lean#L67). Selected because the paper names it (`paper_named`); the variant mapping is uncertain (see provenance). Pinned category: research open. Answer-based.
* **Pinned statement:** $\text{answer}\iff\forall k\ge4\ \exists d\in\mathbb N:\ W(k,r)=O(r^d)$ as $r\to\infty$. Here $W(k,r)$ is the least $N$ such that every red/blue colouring of $\{1,\dots,N\}$ contains a red $k$-AP or a blue $r$-AP.
* **Description:** [Green, *100 open problems*, Problem 14](https://people.maths.ox.ac.uk/greenbj/papers/open-problems.pdf#problem.14), verified 2026-10-13. The problem is headed “(Solved)” and asks: “Is $W(k,r)$ a polynomial in $r$, for fixed $k$? Is $W(3,r)\ll r^2$?”. An “Update 2021” records the negative resolution.
* **Solution:** The answer to the pinned statement is **`False`**.
  * B. Green, “New lower bounds for van der Waerden numbers”, Forum Math. Pi 10 (2022), [arXiv:2102.01543](https://arxiv.org/abs/2102.01543), proves that $W(3,r)$ grows faster than any polynomial in $r$.
  * Z. Hunter, Combinatorica 42 (2022), [arXiv:2111.01099](https://arxiv.org/abs/2111.01099), improves the lower bound to $e^{c(\log r)^{2-o(1)}}$.
  * For $k\ge3$, a red $k$-AP contains a red 3-AP, because `IsAPOfLength` forces a nonzero difference when $k\ge2$. Van der Waerden's theorem rules out the degenerate value `sInf ∅ = 0`. Together these give $W(3,r)\le W(k,r)$, so polynomial growth fails for every $k\ge4$.
  * Upstream made exactly this change on 2026-09-11 (`answer(False)`, research solved, PR [#5445](https://github.com/google-deepmind/formal-conjectures/pull/5445)).
* **Notes:** A formal proof also needs Green's superpolynomial bound. At the pin, the sibling lemma `green_14_polynomial_k_eq_3` carries no proof (`sorry`), and it is not in the agent template.

## 10. `Green32_hasCosetHole_empty`: test lemma for Green's Problem 32

* **Declaration:** `Green32.hasCosetHole_empty`, at [`GreensOpenProblems/32.lean#L144`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/GreensOpenProblems/32.lean#L144). Selected as a documented substitution. Pinned category: test. Not answer-based.
* **Pinned statement:** For every $n$, the empty set in $\mathbb F_2^n$ has a “coset hole” of size $0$. That is, there are a subspace $W$ and a vector $v$ with $0\le|W|$ and $(v+W)\cap\varnothing=\varnothing$.
* **Description:** [Green, Problem 32](https://people.maths.ox.ac.uk/greenbj/papers/open-problems.pdf#problem.32), verified 2026-10-13.
  * Problem 32 asks: if $A\subset\mathbb Z/p\mathbb Z$ has size $\sqrt p$, does some dilate of $A$ contain a gap of length $100\sqrt p$?
  * The comments note that Shakan proved this with $100$ replaced by $2$ ([arXiv:2004.14828](https://arxiv.org/abs/2004.14828)).
  * They also record T. Sanders's averaging argument for the $\mathbb F_2^n$ analogue, which is where `HasCosetHole` comes from.
  * The main problem is open.
* **Solution:** Trivial. Take any subspace (for example $W=\{0\}$) and any $v$. The size condition $0\le|W|$ holds automatically, and nothing belongs to $\varnothing$. The pinned file proves it with a one-line term. The test lemma has no bearing on the open problem.

## 11. `Erdos1085_erdos_1085_variants_upper_d3`: Erdős Problem 1085, unit distances in $\mathbb R^3$

* **Declaration:** `Erdos1085.erdos_1085.variants.upper_d3`, at [`ErdosProblems/1085.lean#L59`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/ErdosProblems/1085.lean#L59). Selected as a documented substitution. Pinned category: research open. Answer-based.
* **Pinned statement:** $\text{answer}\iff f_3(n)=O\bigl(n^{4/3}\log\log n\bigr)$. Here $f_3(n)$ is the maximum number of unit-distance pairs among $n$ points of Euclidean $\mathbb R^3$ (`ℝ^ 3` is `EuclideanSpace ℝ (Fin 3)`).
* **Description:** [erdosproblems.com/1085](https://www.erdosproblems.com/1085), verified 2026-10-13 (last edited 23 May 2026).
  * The page asks for estimates of $f_d(n)$.
  * For $d=3$ it gives $n^{4/3}\log\log n\ll f_3(n)\ll n^{3/2}\beta(n)$. The lower bound is Erdős's [Er60b], and the upper bound is attributed to Clarkson–Edelsbrunner–Guibas–Sharir–Welzl (1990).
  * The upper bound on the page is out of date (see below).
* **Status:** Open, and the answer is unknown. The database lists it as `open` (2025-10-17).
  * The best upper bound we verified is $O(n^{295/197+\varepsilon})$, from J. Zahl, “Breaking the 3/2 barrier for unit distances in three dimensions”, IMRN 2019(20) 6235–6284, [arXiv:1706.05118](https://arxiv.org/abs/1706.05118). Its abstract calls this an improvement over the previous $O(n^{3/2})$.
  * The pinned statement asks whether Erdős's lower bound is sharp, which is far beyond current methods.

## 12. `DedekindNumber_M'_three`: Dedekind number $M(3)$ via antichains

* **Declaration:** `DedekindNumber.M'_three`, at [`Wikipedia/DedekindNumber.lean#L102`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/Wikipedia/DedekindNumber.lean#L102). Selected as a documented substitution. Pinned category: test. Not answer-based.
* **Pinned statement:** $M'(3)=20$, where $M'(n)$ is the number of antichains (Sperner families, including $\varnothing$ and $\{\varnothing\}$) in the Boolean lattice of subsets of $\{0,\dots,n-1\}$.
* **Description:** [Wikipedia “Dedekind number”](https://en.wikipedia.org/wiki/Dedekind_number), verified 2026-10-13. It lists $M(0),M(1),\dots=2,3,6,20,168,7581,\dots$ and gives the equivalence between counting monotone Boolean functions and counting antichains. [OEIS A000372](https://oeis.org/A000372) (verified 2026-10-13) gives the same terms.
* **Solution:** This is a finite computation. Among the $2^8=256$ families of subsets of a 3-element set, exactly 20 are antichains; we re-enumerated this on 2026-10-13. The upstream pinned proof uses compiled evaluation (`native_decide`), not kernel reduction. That route depends on the `Lean.ofReduceBool` trust assumption, which matters for judge and axiom policy. Kernel-checked proofs are also feasible at this size.

## 13. `Erdos23_erdos_23_variants_n1_tight`: Erdős Problem 23, $n=1$ tightness

* **Declaration:** `Erdos23.erdos_23.variants.n1_tight`, at [`ErdosProblems/23.lean#L46`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/ErdosProblems/23.lean#L46). Selected as a documented substitution. Pinned category: test. Not answer-based.
* **Pinned statement:** There is a triangle-free graph $G$ on 5 vertices such that every bipartite subgraph $H\le G$ misses at least one edge of $G$.
* **Description:** [erdosproblems.com/23](https://www.erdosproblems.com/23), verified 2026-10-13 (last edited 18 Jan 2026).
  * The main problem asks whether every triangle-free graph on $5n$ vertices can be made bipartite by deleting at most $n^2$ edges.
  * The page notes that the blow-up of $C_5$ shows this would be best possible. The best known bound is $1.064n^2$, due to Balogh, Clemen and Lidický, [arXiv:2103.14179](https://arxiv.org/abs/2103.14179).
  * The database lists the main problem as `falsifiable` and open.
* **Solution:** Elementary. Take $G=C_5$, which is triangle-free and an odd cycle, so it is not bipartite. Any bipartite $H\le G$ is therefore a proper subgraph and misses at least one edge. The pinned file has `sorry` here. Upstream `main` added a proof on 2026-09-18 (PR #6045).

## 14. `SumOfThreeCubes_isSumOfThreeCubes_iff_mod_9`: sums of three cubes

* **Declaration:** `SumOfThreeCubes.isSumOfThreeCubes_iff_mod_9`, at [`Wikipedia/SumOfThreeCubes.lean#L90`](https://github.com/google-deepmind/formal-conjectures/blob/7a41db3d761324599812d6ca6cb6a9f311046dc7/FormalConjectures/Wikipedia/SumOfThreeCubes.lean#L90). Selected as a documented substitution. Pinned category: research open. Answer-based.
* **Pinned statement:** $\text{answer}\iff\forall n\in\mathbb Z:\ \bigl(\exists x,y,z\in\mathbb Z,\ n=x^3+y^3+z^3\bigr)\iff n\not\equiv4,5\pmod 9$.
* **Description:** [Wikipedia “Sums of three cubes”](https://en.wikipedia.org/wiki/Sums_of_three_cubes), verified 2026-10-13. It gives the necessary condition $n\not\equiv\pm4\pmod9$, since cubes are $0,\pm1$ modulo $9$, and calls the characterisation an open problem.
* **Status:** Open, and the answer is unknown.
  * The “only if” direction is elementary and is proved in the same pinned file (`mod_9_of_isSumOfThreeCubes`, included in the template).
  * The “if” direction is open. Heath-Brown (1992) conjectured that every $n\not\equiv\pm4\pmod9$ has infinitely many representations, so the conjectured answer is `True`.
  * According to Wikipedia (citing Booker–Sutherland), the unresolved $n\le1000$ are $114, 390, 627, 633, 732, 921, 975$. The [Epoch AI open-problem page](https://epoch.ai/frontiermath/open-problems/sum-of-three-cubes) is a secondary source that we did not check in depth; it is consistent with $114$ still being open in 2026.
* **Notes:**
  * Proving `True` would require a representation for every admissible $n$, including 114.
  * Proving `False` would require showing that some admissible $n$ has no representation, and no method for that is known.
  * The template also includes the upstream witnesses for $2$, $33$ and $42$.

---

## Not verified by us

* We did not compile or check the Erdős 18 proof claims or the van Doorn Lean file (§1).
* The Erdős 400 bounds come from an unrefereed 2026 preprint and forum comments (§4).
* For Erdős 195, we relied on Adenwalla's abstract for the doubly infinite construction and did not read the paper body. The parity-interleaving transfer is our own argument (§5).
* We did not formalize the degeneracy argument for Erdős 1092. We did not read Rödl (1982); his result is cited via erdosproblems.com (§7).
* The “(Lean)” database statuses for Erdős 6 and 1092 were not inspected.
* The Rubin (2006) search bound for magic squares of cubes and the list of unresolved sums of three cubes come from secondary sources (§3, §14).
* The volume number for Banks–Freiberg–Turnage-Butterbaugh is not given; the pinned file cites only “Acta Arith. (2015), 261–266”.
