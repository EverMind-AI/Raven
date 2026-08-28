# The task to paste

Paste one of these into the TUI, verbatim. Do not add hints about machines,
tools, or the on-call agent -- the demo is watching raven work those out.

## English

```
Run a 2D heat-conduction case for me; I want the smallest end-time L2 error
you can get. The solver and job script are in demos/oncall_heat2d/arena --
use them as they are, don't write your own and don't modify them.
This problem has an exact solution and the script computes the error itself;
the number you report has to hold up -- exit code 0 does not mean the result
is usable. You have 25 minutes, use them as you see fit. I also use this
machine, so don't assume you have it to yourself.
When you're done, tell me the configuration you settled on and its error.
Ask me if unsure -- but I may be slow to answer, so don't sit idle.
```

## 中文

```
帮我跑一个二维热传导的算例,我要末时刻的 L2 误差尽可能小。
求解器和作业脚本在 demos/oncall_heat2d/arena,直接用,别自己写,也别改它。
这个问题有解析解,脚本会把误差算出来,报上来的那个数得站得住 ——
脚本退出码是 0 不代表结果能用。
一共给你 25 分钟,用完为止。这台机器我自己也在用,别假定独占。
完事跟我说你最后选的配置和它的误差。
有拿不准的可以问我,但我可能很久不回,别干等。
```

## If it asks about a machine

For this very computer it may skip the ask and register your laptop itself,
telling you what it wrote down -- that is the chain working, not a bug.
If it does ask, answer with your own computer. The pieces it needs (any wording works):

- what you call it: anything, e.g. "my laptop"
- how it is reached: **this very computer** (no ssh)
- what is installed: "Python 3 + numpy; the heat2d case in demos/oncall_heat2d/arena"
- budget unit: minute
- jobs at once: 1
