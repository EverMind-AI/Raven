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

What §9's rule is made of: a delivered page in a 3.9in column set its expression with
`write`, the box broke the line after "分类" and put the rest of the clause on the
next line, and every subscript in it was flat — F4 for F-sub-4, Qinst for Q-sub-inst
— so the one thing the notation carried was gone. `formula()` splits at the
expression's own semicolons when even the floor is not enough, which is the one break
that does not fall inside a symbol.

Set the rest as text in the deck's font, real Unicode where it reads cleanly and
plain language where it does not. Never paste TeX source onto a slide. If an
expression is not load-bearing, explain the idea in words.
