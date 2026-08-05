# DAG per-node output-file list to the main agent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-node output-file list to the `run_subagent_dag` result text so the main agent can `Read`/review any node's output, not just the terminal outputs.

**Architecture:** A text-only addition in `SubAgentDagTool.call`: after `Files under: <dir>`, render a "Node output files:" section from `result.files` (already computed), then keep the inline terminal outputs. No change to the runner, store, manifest, file layout, or result `metadata`.

**Tech Stack:** Python 3.11+, `unittest`/`pytest`.

## Global Constraints

- **Additive / text-only:** only the `content` text of the DAG tool result changes; `metadata` (incl. `files`) and all on-disk files are unchanged.
- **List format:** `- <node> [<status>]: <output_file>` for every node in `result.files` order; when `output_file` is falsy, render `(no output file)`. Include `failed`/`skipped` nodes.
- **Keep the inline terminal outputs** (`## <node>\n<text>`) after the new section.
- **Style/CI:** black (line length **79**), flake8, pylint, mypy, docstring checks. Run `pre-commit run --files <changed files>` before committing; fix the code, never disable checks. Backend tests: `conda run -n ravenx python -m pytest tests/<file> -v`.
- **Tests:** assert real behavior; build expected lines from `metadata["files"]` (the run dir is randomized, so do not hardcode paths).
- **Commits:** `git add` only the files named in the task (never `git add -A`; never stage `.webapp_logs/*.pid`). End the commit message with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: List per-node output files in the DAG result text

**Files:**
- Modify: `src/agentscope/subagent/_dag/_tool.py` (the `call` text assembly, ~lines 155-160)
- Test: `tests/subagent_dag_tool_test.py`

**Interfaces:**
- Consumes: `result.files` — `list[dict]`, each `{"node": str, "status": str, "prompt_file": str | None, "output_file": str | None}`, ordered by `spec.nodes` (produced by `run_dag`/`_finalize`, unchanged).
- Produces: no signature change; the result `content` text gains a "Node output files:" section.

- [ ] **Step 1: Write the failing test**

Add this method to `class SubAgentDagToolTest` in `tests/subagent_dag_tool_test.py`:

```python
    async def test_result_text_lists_node_output_files(self) -> None:
        """The result text lists every node with its status + output file."""
        with tempfile.TemporaryDirectory() as workdir:
            tool = SubAgentDagTool(
                subagents={"s": _FakeSubAgent("s")},
                backend=LocalBackend(),
                workdir=workdir,
            )
            chunks = await _collect(
                tool.call(
                    nodes=[
                        {"id": "A", "subagent": "s", "prompt_template": "go"},
                        {
                            "id": "B",
                            "subagent": "s",
                            "depends_on": ["A"],
                            "prompt_template": "{{ A.output }}",
                        },
                    ],
                ),
            )
            text = chunks[-1].content[0].text
            files = {f["node"]: f for f in chunks[-1].metadata["files"]}
            self.assertIn("Node output files:", text)
            self.assertIn(
                f"- A [completed]: {files['A']['output_file']}",
                text,
            )
            self.assertIn(
                f"- B [completed]: {files['B']['output_file']}",
                text,
            )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `conda run -n ravenx python -m pytest tests/subagent_dag_tool_test.py::SubAgentDagToolTest::test_result_text_lists_node_output_files -v`
Expected: FAIL — the current result text has no "Node output files:" section (`assertIn("Node output files:", text)` fails).

- [ ] **Step 3: Add the section in `_dag/_tool.py`**

In `SubAgentDagTool.call`, replace this block:

```python
        lines = [
            f"DAG run {result.run_id} complete: {result.summary}",
            f"Files under: {result.dir}",
        ]
        for term in result.terminal_outputs:
            lines.append(f"\n## {term['node']}\n{term['text']}")
```

with:

```python
        lines = [
            f"DAG run {result.run_id} complete: {result.summary}",
            f"Files under: {result.dir}",
            "",
            "Node output files:",
        ]
        for entry in result.files:
            output_file = entry["output_file"] or "(no output file)"
            lines.append(
                f"- {entry['node']} [{entry['status']}]: {output_file}",
            )
        for term in result.terminal_outputs:
            lines.append(f"\n## {term['node']}\n{term['text']}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `conda run -n ravenx python -m pytest tests/subagent_dag_tool_test.py tests/subagent_dag_integration_test.py -v`
Expected: PASS — the new test plus all pre-existing DAG tool/integration tests (the added test class covers the two-node chain; existing tests assert `metadata`, which is unchanged).

- [ ] **Step 5: Pre-commit + commit**

```bash
pre-commit run --files src/agentscope/subagent/_dag/_tool.py tests/subagent_dag_tool_test.py
git add src/agentscope/subagent/_dag/_tool.py tests/subagent_dag_tool_test.py
git commit -m "$(cat <<'EOF'
feat(subagent): list per-node output files in the DAG result text

Add a "Node output files:" section (node [status]: output_file, every
node) so the main agent can Read/review any node's output. metadata
unchanged.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] Run the DAG test files once more: `conda run -n ravenx python -m pytest tests/subagent_dag_tool_test.py tests/subagent_dag_runner_test.py tests/subagent_dag_integration_test.py -v` — all green.

## Notes for the executor

- Do NOT change `metadata`, `_runner.py`, `_store.py`, or the on-disk file layout — the files already exist; this only surfaces their paths in the readable text.
- Do NOT stage `.webapp_logs/*.pid`. Never `git add -A`.
