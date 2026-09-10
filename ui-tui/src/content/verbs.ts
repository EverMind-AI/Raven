export const TOOL_VERBS: Record<string, string> = {
  browser: 'browsing',
  clarify: 'asking',
  create_file: 'creating',
  delegate_task: 'delegating',
  delete_file: 'deleting',
  execute_code: 'executing',
  image_generate: 'generating',
  list_files: 'listing',
  memory: 'remembering',
  patch: 'patching',
  read_file: 'reading',
  run_command: 'running',
  search_code: 'searching',
  search_files: 'searching',
  terminal: 'terminal',
  web_extract: 'extracting',
  web_search: 'searching',
  write_file: 'writing'
}

// What the status ticker says while a turn runs. Ordered as one round of work
// -- arrive, gather, work, converge -- rather than by how close the words are
// in meaning: at one word every 2.5s a reader sees most of the list in a single
// turn, and fifteen synonyms for "thinking" in a row read as a thesaurus.
//
// Each one is true of a raven and true of the agent. Not a filter list: these
// words never censor a model's reasoning, which is why an ordinary word like
// `tracing` is safe here (see `tickerNoise.ts`).
//
// `remembering` is the one that fits best and cannot be used -- `TOOL_VERBS`
// already spends it on the memory tool, and the ticker would then say the same
// word for "thinking" and for "calling a tool".
export const VERBS = [
  'circling',
  'scouting',
  'gathering',
  'sifting',
  'tracing',
  'turning',
  'weighing',
  'prying',
  'homing',
  'tallying',
  'caching',
  'watching',
  'perching',
  'plotting'
]
