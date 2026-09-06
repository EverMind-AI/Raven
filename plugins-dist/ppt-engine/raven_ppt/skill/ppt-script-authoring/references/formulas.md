# Formulas

Called from §9 of the skill, which holds the rule: never set an expression with
`write`.

```python
from ppt_layout import formula
formula(slide, box, "掩码 logits = (F_4, Q'_{inst})；分类 logits = (Q'_{inst}, concat(Q'_{sem}, Q'_{bg}))", T)
```

`_x` and `_{xyz}` subscript, `^x` and `^{xyz}` superscript. A lone latin letter comes
out italic the way a variable is, and a word of two or more letters upright the way
`concat` and `softmax` are.

`formula()` splits at the expression's own semicolons when even the floor is not enough,
which is the one break that does not fall inside a symbol.

**Anything that stacks is TeX.** A fraction, a root, a sum or product with limits, an
integral: write the expression in TeX and `formula` typesets it as a picture in the
deck's ink at the size you give, scaled down only if the box is narrower:

```python
formula(slide, box, r"\mathrm{Attention}(Q,K,V)=\mathrm{softmax}\left(\frac{QK^{T}}{\sqrt{d_k}}\right)V", T, size=20)
```

`\frac{}{}`, `\sqrt{}`, `\sum_{i=1}^{n}`, `\int`, `\mathrm{softmax}` for a word set
upright, Greek by name, `\cdot`, `\times`, `\le`. A slash and a `√` sign are not a
fraction and a root -- a delivered page set the attention formula that way and it read as
a line of code. The expression carries no CJK: `write` the sentence around it and give
`formula` the mathematics alone. `formula_type_size` answers for TeX too, with the size the
picture lands at in that width.

Set the rest as text in the deck's font, real Unicode where it reads cleanly and
plain language where it does not. Never paste TeX source onto a slide. If an
expression is not load-bearing, explain the idea in words.
