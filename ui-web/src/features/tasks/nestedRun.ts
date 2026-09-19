/* Parses the identity a `spawn` / `run_subagent_dag` / `load_playbook` call's
 * own RESULT text carries -- the receipt each tool returns the moment its
 * dispatch is accepted, before this node's own transcript has anything else
 * to say about the run it just started.
 *
 * `manager.py`'s spawn receipt ("Subagent [<label>] started (id: <task_id>).
 * ...") and `dag_tool.py` / `playbook/executor.py`'s dag receipt ("DAG
 * <run_id>: <n> nodes started" / "DAG <run_id>: started ..."). Pure string
 * parsing, no store: `DelegDtl` resolves the tasks store's own row from the
 * ids these return, rather than trusting the receipt for anything the store
 * can answer better (state, duration, scale).
 */

export interface SpawnReceipt {
  label: string
  taskId: string
}

/* The label can itself contain "] started (id: ...)" (a task summary that
   quotes another receipt verbatim), so the label is read greedily -- a
   regex engine's own backtracking lands on the LAST such marker in the
   string, which is the one this receipt actually ends on. */
const SPAWN_RECEIPT = /^Subagent \[([\s\S]*)\] started \(id: ([^)]+)\)\./

export function parseSpawnReceipt(result: string): SpawnReceipt | null {
  const m = SPAWN_RECEIPT.exec(result)
  return m ? { label: m[1] as string, taskId: m[2] as string } : null
}

export interface DagReceipt {
  runId: string
}

/* Matches both the graph tool's own receipt ("DAG <run_id>: 3 nodes
   started") and the playbook loader's ("DAG <run_id>: started '<name>'
   (<n> steps); ...") -- the run id is the store's own key either way, and
   nothing past it needs parsing. */
const DAG_RECEIPT = /^DAG (\S+): (?:\d+ nodes started|started)\b/

export function parseDagReceipt(result: string): DagReceipt | null {
  const m = DAG_RECEIPT.exec(result)
  return m ? { runId: m[1] as string } : null
}
