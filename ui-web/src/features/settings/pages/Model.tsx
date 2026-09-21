/* Model settings: which model each job runs on. The accounts those models come
   from are the Model providers page -- "what do I have" and "how do I spend it"
   are two questions, and one page answering both buried the second. */
import { Roles } from '../providers/Roles'

import type { JSX } from 'react'

export function Model(): JSX.Element {
  return <Roles />
}
